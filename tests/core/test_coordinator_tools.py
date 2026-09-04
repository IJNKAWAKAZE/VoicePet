import asyncio
import threading

import pytest

from core.audit import AuditContext
from core.cancellation import CancellationToken
from core.coordinator import Coordinator
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    ErrorSeverity,
    LlmUsageRecorded,
    MemoryResultReady,
    RuntimeErrorEvent,
    StateChanged,
    ToolResultReady,
)
from core.llm import LlmCompleted, LlmTextDelta, LlmToolCall, ToolDefinition
from core.memory import MemoryStatus, MemoryStore
from core.memory_candidates import MemoryCandidateService
from core.policy import (
    AuthorizationIssuer,
    ConcurrencyPolicy,
    ConfirmationMode,
    PolicyEngine,
    PolicySettings,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext
from core.short_term_summary import (
    SHORT_TERM_SUMMARY_TOOL_NAME,
    ShortTermSummaryService,
    short_term_summary_tool_definition,
)
from core.tool_types import ToolExecutionResult, ToolExecutionStatus


async def wait_until(predicate, attempts=200):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class Audio:
    async def record_until_silence(self, token: CancellationToken):
        return b"audio"


class Transcript:
    async def transcribe(self, audio, token):
        return "do tool"


class ToolLlm:
    def __init__(self, name="test_tool"):
        self.name = name
        self.requests = []

    async def stream(self, request, token):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield LlmToolCall("call-1", self.name, {"value": "x"})
            yield LlmCompleted("response-1", 6, 4)
            return
        yield LlmTextDelta("操作已经完成")
        yield LlmCompleted("response-2", 10, 4)


class LoopingToolLlm:
    def __init__(self):
        self.calls = 0

    async def stream(self, request, token):
        del request, token
        self.calls += 1
        yield LlmToolCall("call-loop", "test_tool", {"value": "x"})
        yield LlmCompleted(f"response-{self.calls}", 1, 1)


class CandidateLlm:
    def __init__(self, content="用户偏好低音量"):
        self.content = content
        self.requests = []

    async def stream(self, request, token):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield LlmToolCall(
                "memory-1",
                "propose_memory",
                {
                    "category": "preference",
                    "content": self.content,
                    "confidence": 0.8,
                },
            )
            yield LlmCompleted("response-memory-tool", 6, 4)
            return
        yield LlmTextDelta("我记下了一个待你确认的偏好")
        yield LlmCompleted("response-memory", 10, 6)


class SummaryLlm:
    def __init__(self):
        self.requests = []

    async def stream(self, request, token):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield LlmToolCall(
                "summary-1",
                "update_daily_summary",
                {
                    "topic": "工具执行",
                    "unfinished_items": ["查看结果"],
                },
            )
            yield LlmCompleted("response-summary-tool", 6, 4)
            return
        yield LlmTextDelta("最终回复")
        yield LlmCompleted("response-summary", 10, 4)


class DelayedCompletionToolLlm:
    def __init__(self):
        self.requests = []
        self.proposal_emitted = asyncio.Event()
        self.release_completion = asyncio.Event()

    async def stream(self, request, token):
        del token
        self.requests.append(request)
        if len(self.requests) == 1:
            self.proposal_emitted.set()
            yield LlmToolCall("call-1", "test_tool", {"value": "x"})
            await self.release_completion.wait()
            yield LlmCompleted("response-1", 6, 4)
            return
        yield LlmTextDelta("操作已经完成")
        yield LlmCompleted("response-2", 5, 5)


class BudgetedToolLlm:
    def __init__(self, clock):
        self._clock = clock
        self.requests = []

    async def stream(self, request, token):
        del token
        self.requests.append(request)
        if len(self.requests) == 1:
            yield LlmToolCall("call-1", "test_tool", {"value": "x"})
            self._clock[0] += 0.125
            yield LlmCompleted("response-1", 6, 4)
            return
        yield LlmTextDelta("操作已经完成")
        self._clock[0] += 0.05
        yield LlmCompleted("response-2", 5, 5)


class TokenExhaustingToolLlm:
    async def stream(self, request, token):
        del request, token
        yield LlmToolCall("call-1", "test_tool", {"value": "x"})
        yield LlmCompleted("response-1", 8, 3)


class TokenExhaustingReplyLlm:
    async def stream(self, request, token):
        del request, token
        yield LlmTextDelta("预算内已经生成的回复")
        yield LlmCompleted("response-1", 8, 3)


class ResettingBudgetLlm:
    def __init__(self):
        self.requests = []
        self.first_turn_held = asyncio.Event()

    async def stream(self, request, token):
        del token
        self.requests.append(request)
        request_number = len(self.requests)
        yield LlmTextDelta(f"回复 {request_number}")
        yield LlmCompleted(f"response-{request_number}", 6, 4)
        if request_number == 1:
            await self.first_turn_held.wait()


class MultipleToolCallLlm:
    async def stream(self, request, token):
        del request, token
        yield LlmToolCall("call-1", "test_tool", {"value": "x"})
        yield LlmToolCall("call-2", "test_tool", {"value": "y"})
        yield LlmCompleted("response-1", 6, 4)


class Executor:
    def __init__(self, *, wait=False, error=None):
        self.wait = wait
        self.error = error
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        proposal,
        decision,
        authorization,
        token,
        *,
        audit_context: AuditContext,
    ):
        self.calls.append(
            (proposal, decision, authorization, token, audit_context)
        )
        self.started.set()
        if self.wait:
            await self.release.wait()
        if self.error:
            raise self.error
        return ToolExecutionResult(
            ToolExecutionStatus.SUCCESS,
            {"ok": True},
            "执行成功",
        )


def tool_manifest(risk):
    return ToolManifest(
        name="test_tool",
        description="执行测试工具",
        input_schema={
            "type": "object",
            "maxProperties": 1,
            "properties": {"value": {"type": "string", "maxLength": 8}},
            "required": ["value"],
            "additionalProperties": False,
        },
        base_risk=risk,
        timeout=2,
        concurrency_policy=ConcurrencyPolicy.SERIAL,
    )


def build(
    risk,
    executor,
    *,
    llm_name="test_tool",
    llm_tools=(),
    llm=None,
    memory_context=None,
    session_context=None,
    memory_candidates=None,
    session_archive=None,
    short_term_summaries=None,
    coordinator_settings=None,
):
    bus = EventBus()
    registry = ToolRegistry([tool_manifest(risk)])
    llm = llm or ToolLlm(llm_name)
    coordinator = Coordinator(
        Audio(),
        Transcript(),
        bus,
        llm_provider=llm,
        llm_tools=llm_tools,
        memory_context=memory_context,
        session_context=session_context,
        memory_candidates=memory_candidates,
        session_archive=session_archive,
        short_term_summaries=short_term_summaries,
        policy_engine=PolicyEngine(registry, PolicySettings()),
        authorization_issuer=AuthorizationIssuer(b"k" * 32),
        tool_executor=executor,
        **(coordinator_settings or {}),
    )
    return coordinator, bus, llm


def test_coordinator_forwards_local_tool_definitions_to_llm_request():
    async def scenario():
        executor = Executor()
        manifest = tool_manifest(RiskLevel.R0)
        definition = ToolDefinition(
            manifest.name,
            manifest.description,
            manifest.input_schema,
        )
        coordinator, _, llm = build(
            RiskLevel.R0,
            executor,
            llm_tools=(definition,),
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(llm.requests) >= 1)

        assert llm.requests[0].tools == (definition,)
        await coordinator.stop()

    asyncio.run(scenario())


def test_tool_waits_for_completed_usage_before_execution():
    async def scenario():
        llm = DelayedCompletionToolLlm()
        executor = Executor()
        coordinator, _, _ = build(
            RiskLevel.R0,
            executor,
            llm=llm,
        )

        await coordinator.start_listening()
        await llm.proposal_emitted.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert executor.calls == []

        llm.release_completion.set()
        await wait_until(lambda: len(executor.calls) == 1)
        await coordinator.stop()

    asyncio.run(scenario())


def test_coordinator_records_usage_and_clamps_follow_up_output_budget():
    async def scenario():
        now = [10.0]
        llm = BudgetedToolLlm(now)
        coordinator, bus, _ = build(
            RiskLevel.R0,
            Executor(),
            llm=llm,
            coordinator_settings={
                "llm_model": "gpt-budget-test",
                "max_turn_tokens": 20,
                "clock": lambda: now[0],
            },
        )
        usage = []
        bus.subscribe(LlmUsageRecorded, usage.append)

        await coordinator.start_listening()
        await wait_until(lambda: coordinator.response_text == "操作已经完成")

        assert [request.max_output_tokens for request in llm.requests] == [20, 10]
        assert [event.model for event in usage] == [
            "gpt-budget-test",
            "gpt-budget-test",
        ]
        assert [event.duration_ms for event in usage] == [125, 50]
        assert [event.input_tokens for event in usage] == [6, 5]
        assert [event.output_tokens for event in usage] == [4, 5]
        assert [event.turn_total_tokens for event in usage] == [10, 20]
        await coordinator.stop()

    asyncio.run(scenario())


def test_token_exhaustion_prevents_proposed_tool_execution():
    async def scenario():
        executor = Executor()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            executor,
            llm=TokenExhaustingToolLlm(),
            coordinator_settings={"max_turn_tokens": 10},
        )
        errors = []
        bus.subscribe(RuntimeErrorEvent, errors.append)

        await coordinator.start_listening()
        await wait_until(lambda: bool(errors))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert executor.calls == []
        assert errors[-1].error_code == "runtime.budget"
        assert errors[-1].user_action_required is True
        await coordinator.stop()

    asyncio.run(scenario())


def test_token_exhaustion_preserves_reply_without_starting_archive(tmp_path):
    async def scenario():
        archive = SessionArchiveStore(tmp_path / "assistant.db")
        coordinator, bus, _ = build(
            RiskLevel.R0,
            Executor(),
            llm=TokenExhaustingReplyLlm(),
            session_archive=archive,
            coordinator_settings={"max_turn_tokens": 10},
        )
        usage = []
        errors = []
        bus.subscribe(LlmUsageRecorded, usage.append)
        bus.subscribe(RuntimeErrorEvent, errors.append)

        await coordinator.start_listening()
        await wait_until(lambda: len(usage) == 1)
        await wait_until(lambda: bool(errors) or bool(archive.list_turns()))

        assert coordinator.response_text == "预算内已经生成的回复"
        assert archive.list_turns() == ()
        assert errors[-1].error_code == "runtime.budget"
        assert coordinator.phase is ConversationPhase.IDLE
        await coordinator.stop()
        archive.close()

    asyncio.run(scenario())


def test_interrupt_starts_fresh_duration_and_token_budget():
    async def scenario():
        now = [10.0]
        llm = ResettingBudgetLlm()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            Executor(),
            llm=llm,
            coordinator_settings={
                "max_turn_duration": 5.0,
                "max_turn_tokens": 20,
                "clock": lambda: now[0],
            },
        )
        usage = []
        bus.subscribe(LlmUsageRecorded, usage.append)

        first_turn = await coordinator.start_listening()
        await wait_until(lambda: len(usage) == 1)
        now[0] = 20.0
        second_turn = await coordinator.interrupt()
        await wait_until(lambda: len(usage) == 2)

        assert first_turn != second_turn
        assert [request.max_output_tokens for request in llm.requests] == [20, 20]
        assert [event.turn_total_tokens for event in usage] == [10, 10]
        assert [event.turn_id for event in usage] == [first_turn, second_turn]
        await coordinator.stop()

    asyncio.run(scenario())


def test_multiple_tool_proposals_are_rejected_as_protocol_error():
    async def scenario():
        executor = Executor()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            executor,
            llm=MultipleToolCallLlm(),
        )
        errors = []
        usage = []
        bus.subscribe(RuntimeErrorEvent, errors.append)
        bus.subscribe(LlmUsageRecorded, usage.append)

        await coordinator.start_listening()
        await wait_until(lambda: bool(errors))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert executor.calls == []
        assert errors[-1].error_code == "llm.protocol"
        assert [(event.input_tokens, event.output_tokens) for event in usage] == [
            (6, 4)
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_coordinator_injects_session_before_assembled_memory_history():
    class Context:
        def build_history(self, text):
            assert text == "do tool"
            return ({"role": "developer", "content": "bounded memory"},)

    async def scenario():
        session = SessionContext()
        session.add_turn("上一问", "上一答")
        coordinator, _, llm = build(
            RiskLevel.R0,
            Executor(),
            memory_context=Context(),
            session_context=session,
        )
        await coordinator.start_listening()
        await wait_until(lambda: len(llm.requests) >= 1)
        assert llm.requests[0].history == (
            {"role": "user", "content": "上一问"},
            {"role": "assistant", "content": "上一答"},
            {"role": "developer", "content": "bounded memory"},
        )
        await coordinator.stop()

    asyncio.run(scenario())


def test_r0_tool_executes_automatically_with_distinct_operation_context():
    async def scenario():
        executor = Executor()
        coordinator, bus, llm = build(RiskLevel.R0, executor)
        states = []
        results = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(ToolResultReady, results.append)

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(results) == 1)

        assert executor.calls[0][2] is None
        context = executor.calls[0][4]
        assert context.turn_id == str(turn_id)
        assert context.tool_call_id == "call-1"
        executing = next(
            event for event in states
            if event.current is ConversationPhase.EXECUTING_TOOL
        )
        assert context.correlation_id == str(executing.correlation_id)
        assert results[0].status == "success"
        await wait_until(lambda: coordinator.response_text == "操作已经完成")
        assert len(llm.requests) == 2
        output = next(
            item for item in llm.requests[1].history
            if item.get("type") == "function_call_output"
        )
        assert output["call_id"] == "call-1"
        assert '"status":"success"' in output["output"]
        assert coordinator.phase is ConversationPhase.IDLE
        await coordinator.stop()

    asyncio.run(scenario())


def test_tool_final_reply_is_archived_once_for_the_original_user_input():
    async def scenario():
        session = SessionContext()
        coordinator, _, llm = build(
            RiskLevel.R0,
            Executor(),
            session_context=session,
        )

        await coordinator.start_listening()
        await wait_until(lambda: coordinator.response_text == "操作已经完成")

        assert len(llm.requests) == 2
        assert session.build_history() == (
            {"role": "user", "content": "do tool"},
            {"role": "assistant", "content": "操作已经完成"},
        )
        await coordinator.stop()

    asyncio.run(scenario())


def test_r2_tool_waits_for_approval_then_executes_once():
    async def scenario():
        executor = Executor()
        coordinator, bus, _ = build(RiskLevel.R2, executor)
        approvals = []
        bus.subscribe(ApprovalRequested, approvals.append)

        await coordinator.start_listening()
        await wait_until(
            lambda: coordinator.phase is ConversationPhase.AWAITING_APPROVAL
        )
        assert len(approvals) == 1
        assert executor.calls == []

        await coordinator.approve_pending(ConfirmationMode.VOICE)
        await wait_until(lambda: len(executor.calls) == 1)
        assert isinstance(executor.calls[0][2], str)
        with pytest.raises(RuntimeError, match="待确认"):
            await coordinator.approve_pending(ConfirmationMode.VOICE)
        await coordinator.stop()

    asyncio.run(scenario())


def test_expired_turn_rejects_approved_tool_before_side_effect_starts():
    async def scenario():
        executor = Executor()
        coordinator, bus, _ = build(
            RiskLevel.R2,
            executor,
            coordinator_settings={"max_turn_duration": 0.02},
        )
        errors = []
        bus.subscribe(RuntimeErrorEvent, errors.append)

        await coordinator.start_listening()
        await wait_until(
            lambda: coordinator.phase is ConversationPhase.AWAITING_APPROVAL
        )
        await asyncio.sleep(0.03)
        await coordinator.approve_pending(ConfirmationMode.VOICE)
        await wait_until(lambda: bool(errors))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert executor.calls == []
        assert errors[-1].error_code == "runtime.budget"
        await coordinator.stop()

    asyncio.run(scenario())


def test_budget_expiry_during_executing_event_rejects_tool_start():
    async def scenario():
        current = [0.0]
        executor = Executor()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            executor,
            coordinator_settings={
                "max_turn_duration": 0.5,
                "clock": lambda: current[0],
            },
        )
        errors = []

        async def expire_on_executing(event):
            if event.current is ConversationPhase.EXECUTING_TOOL:
                current[0] = 1.0

        bus.subscribe(StateChanged, expire_on_executing)
        bus.subscribe(RuntimeErrorEvent, errors.append)

        await coordinator.start_listening()
        await wait_until(lambda: bool(errors))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert executor.calls == []
        assert errors[-1].error_code == "runtime.budget"
        await coordinator.stop()

    asyncio.run(scenario())


def test_wrong_confirmation_and_explicit_rejection_never_execute():
    async def scenario():
        executor = Executor()
        coordinator, bus, _ = build(RiskLevel.R3, executor)
        results = []
        bus.subscribe(ToolResultReady, results.append)
        await coordinator.start_listening()
        await wait_until(
            lambda: coordinator.phase is ConversationPhase.AWAITING_APPROVAL
        )
        with pytest.raises(RuntimeError, match="确认方式"):
            await coordinator.approve_pending(ConfirmationMode.VOICE)
        await coordinator.reject_pending()
        assert executor.calls == []
        assert results[-1].status == "denied"
        assert coordinator.phase is ConversationPhase.RECOVERING
        await coordinator.stop()

    asyncio.run(scenario())


def test_policy_denial_enters_recovery_and_worker_error_returns_idle():
    async def denied_scenario():
        executor = Executor()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            executor,
            llm_name="unknown_tool",
        )
        results = []
        bus.subscribe(ToolResultReady, results.append)
        await coordinator.start_listening()
        await wait_until(lambda: coordinator.phase is ConversationPhase.RECOVERING)
        assert results[-1].status == "denied"
        assert executor.calls == []
        await coordinator.stop()

    async def error_scenario():
        executor = Executor(error=RuntimeError("worker disconnected"))
        coordinator, bus, _ = build(RiskLevel.R0, executor)
        errors = []
        bus.subscribe(RuntimeErrorEvent, errors.append)
        await coordinator.start_listening()
        await wait_until(lambda: bool(errors))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
        assert errors[-1].code == "tool.execution"
        assert errors[-1].component == "tool"
        assert errors[-1].severity is ErrorSeverity.ERROR
        assert errors[-1].retryable is False
        assert errors[-1].user_action_required is True
        assert errors[-1].safe_message == (
            "工具执行状态未知，请先查看最近操作记录"
        )
        assert errors[-1].diagnostic_context == {
            "exception_type": "RuntimeError"
        }
        assert "worker disconnected" not in errors[-1].message
        await coordinator.stop()

    asyncio.run(denied_scenario())
    asyncio.run(error_scenario())


def test_interrupt_discards_late_tool_result():
    async def scenario():
        executor = Executor(wait=True)
        coordinator, bus, _ = build(RiskLevel.R0, executor)
        results = []
        bus.subscribe(ToolResultReady, results.append)
        old_turn = await coordinator.start_listening()
        await executor.started.wait()
        new_turn = await coordinator.interrupt()
        executor.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert old_turn != new_turn
        assert all(result.turn_id != old_turn for result in results)
        await coordinator.stop()

    asyncio.run(scenario())


def test_expired_turn_allows_started_tool_to_finish_without_llm_continuation():
    async def scenario():
        executor = Executor(wait=True)
        coordinator, bus, llm = build(
            RiskLevel.R0,
            executor,
            coordinator_settings={"max_turn_duration": 0.02},
        )
        errors = []
        bus.subscribe(RuntimeErrorEvent, errors.append)

        await coordinator.start_listening()
        await executor.started.wait()
        await asyncio.sleep(0.03)

        assert len(executor.calls) == 1
        assert executor.calls[0][3].is_cancelled is False
        executor.release.set()
        await wait_until(lambda: bool(errors))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert len(llm.requests) == 1
        assert errors[-1].error_code == "runtime.budget"
        await coordinator.stop()

    asyncio.run(scenario())


def test_coordinator_limits_tool_calls_per_turn():
    async def scenario():
        executor = Executor()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            executor,
            llm=LoopingToolLlm(),
        )
        results = []
        errors = []
        bus.subscribe(ToolResultReady, results.append)
        bus.subscribe(RuntimeErrorEvent, errors.append)

        await coordinator.start_listening()
        await wait_until(lambda: bool(errors))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert len(executor.calls) == 4
        assert all(result.status == "success" for result in results)
        assert errors[-1].error_code == "runtime.budget"
        assert errors[-1].user_action_required is True
        await coordinator.stop()

    asyncio.run(scenario())


def test_internal_memory_candidate_never_reaches_tool_worker(tmp_path):
    async def scenario():
        store = MemoryStore(tmp_path / "assistant.db")
        executor = Executor()
        llm = CandidateLlm()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            executor,
            llm=llm,
            memory_candidates=MemoryCandidateService(store),
        )
        memory_results = []
        candidate_ready = asyncio.Event()

        def record_memory_result(event):
            memory_results.append(event)
            candidate_ready.set()

        bus.subscribe(MemoryResultReady, record_memory_result)

        await coordinator.start_listening()
        await asyncio.wait_for(candidate_ready.wait(), timeout=1)
        await wait_until(
            lambda: coordinator.response_text == "我记下了一个待你确认的偏好"
        )

        records = store.list_all()
        assert len(records) == 1
        assert records[0].status is MemoryStatus.CANDIDATE
        assert executor.calls == []
        assert memory_results[-1].status == "candidate"
        assert "低音量" not in memory_results[-1].message
        output = next(
            item
            for item in llm.requests[1].history
            if item.get("type") == "function_call_output"
        )
        assert "低音量" not in output["output"]
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())


def test_sensitive_memory_candidate_is_rejected_and_llm_still_finishes(tmp_path):
    async def scenario():
        store = MemoryStore(tmp_path / "assistant.db")
        executor = Executor()
        coordinator, bus, _ = build(
            RiskLevel.R0,
            executor,
            llm=CandidateLlm("password=secret123"),
            memory_candidates=MemoryCandidateService(store),
        )
        memory_results = []
        candidate_ready = asyncio.Event()

        def record_memory_result(event):
            memory_results.append(event)
            candidate_ready.set()

        bus.subscribe(MemoryResultReady, record_memory_result)

        await coordinator.start_listening()
        await asyncio.wait_for(candidate_ready.wait(), timeout=1)
        await wait_until(
            lambda: coordinator.response_text == "我记下了一个待你确认的偏好"
        )

        assert store.list_all() == ()
        assert executor.calls == []
        assert memory_results[-1].status == "denied"
        assert "secret123" not in memory_results[-1].message
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())


def test_interrupt_rolls_back_candidate_created_by_stale_turn(tmp_path):
    class DelayedCandidateService:
        def __init__(self, store):
            self._service = MemoryCandidateService(store)
            self.started = threading.Event()
            self.release = threading.Event()
            self.completed = threading.Event()
            self.discarded = threading.Event()

        def create(self, arguments, source_turn_id):
            self.started.set()
            self.release.wait()
            record = self._service.create(arguments, source_turn_id)
            self.completed.set()
            return record

        def discard(self, memory_id):
            result = self._service.discard(memory_id)
            self.discarded.set()
            return result

    async def scenario():
        store = MemoryStore(tmp_path / "assistant.db")
        candidates = DelayedCandidateService(store)
        coordinator, _, _ = build(
            RiskLevel.R0,
            Executor(),
            llm=CandidateLlm(),
            memory_candidates=candidates,
        )

        old_turn = await coordinator.start_listening()
        await asyncio.to_thread(candidates.started.wait)
        new_turn = await coordinator.interrupt()
        candidates.release.set()
        await asyncio.to_thread(candidates.completed.wait)
        await asyncio.wait_for(
            asyncio.to_thread(candidates.discarded.wait),
            timeout=1,
        )

        assert old_turn != new_turn
        assert store.list_all() == ()
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())


def test_internal_summary_is_saved_only_after_final_reply_and_never_uses_worker(
    tmp_path,
):
    async def scenario():
        archive = SessionArchiveStore(tmp_path / "assistant.db")
        executor = Executor()
        llm = SummaryLlm()
        coordinator, _, _ = build(
            RiskLevel.R0,
            executor,
            llm=llm,
            llm_tools=(short_term_summary_tool_definition(),),
            session_archive=archive,
            short_term_summaries=ShortTermSummaryService(archive),
        )

        turn_id = await coordinator.start_listening()
        for _ in range(100):
            if archive.list_summaries():
                break
            await asyncio.sleep(0.01)

        assert coordinator.response_text == "最终回复"
        assert executor.calls == []
        assert [record.turn_id for record in archive.list_turns()] == [
            str(turn_id)
        ]
        summary = archive.list_summaries()[0]
        assert summary.topic == "工具执行"
        assert summary.unfinished_items == ("查看结果",)
        assert SHORT_TERM_SUMMARY_TOOL_NAME not in {
            tool.name for tool in llm.requests[1].tools
        }
        await coordinator.stop()
        archive.close()

    asyncio.run(scenario())


def test_archive_expiry_prevents_pending_summary_save(tmp_path):
    class ExpiringArchive:
        def __init__(self, archive, clock):
            self._archive = archive
            self._clock = clock

        def archive_turn(self, turn_id, user_text, assistant_text):
            result = self._archive.archive_turn(
                turn_id,
                user_text,
                assistant_text,
            )
            self._clock[0] = 1.0
            return result

        def discard_turn(self, turn_id):
            return self._archive.discard_turn(turn_id)

    async def scenario():
        current = [0.0]
        archive = SessionArchiveStore(tmp_path / "assistant.db")
        coordinator, bus, _ = build(
            RiskLevel.R0,
            Executor(),
            llm=SummaryLlm(),
            session_archive=ExpiringArchive(archive, current),
            short_term_summaries=ShortTermSummaryService(archive),
            coordinator_settings={
                "max_turn_duration": 0.5,
                "clock": lambda: current[0],
            },
        )
        errors = []
        bus.subscribe(RuntimeErrorEvent, errors.append)

        await coordinator.start_listening()
        await wait_until(lambda: bool(errors) or bool(archive.list_summaries()))

        assert len(archive.list_turns()) == 1
        assert archive.list_summaries() == ()
        assert errors[-1].error_code == "runtime.budget"
        assert coordinator.phase is ConversationPhase.IDLE
        await coordinator.stop()
        archive.close()

    asyncio.run(scenario())


def test_expired_started_summary_save_finishes_then_recovers(tmp_path):
    class ExpiringSummary:
        def __init__(self, archive, clock):
            self._service = ShortTermSummaryService(archive)
            self._clock = clock

        def prepare(self, arguments):
            return self._service.prepare(arguments)

        def save(self, source_turn_id, draft):
            result = self._service.save(source_turn_id, draft)
            self._clock[0] = 1.0
            return result

    async def scenario():
        current = [0.0]
        archive = SessionArchiveStore(tmp_path / "assistant.db")
        coordinator, bus, _ = build(
            RiskLevel.R0,
            Executor(),
            llm=SummaryLlm(),
            session_archive=archive,
            short_term_summaries=ExpiringSummary(archive, current),
            coordinator_settings={
                "max_turn_duration": 0.5,
                "clock": lambda: current[0],
            },
        )
        errors = []
        budget_error = asyncio.Event()

        def record_error(event):
            errors.append(event)
            budget_error.set()

        bus.subscribe(RuntimeErrorEvent, record_error)

        await coordinator.start_listening()
        await asyncio.wait_for(budget_error.wait(), timeout=1)

        assert len(archive.list_summaries()) == 1
        assert errors[-1].error_code == "runtime.budget"
        assert coordinator.phase is ConversationPhase.IDLE
        await coordinator.stop()
        archive.close()

    asyncio.run(scenario())


def test_interrupt_during_summary_save_rolls_back_stale_session_data(tmp_path):
    class DelayedSummaryService:
        def __init__(self, archive):
            self._service = ShortTermSummaryService(archive)
            self.started = threading.Event()
            self.release = threading.Event()
            self.completed = threading.Event()

        def prepare(self, arguments):
            return self._service.prepare(arguments)

        def save(self, source_turn_id, draft):
            self.started.set()
            self.release.wait()
            result = self._service.save(source_turn_id, draft)
            self.completed.set()
            return result

    async def scenario():
        archive = SessionArchiveStore(tmp_path / "assistant.db")
        summaries = DelayedSummaryService(archive)
        coordinator, _, _ = build(
            RiskLevel.R0,
            Executor(),
            llm=SummaryLlm(),
            session_archive=archive,
            short_term_summaries=summaries,
        )

        old_turn = await coordinator.start_listening()
        await asyncio.to_thread(summaries.started.wait)
        new_turn = await coordinator.interrupt()
        summaries.release.set()
        await asyncio.to_thread(summaries.completed.wait)
        for _ in range(100):
            if all(record.turn_id != str(old_turn) for record in archive.list_turns()):
                break
            await asyncio.sleep(0.01)

        assert old_turn != new_turn
        assert all(record.turn_id != str(old_turn) for record in archive.list_turns())
        assert all(
            record.source_turn_id != str(old_turn)
            for record in archive.list_summaries()
        )
        await coordinator.stop()
        archive.close()

    asyncio.run(scenario())
