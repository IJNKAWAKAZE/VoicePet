import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.cancellation import CancellationSource, CancelledError
from core.llm import LlmCompleted, LlmProtocolError, LlmTextDelta, LlmToolCall
from core.session_archive import SessionTurnRecord

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def turn(text="我喜欢猫", *, session_id=None):
    return SessionTurnRecord(str(uuid4()), str(uuid4()), session_id or str(uuid4()),
                             text, "知道了", NOW.date(), NOW, NOW + timedelta(days=7))


def fact(source, **overrides):
    data = {"source_turn_id": source.turn_id, "category": "preference", "content": "用户喜欢猫",
            "fact_key": "user.pet", "value": "猫", "quote": "我喜欢猫", "confidence": .9,
            "stability": "stable", "sensitivity": "normal", "explicit_update": False,
            "keywords": ["宠物"]}
    return {**data, **overrides}


class Provider:
    def __init__(self, events, *, cancel=None):
        self.events = events
        self.request = None
        self.cancel = cancel

    async def stream(self, request, token):
        self.request = request
        for event in self.events:
            yield event
        if self.cancel:
            self.cancel.cancel("synthetic cancellation")


def response(facts=(), summary=None):
    return [LlmToolCall("analysis", "submit_memory_analysis", {"facts": list(facts), "summary": summary}),
            LlmCompleted("test", 1, 1)]


def test_extractor_has_only_bounded_submission_tool_and_real_user_evidence():
    async def scenario():
        from core.memory_extraction import MemoryExtractor

        source = turn()
        provider = Provider(response([fact(source)]))
        result = await MemoryExtractor(provider).extract((source,), include_summary=False,
                                                         existing_facts=(), token=CancellationSource().token)
        assert result.source_turn_ids == (source.turn_id,)
        assert result.facts[0][1].user_text == source.user_text
        assert result.facts[0][1].session_id == source.session_id
        assert result.facts[0][0].value == "猫"
        assert provider.request.history == ()
        assert [tool.name for tool in provider.request.tools] == ["submit_memory_analysis"]
        assert provider.request.max_output_tokens == 2048
        assert len(provider.request.input_text) <= 12000
        json.loads(provider.request.input_text)

    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"source_turn_id": str(uuid4())}, {"quote": "助手说用户喜欢猫"}, {"value": "狗"},
    {"confidence": float("nan")}, {"confidence": True}, {"keywords": ["x"] * 9},
    {"keywords": ["我的密码是 synthetic-secret"]}, {"extra": "not allowed"},
    {"explicit_update": 1}, {"stability": "forever"},
])
def test_extractor_rejects_entire_malformed_proposal(overrides):
    async def scenario():
        from core.memory_extraction import MemoryExtractor

        source = turn()
        provider = Provider(response([fact(source), fact(source, **overrides)]))
        with pytest.raises(LlmProtocolError):
            await MemoryExtractor(provider).extract((source,), include_summary=False,
                                                    existing_facts=(), token=CancellationSource().token)

    asyncio.run(scenario())


@pytest.mark.parametrize("events", [
    [LlmTextDelta('{"facts":[],"summary":null}'), LlmCompleted("test", 1, 1)],
    [LlmCompleted("test", 1, 1)],
    [LlmToolCall("x", "run_command", {"command": "do not execute"}), LlmCompleted("test", 1, 1)],
    response()[:-1], response() + response(),
])
def test_extractor_requires_exactly_one_submission_and_complete_response(events):
    async def scenario():
        from core.memory_extraction import MemoryExtractor

        with pytest.raises(LlmProtocolError):
            await MemoryExtractor(Provider(events)).extract((turn(),), include_summary=False,
                                                            existing_facts=(), token=CancellationSource().token)

    asyncio.run(scenario())


def test_extractor_late_cancellation_never_returns_result():
    async def scenario():
        from core.memory_extraction import MemoryExtractor

        cancellation = CancellationSource()
        with pytest.raises(CancelledError):
            await MemoryExtractor(Provider(response(), cancel=cancellation)).extract(
                (turn(),), include_summary=False, existing_facts=(), token=cancellation.token)

    asyncio.run(scenario())


def test_extractor_budget_covers_only_complete_included_turns():
    async def scenario():
        from core.memory_extraction import MemoryExtractor

        first = turn("完整消息" * 1200)
        second = turn("下一条完整消息" * 1200, session_id=first.session_id)
        provider = Provider(response())
        result = await MemoryExtractor(provider).extract((first, second), include_summary=False,
                                                         existing_facts=(), token=CancellationSource().token)
        assert result.source_turn_ids == (first.turn_id,)
        payload = json.loads(provider.request.input_text)
        assert payload["turns"][0]["user_text"] == first.user_text
        assert len(provider.request.input_text) <= 12000

    asyncio.run(scenario())


def test_extractor_reports_one_oversized_turn_without_sending_model_request():
    async def scenario():
        from core.memory_extraction import MemoryExtractor, MemoryInputTooLargeError

        source = replace(turn(), assistant_text="x" * 13000)
        provider = Provider(response())
        with pytest.raises(MemoryInputTooLargeError) as raised:
            await MemoryExtractor(provider).extract((source,), include_summary=False,
                                                    existing_facts=(), token=CancellationSource().token)
        assert raised.value.turn_id == source.turn_id
        assert provider.request is None

    asyncio.run(scenario())


def test_extractor_parses_decisions_only_when_summary_requested():
    async def scenario():
        from core.memory_extraction import MemoryExtractor

        summary = {"topic": "发布计划", "decisions": ["采用 QML"], "unfinished_items": ["测试"]}
        provider = Provider(response(summary=summary))
        extractor = MemoryExtractor(provider)
        result = await extractor.extract((turn(),), include_summary=True, existing_facts=(),
                                         token=CancellationSource().token)
        assert result.summary.decisions == ("采用 QML",)
        with pytest.raises(LlmProtocolError):
            await extractor.extract((turn(),), include_summary=False, existing_facts=(),
                                     token=CancellationSource().token)

    asyncio.run(scenario())
