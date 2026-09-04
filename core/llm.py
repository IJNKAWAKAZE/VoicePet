"""可替换的流式大模型网关"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlparse

from .cancellation import CancellationSource, CancellationToken, CancelledError
from .network_resilience import NetworkCircuitOpenError, NetworkResilience


class LlmError(RuntimeError):
    """LLM 子系统可向运行时报告的基础错误"""

    code = "llm.error"
    retryable = False


class LlmConfigurationError(LlmError):
    """LLM 依赖、认证或请求配置错误"""

    code = "llm.configuration"


class LlmNetworkError(LlmError):
    """可重试的 LLM 连接或服务错误"""

    code = "llm.network"
    retryable = True


class LlmProtocolError(LlmError):
    """LLM 流事件不符合本地协议"""

    code = "llm.protocol"


JsonValue = None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


def _freeze_json(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise LlmConfigurationError(
        f"JSON 值包含不支持的类型: {type(value).__name__}"
    )


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_strict_object_schema(schema: Mapping[str, Any]) -> None:
    if schema.get("type") != "object":
        raise LlmConfigurationError("工具 Schema 顶层类型必须为 object")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise LlmConfigurationError("工具 Schema 必须包含 properties 对象")
    if schema.get("additionalProperties") is not False:
        raise LlmConfigurationError("严格工具 Schema 必须禁止额外字段")
    required = schema.get("required")
    if not isinstance(required, (list, tuple)):
        raise LlmConfigurationError("严格工具 Schema 必须包含 required")
    if set(required) != set(properties):
        raise LlmConfigurationError("严格工具 Schema 必须要求全部属性")
    for property_schema in properties.values():
        if isinstance(property_schema, Mapping) and property_schema.get("type") == "object":
            _validate_strict_object_schema(property_schema)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """经过严格模式校验的本地工具定义"""

    name: str
    description: str
    input_schema: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise LlmConfigurationError("工具名称不能为空")
        if not self.description.strip():
            raise LlmConfigurationError("工具描述不能为空")
        _validate_strict_object_schema(self.input_schema)
        frozen = _freeze_json(self.input_schema)
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "input_schema", frozen)


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """发送给 Provider 的最小本地请求"""

    instructions: str
    input_text: str
    history: tuple[Mapping[str, JsonValue], ...] = ()
    tools: tuple[ToolDefinition, ...] = ()
    max_output_tokens: int = 1024

    def __post_init__(self) -> None:
        if not self.instructions.strip():
            raise LlmConfigurationError("LLM instructions 不能为空")
        if not self.input_text.strip():
            raise LlmConfigurationError("LLM 输入不能为空")
        if self.max_output_tokens <= 0:
            raise LlmConfigurationError("最大输出 token 必须大于零")
        frozen_history = tuple(_freeze_json(item) for item in self.history)
        if not all(isinstance(item, Mapping) for item in frozen_history):
            raise LlmConfigurationError("LLM 历史项必须是对象")
        object.__setattr__(self, "history", frozen_history)
        object.__setattr__(self, "tools", tuple(self.tools))


@dataclass(frozen=True, slots=True)
class LlmTextDelta:
    """Provider 返回的文本增量"""

    text: str


@dataclass(frozen=True, slots=True)
class LlmToolCall:
    """Provider 返回的结构化工具提议"""

    call_id: str
    name: str
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        frozen = _freeze_json(self.arguments)
        if not isinstance(frozen, Mapping):
            raise LlmProtocolError("工具参数必须是 JSON object")
        object.__setattr__(self, "arguments", frozen)


@dataclass(frozen=True, slots=True)
class LlmCompleted:
    """不包含正文的 LLM 完成统计"""

    response_id: str
    input_tokens: int
    output_tokens: int


LlmStreamEvent = LlmTextDelta | LlmToolCall | LlmCompleted


class LlmProvider(Protocol):
    """流式 LLM Provider 边界"""

    def stream(
        self,
        request: LlmRequest,
        token: CancellationToken,
    ) -> AsyncIterator[LlmStreamEvent]: ...


class ResilientLlmProvider:
    """为 LLM 流式请求增加退避和熔断且不重放已发布事件"""

    def __init__(
        self,
        provider: LlmProvider,
        resilience: NetworkResilience | None = None,
    ) -> None:
        self._provider = provider
        self._resilience = resilience or NetworkResilience()

    async def probe(self) -> Mapping[str, object]:
        """通过同一网络策略执行无生成能力探测"""

        probe = getattr(self._provider, "probe", None)
        if not callable(probe):
            raise LlmConfigurationError("当前 LLM Provider 不支持能力探测")
        source = CancellationSource()
        try:
            return await self._resilience.execute(
                probe,
                source.token,
                retryable=self._is_retryable,
            )
        except NetworkCircuitOpenError:
            raise LlmNetworkError("LLM 网络服务暂时熔断") from None

    async def stream(
        self,
        request: LlmRequest,
        token: CancellationToken,
    ) -> AsyncIterator[LlmStreamEvent]:
        """首个事件前允许重试并保留原有流式边界"""

        try:
            async for event in self._resilience.stream(
                lambda: self._provider.stream(request, token),
                token,
                retryable=self._is_retryable,
            ):
                yield event
        except NetworkCircuitOpenError:
            raise LlmNetworkError("LLM 网络服务暂时熔断") from None

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        return isinstance(error, LlmNetworkError)


class OpenAIResponsesProvider:
    """OpenAI Responses API 的无状态流式适配器"""

    def __init__(
        self,
        *,
        client: Any | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gpt-5.6-terra",
        reasoning_effort: str = "low",
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        if not model.strip():
            raise LlmConfigurationError("LLM 模型名称不能为空")
        if client is None:
            try:
                from openai import AsyncOpenAI
            except (ImportError, ModuleNotFoundError) as error:
                raise LlmConfigurationError(
                    "缺少 OpenAI 依赖，请安装 voicepet[llm]"
                ) from error
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "max_retries": max_retries,
                "timeout": timeout,
            }
            if base_url is not None:
                kwargs["base_url"] = base_url
            try:
                client = AsyncOpenAI(**kwargs)
            except Exception as error:
                raise LlmConfigurationError(
                    f"OpenAI 客户端初始化失败: {error}"
                ) from error
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort

    async def probe(self) -> Mapping[str, object]:
        """查询配置模型是否可访问且不发起生成"""

        try:
            model = await self._client.models.retrieve(self._model)
            model_id = getattr(model, "id", None)
            if not isinstance(model_id, str) or not model_id.strip():
                raise LlmProtocolError("模型查询结果缺少模型标识")
            return {"model": model_id}
        except LlmError:
            raise
        except Exception as error:
            raise self._map_provider_error(error) from error

    async def stream(
        self,
        request: LlmRequest,
        token: CancellationToken,
    ) -> AsyncIterator[LlmStreamEvent]:
        token.throw_if_cancelled()
        stream = None
        try:
            stream = await self._client.responses.create(
                model=self._model,
                instructions=request.instructions,
                input=self._build_input(request),
                reasoning={"effort": self._reasoning_effort},
                max_output_tokens=request.max_output_tokens,
                tools=self._build_tools(request.tools),
                parallel_tool_calls=False,
                stream=True,
                store=False,
            )
            completed = False
            async for event in stream:
                token.throw_if_cancelled()
                mapped = self._map_event(event)
                if mapped is None:
                    continue
                if isinstance(mapped, LlmCompleted):
                    completed = True
                yield mapped
                token.throw_if_cancelled()
            if not completed:
                raise LlmProtocolError("LLM 流缺少完成事件")
        except (LlmError, CancelledError):
            raise
        except Exception as error:
            raise self._map_provider_error(error) from error
        finally:
            if stream is not None:
                close = getattr(stream, "close", None)
                if close is None:
                    close = getattr(stream, "aclose", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result

    @staticmethod
    def _build_input(request: LlmRequest) -> list[dict[str, Any]]:
        history = [_thaw_json(item) for item in request.history]
        history.append({"role": "user", "content": request.input_text.strip()})
        return history

    @staticmethod
    def _build_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": _thaw_json(tool.input_schema),
                "strict": True,
            }
            for tool in tools
        ]

    @staticmethod
    def _map_event(event: Any) -> LlmStreamEvent | None:
        event_type = getattr(event, "type", "")
        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", None)
            if not isinstance(delta, str):
                raise LlmProtocolError("文本增量缺少字符串 delta")
            return LlmTextDelta(delta)
        if event_type == "response.output_item.done":
            item = getattr(event, "item", None)
            if getattr(item, "type", None) != "function_call":
                return None
            call_id = getattr(item, "call_id", None)
            name = getattr(item, "name", None)
            arguments_text = getattr(item, "arguments", None)
            if not all(isinstance(value, str) and value for value in (call_id, name)):
                raise LlmProtocolError("工具调用缺少 call_id 或 name")
            if not isinstance(arguments_text, str):
                raise LlmProtocolError("工具调用缺少 arguments")
            try:
                arguments = json.loads(arguments_text)
            except json.JSONDecodeError as error:
                raise LlmProtocolError("工具参数不是有效 JSON") from error
            if not isinstance(arguments, dict):
                raise LlmProtocolError("工具参数必须是 JSON object")
            return LlmToolCall(call_id, name, arguments)
        if event_type == "response.completed":
            response = getattr(event, "response", None)
            response_id = getattr(response, "id", None)
            if not isinstance(response_id, str) or not response_id:
                raise LlmProtocolError("完成事件缺少 response id")
            usage = getattr(response, "usage", None)
            return LlmCompleted(
                response_id,
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
            )
        return None

    @staticmethod
    def _map_provider_error(error: Exception) -> LlmError:
        status = getattr(error, "status_code", None)
        error_type = type(error).__name__
        name = error_type.lower()
        status_detail = f" HTTP {status}" if isinstance(status, int) else ""
        if status in {408, 409, 429} or isinstance(status, int) and status >= 500:
            return LlmNetworkError(
                f"LLM 服务暂时不可用: {error_type}{status_detail}"
            )
        if any(term in name for term in ("connection", "timeout", "ratelimit")):
            return LlmNetworkError(f"LLM 网络请求失败: {error_type}{status_detail}")
        if status in {400, 401, 403, 404} or any(
            term in name
            for term in ("authentication", "permission", "badrequest")
        ):
            return LlmConfigurationError(
                f"LLM 请求配置失败: {error_type}{status_detail}"
            )
        return LlmError(f"LLM Provider 执行失败: {error_type}{status_detail}")


class OpenAIChatCompletionsProvider(OpenAIResponsesProvider):
    """OpenAI Chat Completions API 的无状态流式适配器"""

    async def stream(
        self,
        request: LlmRequest,
        token: CancellationToken,
    ) -> AsyncIterator[LlmStreamEvent]:
        token.throw_if_cancelled()
        stream = None
        response_id: str | None = None
        input_tokens = 0
        output_tokens = 0
        tool_parts: dict[int, dict[str, str]] = {}
        try:
            settings: dict[str, Any] = {
                "model": self._model,
                "messages": self._build_chat_messages(request),
                "max_tokens": request.max_output_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if request.tools:
                settings["tools"] = self._build_tools(request.tools)
                settings["parallel_tool_calls"] = False
            stream = await self._client.chat.completions.create(**settings)
            async for chunk in stream:
                token.throw_if_cancelled()
                chunk_id = getattr(chunk, "id", None)
                if isinstance(chunk_id, str) and chunk_id:
                    response_id = chunk_id
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    output_tokens = int(
                        getattr(usage, "completion_tokens", 0) or 0
                    )
                for choice in getattr(chunk, "choices", ()) or ():
                    delta = getattr(choice, "delta", None)
                    content = getattr(delta, "content", None)
                    if isinstance(content, str) and content:
                        yield LlmTextDelta(content)
                        token.throw_if_cancelled()
                    self._collect_tool_fragments(delta, tool_parts)
            if response_id is None:
                raise LlmProtocolError("Chat Completions 流缺少响应标识")
            for index in sorted(tool_parts):
                parts = tool_parts[index]
                call_id = parts["id"]
                name = parts["name"]
                if not call_id or not name:
                    raise LlmProtocolError("Chat Completions 工具调用缺少标识或名称")
                try:
                    arguments = json.loads(parts["arguments"])
                except json.JSONDecodeError as error:
                    raise LlmProtocolError("工具参数不是有效 JSON") from error
                if not isinstance(arguments, dict):
                    raise LlmProtocolError("工具参数必须是 JSON object")
                yield LlmToolCall(call_id, name, arguments)
            yield LlmCompleted(response_id, input_tokens, output_tokens)
        except (LlmError, CancelledError):
            raise
        except Exception as error:
            raise self._map_provider_error(error) from error
        finally:
            if stream is not None:
                close = getattr(stream, "close", None)
                if close is None:
                    close = getattr(stream, "aclose", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result

    @staticmethod
    def _build_chat_messages(request: LlmRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.instructions}
        ]
        for item in request.history:
            item_type = item.get("type")
            if item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments")
                if not all(
                    isinstance(value, str) and value
                    for value in (call_id, name, arguments)
                ):
                    raise LlmConfigurationError("工具调用历史格式无效")
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    }
                )
                continue
            if item_type == "function_call_output":
                call_id = item.get("call_id")
                output = item.get("output")
                if not all(
                    isinstance(value, str) and value
                    for value in (call_id, output)
                ):
                    raise LlmConfigurationError("工具结果历史格式无效")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output,
                    }
                )
                continue
            role = item.get("role")
            content = item.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(
                content, str
            ):
                raise LlmConfigurationError("Chat Completions 历史项格式无效")
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": request.input_text.strip()})
        return messages

    @staticmethod
    def _build_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": _thaw_json(tool.input_schema),
                    "strict": True,
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _collect_tool_fragments(
        delta: Any,
        tool_parts: dict[int, dict[str, str]],
    ) -> None:
        for fragment in getattr(delta, "tool_calls", ()) or ():
            index = getattr(fragment, "index", None)
            if not isinstance(index, int) or index < 0:
                raise LlmProtocolError("工具调用分片缺少有效索引")
            parts = tool_parts.setdefault(
                index,
                {"id": "", "name": "", "arguments": ""},
            )
            call_id = getattr(fragment, "id", None)
            if isinstance(call_id, str) and call_id:
                if parts["id"] and parts["id"] != call_id:
                    raise LlmProtocolError("工具调用分片标识不一致")
                parts["id"] = call_id
            function = getattr(fragment, "function", None)
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", None)
            if isinstance(name, str):
                parts["name"] += name
            if isinstance(arguments, str):
                parts["arguments"] += arguments


class OpenAICompatibleProvider(OpenAIResponsesProvider):
    """通过自定义 base URL 使用 Responses 协议的 Provider"""

    def __init__(self, *, base_url: str, api_key: str, **kwargs: Any) -> None:
        parsed = urlparse(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in local_hosts
        ):
            raise LlmConfigurationError("兼容服务必须使用 HTTPS 或本机 HTTP")
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)


class MockProvider:
    """用于测试和离线演示的确定性 Provider"""

    def __init__(self, events: Sequence[LlmStreamEvent]) -> None:
        self._events = tuple(events)

    async def stream(
        self,
        request: LlmRequest,
        token: CancellationToken,
    ) -> AsyncIterator[LlmStreamEvent]:
        del request
        for event in self._events:
            token.throw_if_cancelled()
            await asyncio.sleep(0)
            token.throw_if_cancelled()
            yield event
