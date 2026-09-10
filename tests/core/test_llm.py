import asyncio
import sys
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from core.cancellation import CancellationSource, CancelledError
from core.llm import (
    LlmAttachment,
    LlmCompleted,
    LlmConfigurationError,
    LlmNetworkError,
    LlmProtocolError,
    LlmRequest,
    LlmTextDelta,
    LlmToolCall,
    MockProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ResilientLlmProvider,
    ToolDefinition,
)
from core.network_resilience import NetworkResilience, NetworkResilienceSettings


def strict_tool() -> ToolDefinition:
    return ToolDefinition(
        name="open_app",
        description="打开一个允许的应用",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"type": "object", "properties": {}, "required": []},
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
    ],
)
def test_tool_definition_rejects_non_strict_schema(schema):
    with pytest.raises(LlmConfigurationError):
        ToolDefinition("tool", "description", schema)


def test_llm_types_are_immutable_and_request_rejects_blank_text():
    event = LlmTextDelta("hello")
    request = LlmRequest("system", "hello", tools=(strict_tool(),))

    with pytest.raises(FrozenInstanceError):
        event.text = "changed"
    with pytest.raises(FrozenInstanceError):
        request.input_text = "changed"
    with pytest.raises(LlmConfigurationError):
        LlmRequest("system", "  ")


def test_llm_request_keeps_attachment_metadata_without_loading_content(tmp_path):
    attachment = LlmAttachment(
        str(tmp_path / "photo.png"), "photo.png", "image/png", 12, "a" * 64, "image"
    )
    request = LlmRequest("system", "请看看", attachments=(attachment,))
    assert request.attachments == (attachment,)


def test_responses_input_contains_image_reference_for_attachment(tmp_path):
    attachment = LlmAttachment(
        str(tmp_path / "photo.png"), "photo.png", "image/png", 12, "a" * 64, "image"
    )
    input_data = OpenAIResponsesProvider._build_input(
        LlmRequest("system", "请看看", attachments=(attachment,))
    )
    assert input_data[-1]["role"] == "user"
    assert input_data[-1]["content"][-1]["type"] == "input_image"
    assert input_data[-1]["content"][-1]["file_url"] == attachment.path


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
def test_small_text_attachment_is_inlined_for_responses_protocol(tmp_path, encoding):
    path = tmp_path / "名单.txt"
    path.write_bytes("医院甲\n医院乙".encode(encoding))
    attachment = LlmAttachment(
        str(path), path.name, "text/plain", path.stat().st_size, "a" * 64, "file"
    )
    request = LlmRequest("system", "请查看名单", attachments=(attachment,))

    responses_content = OpenAIResponsesProvider._build_input(request)[-1]["content"]

    assert "医院甲\n医院乙" in responses_content[-1]["text"]
    assert "附件正文开始" in responses_content[-1]["text"]


class FakeStream:
    def __init__(self, events):
        self.events = list(events)
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def close(self):
        self.closed += 1


class FakeResponses:
    def __init__(self, stream):
        self.stream = stream
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.stream


class FakeClient:
    def __init__(self, stream):
        self.responses = FakeResponses(stream)


def test_auto_reasoning_effort_omits_provider_override():
    stream = FakeStream([
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(id="response", usage=None),
        )
    ])
    client = FakeClient(stream)
    provider = OpenAIResponsesProvider(client=client, reasoning_effort="auto")

    asyncio.run(collect(provider, LlmRequest("system", "hello")))

    assert "reasoning" not in client.responses.calls[0]










class FakeModels:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def retrieve(self, model):
        self.calls.append(model)
        if self.error is not None:
            raise self.error
        return self.result


class ProbeClient:
    def __init__(self, models):
        self.models = models


class FailingResponses:
    def __init__(self, error):
        self.error = error

    async def create(self, **kwargs):
        del kwargs
        raise self.error


class FailingClient:
    def __init__(self, error):
        self.responses = FailingResponses(error)


async def collect(provider, request, source=None):
    source = source or CancellationSource()
    return [event async for event in provider.stream(request, source.token)]


class ScriptedNetworkProvider:
    def __init__(self, streams, probes=()):
        self.streams = list(streams)
        self.probes = list(probes)
        self.stream_calls = 0
        self.probe_calls = 0

    def stream(self, request, token):
        del request, token
        outcome = self.streams[self.stream_calls]
        self.stream_calls += 1

        async def events():
            if isinstance(outcome, BaseException):
                raise outcome
            for event in outcome:
                if isinstance(event, BaseException):
                    raise event
                yield event

        return events()

    async def probe(self):
        outcome = self.probes[self.probe_calls]
        self.probe_calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_resilient_llm_retries_network_failure_before_first_event():
    async def scenario():
        delays = []

        async def sleeper(delay):
            delays.append(delay)

        completed = LlmCompleted("response-1", 1, 1)
        provider = ScriptedNetworkProvider(
            [
                LlmNetworkError("private first failure"),
                [LlmTextDelta("你好"), completed],
            ]
        )
        resilient = ResilientLlmProvider(
            provider,
            NetworkResilience(sleeper=sleeper),
        )

        events = await collect(resilient, LlmRequest("system", "hello"))

        assert events == [LlmTextDelta("你好"), completed]
        assert provider.stream_calls == 2
        assert delays == [0.25]

    asyncio.run(scenario())


def test_resilient_llm_never_restarts_after_visible_stream_event():
    async def scenario():
        provider = ScriptedNetworkProvider(
            [[LlmTextDelta("visible"), LlmNetworkError("private failure")]]
        )
        resilient = ResilientLlmProvider(provider)
        received = []

        with pytest.raises(LlmNetworkError):
            async for event in resilient.stream(
                LlmRequest("system", "hello"),
                CancellationSource().token,
            ):
                received.append(event)

        assert received == [LlmTextDelta("visible")]
        assert provider.stream_calls == 1

    asyncio.run(scenario())


def test_resilient_llm_maps_open_circuit_without_calling_provider_again():
    async def scenario():
        provider = ScriptedNetworkProvider(
            [LlmNetworkError("private provider body")]
        )
        resilient = ResilientLlmProvider(
            provider,
            NetworkResilience(
                NetworkResilienceSettings(
                    max_attempts=1,
                    failure_threshold=1,
                )
            ),
        )
        request = LlmRequest("system", "hello")

        with pytest.raises(LlmNetworkError):
            await collect(resilient, request)
        with pytest.raises(LlmNetworkError) as captured:
            await collect(resilient, request)

        assert provider.stream_calls == 1
        assert "private provider body" not in str(captured.value)

    asyncio.run(scenario())


def test_resilient_llm_probe_uses_same_retry_policy():
    async def scenario():
        delays = []

        async def sleeper(delay):
            delays.append(delay)

        provider = ScriptedNetworkProvider(
            [],
            probes=[LlmNetworkError("private"), {"model": "gpt-test"}],
        )
        resilient = ResilientLlmProvider(
            provider,
            NetworkResilience(sleeper=sleeper),
        )

        assert await resilient.probe() == {"model": "gpt-test"}
        assert provider.probe_calls == 2
        assert delays == [0.25]

    asyncio.run(scenario())


def test_openai_provider_probe_retrieves_configured_model_without_generation():
    models = FakeModels(SimpleNamespace(id="gpt-test"))
    provider = OpenAIResponsesProvider(client=ProbeClient(models), model="gpt-test")

    result = asyncio.run(provider.probe())

    assert result == {"model": "gpt-test"}
    assert models.calls == ["gpt-test"]


def test_openai_provider_probe_maps_failure_without_leaking_response_body():
    error = type("ServiceUnavailableError", (RuntimeError,), {})(
        "private response body"
    )
    error.status_code = 503
    provider = OpenAIResponsesProvider(
        client=ProbeClient(FakeModels(error=error)),
        model="gpt-test",
    )

    with pytest.raises(LlmNetworkError) as captured:
        asyncio.run(provider.probe())

    assert "private response body" not in str(captured.value)


def test_openai_provider_probe_rejects_missing_model_identifier():
    provider = OpenAIResponsesProvider(
        client=ProbeClient(FakeModels(SimpleNamespace(id=""))),
        model="gpt-test",
    )

    with pytest.raises(LlmProtocolError, match="模型标识"):
        asyncio.run(provider.probe())


def test_openai_provider_sends_stateless_responses_request_and_maps_events():
    stream = FakeStream(
        [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(type="response.output_text.delta", delta="你"),
            SimpleNamespace(type="response.output_text.delta", delta="好"),
            SimpleNamespace(
                type="response.output_item.done",
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    name="open_app",
                    arguments='{"name":"calc"}',
                ),
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    id="resp-1",
                    usage=SimpleNamespace(input_tokens=12, output_tokens=4),
                ),
            ),
        ]
    )
    client = FakeClient(stream)
    provider = OpenAIResponsesProvider(client=client)
    request = LlmRequest(
        instructions="你是桌面助手",
        input_text="打开计算器",
        history=({"role": "assistant", "content": "你好"},),
        tools=(strict_tool(),),
        max_output_tokens=512,
    )

    events = asyncio.run(collect(provider, request))

    assert events == [
        LlmTextDelta("你"),
        LlmTextDelta("好"),
        LlmToolCall("call-1", "open_app", {"name": "calc"}),
        LlmCompleted("resp-1", 12, 4),
    ]
    assert stream.closed == 1
    assert client.responses.calls == [
        {
            "model": "gpt-5.6-terra",
            "instructions": "你是桌面助手",
            "input": [
                {"role": "assistant", "content": "你好"},
                {"role": "user", "content": "打开计算器"},
            ],
            "reasoning": {"effort": "low"},
            "max_output_tokens": 512,
            "tools": [
                {
                    "type": "function",
                    "name": "open_app",
                    "description": "打开一个允许的应用",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            ],
            "parallel_tool_calls": False,
            "stream": True,
            "store": False,
        }
    ]


def test_responses_provider_maps_output_limit_as_incomplete_completion():
    stream = FakeStream(
        [
            SimpleNamespace(type="response.output_text.delta", delta="部分回复"),
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    id="resp-limited",
                    usage=SimpleNamespace(input_tokens=100, output_tokens=1024),
                    incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                ),
            ),
        ]
    )
    provider = OpenAIResponsesProvider(client=FakeClient(stream))

    events = asyncio.run(collect(provider, LlmRequest("system", "hello")))

    assert events == [
        LlmTextDelta("部分回复"),
        LlmCompleted("resp-limited", 100, 1024, True, "max_output_tokens"),
    ]


















@pytest.mark.parametrize(
    "arguments",
    ["not-json", "[]", "null"],
)
def test_function_call_arguments_must_be_json_object(arguments):
    stream = FakeStream(
        [
            SimpleNamespace(
                type="response.output_item.done",
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    name="open_app",
                    arguments=arguments,
                ),
            )
        ]
    )
    provider = OpenAIResponsesProvider(client=FakeClient(stream))

    with pytest.raises(LlmProtocolError):
        asyncio.run(collect(provider, LlmRequest("system", "hello")))
    assert stream.closed == 1


def test_stream_without_completed_event_is_protocol_error():
    stream = FakeStream(
        [SimpleNamespace(type="response.output_text.delta", delta="partial")]
    )
    provider = OpenAIResponsesProvider(client=FakeClient(stream))

    with pytest.raises(LlmProtocolError, match="完成事件"):
        asyncio.run(collect(provider, LlmRequest("system", "hello")))
    assert stream.closed == 1


def test_cancelled_stream_closes_and_discards_later_events():
    source = CancellationSource()

    class CancellingStream(FakeStream):
        async def __anext__(self):
            event = await super().__anext__()
            source.cancel("user_interrupt")
            return event

    stream = CancellingStream(
        [SimpleNamespace(type="response.output_text.delta", delta="stale")]
    )
    provider = OpenAIResponsesProvider(client=FakeClient(stream))

    with pytest.raises(CancelledError):
        asyncio.run(
            collect(provider, LlmRequest("system", "hello"), source)
        )
    assert stream.closed == 1


@pytest.mark.parametrize(
    ("error_name", "status_code"),
    [
        ("APIConnectionError", None),
        ("APITimeoutError", None),
        ("APIStatusError", 408),
        ("APIStatusError", 409),
        ("RateLimitError", 429),
        ("InternalServerError", 500),
        ("ServiceUnavailableError", 503),
    ],
)
def test_transient_provider_errors_are_mapped_to_network_error(
    error_name,
    status_code,
):
    error_type = type(error_name, (RuntimeError,), {})
    error = error_type("包含不应泄漏的请求正文")
    error.status_code = status_code
    provider = OpenAIResponsesProvider(client=FailingClient(error))

    with pytest.raises(LlmNetworkError) as captured:
        asyncio.run(collect(provider, LlmRequest("system", "hello")))

    assert captured.value.retryable is True
    assert "请求正文" not in str(captured.value)


@pytest.mark.parametrize(
    ("error_name", "status_code"),
    [
        ("AuthenticationError", 401),
        ("PermissionDeniedError", 403),
        ("BadRequestError", 400),
    ],
)
def test_permanent_provider_errors_are_mapped_to_configuration_error(
    error_name,
    status_code,
):
    error_type = type(error_name, (RuntimeError,), {})
    error = error_type("包含不应泄漏的请求正文")
    error.status_code = status_code
    provider = OpenAIResponsesProvider(client=FailingClient(error))

    with pytest.raises(LlmConfigurationError) as captured:
        asyncio.run(collect(provider, LlmRequest("system", "hello")))

    assert captured.value.retryable is False
    assert "请求正文" not in str(captured.value)


def test_mock_provider_is_deterministic_and_honors_cancellation():
    configured = (
        LlmTextDelta("hello"),
        LlmCompleted("mock-1", 1, 1),
    )
    provider = MockProvider(configured)

    assert asyncio.run(
        collect(provider, LlmRequest("system", "hello"))
    ) == list(configured)

    source = CancellationSource()
    source.cancel("shutdown")
    with pytest.raises(CancelledError):
        asyncio.run(
            collect(provider, LlmRequest("system", "hello"), source)
        )


def test_compatible_provider_validates_base_url_and_builds_client(monkeypatch):
    created = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.responses = FakeResponses(FakeStream([]))

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI),
    )

    with pytest.raises(LlmConfigurationError, match="HTTP"):
        OpenAICompatibleProvider(base_url="ftp://example.com/v1", api_key="key")

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:8080/v1",
        api_key="key",
    )

    assert isinstance(provider, OpenAIResponsesProvider)
    assert created == [
        {
            "api_key": "key",
            "base_url": "http://localhost:8080/v1",
            "max_retries": 2,
            "timeout": 30.0,
        }
    ]
