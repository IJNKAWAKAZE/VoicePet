"""对话轮次编排与过期结果隔离"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from .agent_types import AgentEventType
from .asr import AsrError
from .audio_types import AudioError
from .audit import AuditContext
from .builtin_tools import ReadFileTool
from .cancellation import CancellationSource, CancellationToken, CancelledError
from .config import normalize_wake_keyword
from .event_bus import EventBus
from .events import (
    AgentApprovalRequested,
    AgentProgress,
    ApprovalRequested,
    ConversationPhase,
    CorrelationId,
    LlmUsageRecorded,
    MemoryResultReady,
    RecordingStarted,
    SpeakRequested,
    TextDelta,
    TextInputSubmitted,
    ToolResultReady,
    TranscriptReady,
    TurnId,
    WakeCommandPending,
    validate_llm_model_name,
)
from .llm import (
    LlmAttachment,
    LlmCompleted,
    LlmError,
    LlmProtocolError,
    LlmProvider,
    LlmRequest,
    LlmTextDelta,
    LlmToolCall,
    ToolDefinition,
    can_inline_text_attachment,
)
from .memory import MemoryConfigurationError
from .memory_candidates import MEMORY_CANDIDATE_TOOL_NAME, MemoryCandidateService
from .memory_intents import MemoryOperation, MemoryOperationService
from .policy import (
    AuthorizationIssuer,
    ConfirmationMode,
    PolicyDecision,
    PolicyEngine,
    ToolProposal,
    canonical_json,
)
from .runtime_errors import runtime_error_event
from .short_term_summary import (
    SHORT_TERM_SUMMARY_TOOL_NAME,
    ShortTermSummaryDraft,
    ShortTermSummaryService,
)
from .state_machine import ConversationStateMachine
from .tool_types import ToolExecutionResult, ToolExecutionStatus
from .tts import (
    AudioPlayer,
    SpeechSynthesizer,
    SynthesizedAudio,
    TtsError,
    markdown_to_speech_text,
    split_speech_text,
)
from .turn_budget import TurnBudget, TurnBudgetExceededError, TurnBudgetLimits
from .wake import prepare_transcript_for_source

ResultT = TypeVar("ResultT")
_ACTIVATION_SOURCES = frozenset(
    {"click", "hotkey", "shortcut", "wake_word", "wake_followup"}
)
_WAKE_ACKNOWLEDGEMENT = "我在，请说"


class AudioSession(Protocol):
    """录音适配器边界"""

    async def record_until_silence(
        self,
        token: CancellationToken,
        *,
        include_preroll: bool = True,
    ) -> bytes: ...


class TranscriptAdapter(Protocol):
    """语音转写适配器边界"""

    async def transcribe(
        self,
        audio: bytes,
        token: CancellationToken,
    ) -> str: ...


class ToolExecutor(Protocol):
    """Coordinator 使用的 Tool Worker 客户端边界"""

    async def execute(
        self,
        proposal: ToolProposal,
        decision: PolicyDecision,
        authorization: str | None,
        token: CancellationToken,
        *,
        audit_context: AuditContext,
    ) -> ToolExecutionResult: ...


class MemoryContextProvider(Protocol):
    """Coordinator 使用的最小化记忆上下文边界"""

    def build_history(self, input_text: str) -> tuple[Mapping[str, object], ...]: ...


class SessionContextProvider(Protocol):
    """Coordinator 使用的进程内会话上下文边界"""

    @property
    def session_id(self) -> str: ...

    def build_history(self) -> tuple[Mapping[str, object], ...]: ...

    def add_turn(self, user_text: str, assistant_text: str) -> None: ...


class SessionArchiveProvider(Protocol):
    """Coordinator 使用的可过期会话归档边界"""

    def archive_turn(
        self,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        session_id: str | None = None,
        attachments: Sequence[Mapping[str, object]] = (),
    ) -> object: ...

    def discard_turn(self, turn_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """绑定轮次、策略快照和取消源的待确认工具调用"""

    turn_id: TurnId
    correlation_id: CorrelationId
    proposal: ToolProposal
    decision: PolicyDecision
    source: CancellationSource


@dataclass(frozen=True, slots=True)
class PendingMemoryApproval:
    """绑定轮次和取消源的待确认记忆操作"""

    turn_id: TurnId
    correlation_id: CorrelationId
    operation: MemoryOperation
    source: CancellationSource


class Coordinator:
    """维护活动轮次并编排录音和转写任务"""

    def __init__(
        self,
        audio_session: AudioSession,
        transcript_adapter: TranscriptAdapter,
        event_bus: EventBus,
        state_machine: ConversationStateMachine | None = None,
        *,
        llm_provider: LlmProvider | None = None,
        llm_tools: Sequence[ToolDefinition] = (),
        memory_context: MemoryContextProvider | None = None,
        session_context: SessionContextProvider | None = None,
        session_archive: SessionArchiveProvider | None = None,
        archive_completion: Callable[[str, str], None] | None = None,
        memory_candidates: MemoryCandidateService | None = None,
        short_term_summaries: ShortTermSummaryService | None = None,
        memory_operations: MemoryOperationService | None = None,
        llm_instructions: str = "你是 VoicePet 桌面助手",
        speech_synthesizer: SpeechSynthesizer | None = None,
        audio_player: AudioPlayer | None = None,
        speech_enabled: bool = True,
        manual_input_speech_enabled: bool = False,
        wake_keyword: str = "你好，小蓝",
        policy_engine: PolicyEngine | None = None,
        authorization_issuer: AuthorizationIssuer | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tool_calls_per_turn: int = 4,
        agent_gateway: object | None = None,
        max_turn_duration: float = 120.0,
        max_turn_tokens: int = 16_384,
        llm_model: str = "unknown",
        clock: Callable[[], float] | None = None,
        shutdown_timeout: float = 2.0,
    ) -> None:
        tool_dependencies = (policy_engine, authorization_issuer, tool_executor)
        if any(item is not None for item in tool_dependencies) and not all(
            item is not None for item in tool_dependencies
        ):
            raise ValueError("工具策略、授权和执行器必须成组注入")
        budget_limits = TurnBudgetLimits(
            max_duration=max_turn_duration,
            max_tokens=max_turn_tokens,
            max_tool_calls=max_tool_calls_per_turn,
        )
        validate_llm_model_name(llm_model)
        if type(speech_enabled) is not bool:
            raise TypeError("语音播报开关必须是布尔值")
        if type(manual_input_speech_enabled) is not bool:
            raise TypeError("手动输入语音播报开关必须是布尔值")
        normalize_wake_keyword(wake_keyword)
        self._audio_session = audio_session
        self._transcript_adapter = transcript_adapter
        self._event_bus = event_bus
        self._state_machine = state_machine or ConversationStateMachine()
        self._llm_provider = llm_provider
        self._agent_gateway = agent_gateway
        self._llm_tools = tuple(llm_tools)
        self._memory_context = memory_context
        self._session_context = session_context
        self._session_archive = session_archive
        self._archive_completion = archive_completion
        self._memory_candidates = memory_candidates
        self._short_term_summaries = short_term_summaries
        self._memory_operations = memory_operations
        self._llm_instructions = llm_instructions
        self._speech_synthesizer = speech_synthesizer
        self._audio_player = audio_player
        self._speech_enabled = speech_enabled
        self._manual_input_speech_enabled = manual_input_speech_enabled
        self._active_input_is_manual = False
        self._wake_keyword = wake_keyword
        self._policy_engine = policy_engine
        self._authorization_issuer = authorization_issuer
        self._tool_executor = tool_executor
        self._budget_limits = budget_limits
        self._clock = clock or time.monotonic
        self._llm_model = llm_model
        self._turn_budget: TurnBudget | None = None
        self._shutdown_timeout = shutdown_timeout
        self._tasks: dict[asyncio.Task[None], CancellationSource] = {}
        self._active_source: CancellationSource | None = None
        self._response_text = ""
        self._active_input_text = ""
        self._active_attachments: tuple[LlmAttachment, ...] = ()
        self._llm_history: tuple[Mapping[str, object], ...] = ()
        self._attachment_reader: ReadFileTool | None = None
        self._active_session_id: str | None = None
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_tool_call: LlmToolCall | None = None
        self._pending_approval: PendingApproval | None = None
        self._pending_memory: PendingMemoryApproval | None = None
        self._pending_summary: ShortTermSummaryDraft | None = None
        self._approval_lock = asyncio.Lock()
        self._stopped = False

    def is_current(self, turn_id: TurnId) -> bool:
        return not self._stopped and self._state_machine.turn_id == turn_id

    @property
    def phase(self) -> ConversationPhase:
        return self._state_machine.phase

    @property
    def response_text(self) -> str:
        return self._response_text

    @property
    def speech_enabled(self) -> bool:
        return self._speech_enabled

    @property
    def manual_input_speech_enabled(self) -> bool:
        return self._manual_input_speech_enabled

    @property
    def pending_tool_call(self) -> LlmToolCall | None:
        return self._pending_tool_call

    @property
    def pending_approval(self) -> PendingApproval | None:
        return self._pending_approval

    async def start_listening(self, source: str = "click") -> TurnId:
        self._ensure_running()
        activation_source = self._validate_activation_source(source)
        self._response_text = ""
        self._active_input_text = ""
        self._active_attachments = ()
        self._attachment_reader = None
        self._active_input_is_manual = False
        self._active_session_id = (
            self._session_context.session_id
            if self._session_context is not None
            else None
        )
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_tool_call = None
        self._pending_approval = None
        self._pending_memory = None
        self._pending_summary = None
        budget = TurnBudget(self._budget_limits, clock=self._clock)
        recording_correlation = CorrelationId.new()
        state_event = self._state_machine.start_turn(recording_correlation)
        source = CancellationSource()
        self._active_source = source
        self._turn_budget = budget

        try:
            await self._event_bus.publish(state_event)
        finally:
            # 订阅者失败不能破坏活动轮次必须拥有任务的不变量
            if self.is_current(state_event.turn_id):
                self._launch_pipeline(
                    state_event.turn_id,
                    recording_correlation,
                    source,
                    activation_source=activation_source,
                )
        return state_event.turn_id

    async def submit_text(
        self,
        text: str,
        attachments: tuple[LlmAttachment, ...] = (),
    ) -> TurnId:
        """提交手动文本并跳过录音与语音转写"""

        self._ensure_running()
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized or len(normalized) > 4096:
            raise ValueError("手动输入必须是一至四千零九十六个字符")
        correlation_id = CorrelationId.new()
        state_event = self._state_machine.start_text_turn(correlation_id)
        self._response_text = ""
        self._active_input_text = normalized
        self._active_attachments = tuple(attachments)
        readable_attachments = tuple(
            item for item in attachments if not can_inline_text_attachment(item)
        )
        if readable_attachments:
            try:
                self._attachment_reader = ReadFileTool(
                    allowed_paths={item.path for item in readable_attachments}
                )
            except (OSError, RuntimeError, ValueError):
                # 元数据测试或远端占位附件没有本地正文时仍可正常发送
                self._attachment_reader = None
        else:
            self._attachment_reader = None
        self._active_input_is_manual = True
        self._active_session_id = (
            self._session_context.session_id
            if self._session_context is not None
            else None
        )
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_tool_call = None
        self._pending_approval = None
        self._pending_memory = None
        self._pending_summary = None
        self._turn_budget = TurnBudget(self._budget_limits, clock=self._clock)
        source = CancellationSource()
        self._active_source = source

        try:
            await self._event_bus.publish(state_event)
            await self._event_bus.publish(
                TextInputSubmitted(
                    state_event.turn_id,
                    correlation_id,
                    normalized,
                )
            )
        finally:
            # 手动输入被接受后始终创建对应的后台处理任务
            if self.is_current(state_event.turn_id):
                self._launch_text_pipeline(
                    state_event.turn_id,
                    normalized,
                    source,
                )
        return state_event.turn_id

    async def cancel_active_turn(self) -> None:
        """取消当前轮次并立即把对话状态恢复为空闲"""

        self._ensure_running()
        if self.phase is ConversationPhase.IDLE:
            return
        if self._active_source is not None:
            self._active_source.cancel("user_cancelled")
        finished = self._state_machine.reset(CorrelationId.new())
        self._active_source = None
        self._pending_tool_call = None
        self._pending_approval = None
        self._pending_memory = None
        self._pending_summary = None
        self._turn_budget = None
        if finished is not None:
            await self._event_bus.publish(finished)

    async def capture_manual_transcript(self) -> str:
        """录制一段由静音自动结束的手动语音并返回转写文本"""

        self._ensure_running()
        if self.phase is not ConversationPhase.IDLE:
            raise RuntimeError("当前正在处理其他语音操作")
        source = CancellationSource()
        try:
            audio = await self._audio_session.record_until_silence(
                source.token,
                include_preroll=False,
            )
        except TypeError:
            audio = await self._audio_session.record_until_silence(source.token)
        if not audio:
            return ""
        text = await self._transcript_adapter.transcribe(audio, source.token)
        return text.strip() if isinstance(text, str) else ""

    async def speak_notice(self, text: str) -> TurnId:
        """不经过录音和模型地播放固定提示"""

        self._ensure_running()
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized or len(normalized) > 400:
            raise ValueError("提示文本必须是一至四百个字符")
        if self._speech_synthesizer is None or self._audio_player is None:
            raise RuntimeError("语音提示当前不可用")
        if self.phase is not ConversationPhase.IDLE:
            raise RuntimeError("当前正在处理其他语音操作")

        self._response_text = normalized
        self._active_input_text = ""
        self._active_input_is_manual = False
        self._active_session_id = None
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_tool_call = None
        self._pending_approval = None
        self._pending_memory = None
        self._pending_summary = None
        self._turn_budget = TurnBudget(self._budget_limits, clock=self._clock)
        correlation_id = CorrelationId.new()
        state_event = self._state_machine.start_notice(correlation_id)
        source = CancellationSource()
        self._active_source = source
        await self._event_bus.publish(state_event)
        task = asyncio.create_task(
            self._run_notice(
                state_event.turn_id,
                correlation_id,
                normalized,
                source.token,
            ),
            name=f"voicepet-notice-{state_event.turn_id}-{correlation_id}",
        )
        self._tasks[task] = source
        task.add_done_callback(self._task_finished)
        await task
        return state_event.turn_id

    async def interrupt(self, source: str = "click") -> TurnId:
        self._ensure_running()
        activation_source = self._validate_activation_source(source)
        if self._active_source is not None:
            self._active_source.cancel("user_interrupt")

        self._response_text = ""
        self._active_input_text = ""
        self._active_session_id = (
            self._session_context.session_id
            if self._session_context is not None
            else None
        )
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_tool_call = None
        self._pending_approval = None
        self._pending_memory = None
        self._pending_summary = None
        budget = TurnBudget(self._budget_limits, clock=self._clock)
        recording_correlation = CorrelationId.new()
        state_event = self._state_machine.interrupt(recording_correlation)
        source = CancellationSource()
        self._active_source = source
        self._turn_budget = budget

        try:
            await self._event_bus.publish(state_event)
            if activation_source == "wake_followup":
                await self._event_bus.publish(
                    WakeCommandPending(
                        state_event.turn_id,
                        state_event.correlation_id,
                    )
                )
        finally:
            # 打断后的替代轮次遵循相同任务不变量
            if self.is_current(state_event.turn_id):
                self._launch_pipeline(
                    state_event.turn_id,
                    recording_correlation,
                    source,
                    activation_source=activation_source,
                )
        return state_event.turn_id

    async def set_speech_enabled(self, enabled: bool) -> None:
        """即时更新语音播报状态并停止当前语音工作"""

        self._ensure_running()
        if type(enabled) is not bool:
            raise TypeError("语音播报开关必须是布尔值")
        self._speech_enabled = enabled
        if enabled or self.phase not in {
            ConversationPhase.SYNTHESIZING,
            ConversationPhase.SPEAKING,
        }:
            return
        if self._active_source is not None:
            self._active_source.cancel("speech_disabled")
        finished = self._state_machine.reset(CorrelationId.new())
        if finished is not None and not self._stopped:
            await self._event_bus.publish(finished)

    async def set_manual_input_speech_enabled(self, enabled: bool) -> None:
        """即时更新手动输入语音播报状态"""

        self._ensure_running()
        if type(enabled) is not bool:
            raise TypeError("手动输入语音播报开关必须是布尔值")
        self._manual_input_speech_enabled = enabled
        if (
            enabled
            or not self._active_input_is_manual
            or self.phase
            not in {
                ConversationPhase.SYNTHESIZING,
                ConversationPhase.SPEAKING,
            }
        ):
            return
        if self._active_source is not None:
            self._active_source.cancel("manual_input_speech_disabled")
        finished = self._state_machine.reset(CorrelationId.new())
        if finished is not None and not self._stopped:
            await self._event_bus.publish(finished)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True

        # 对全部自有任务先发出协作取消信号
        sources = set(self._tasks.values())
        if self._active_source is not None:
            sources.add(self._active_source)
        for source in sources:
            source.cancel("shutdown")

        tasks = tuple(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=self._shutdown_timeout,
            )
            # 超过宽限期的适配器使用任务级取消强制收敛
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        self._state_machine.reset(CorrelationId.new())
        self._active_source = None
        self._pending_approval = None
        self._pending_memory = None

    async def approve_pending(self, mode: ConfirmationMode) -> None:
        self._ensure_running()
        async with self._approval_lock:
            pending = self._pending_approval
            memory_pending = self._pending_memory
            if pending is None and memory_pending is None:
                raise RuntimeError("当前没有待确认的工具调用")
            if mode is ConfirmationMode.NONE:
                raise RuntimeError("确认方式未达到策略要求")
            if memory_pending is not None:
                self._pending_memory = None
                await self._execute_memory_operation(memory_pending)
                return
            assert pending is not None
            assert self._authorization_issuer is not None
            try:
                authorization = self._authorization_issuer.issue(
                    pending.decision,
                    mode,
                )
            except Exception as error:
                raise RuntimeError("确认方式未达到策略要求") from error
            self._pending_approval = None
            await self._execute_tool(
                pending.turn_id,
                pending.correlation_id,
                pending.proposal,
                pending.decision,
                authorization,
                pending.source.token,
            )

    async def reject_pending(self) -> None:
        self._ensure_running()
        async with self._approval_lock:
            pending = self._pending_approval
            memory_pending = self._pending_memory
            if pending is None and memory_pending is None:
                raise RuntimeError("当前没有待确认的工具调用")
            if memory_pending is not None:
                self._pending_memory = None
                if not self._accept_result(
                    memory_pending.turn_id,
                    memory_pending.source.token,
                ):
                    return
                await self._event_bus.publish(
                    MemoryResultReady(
                        memory_pending.turn_id,
                        memory_pending.correlation_id,
                        "denied",
                        "用户取消了记忆操作",
                        0,
                    )
                )
                # 用户拒绝不是执行失败，结束原轮次后恢复待机
                if not self._accept_result(
                    memory_pending.turn_id,
                    memory_pending.source.token,
                ):
                    return
                await self.cancel_active_turn()
                return
            assert pending is not None
            self._pending_approval = None
            if not self._accept_result(pending.turn_id, pending.source.token):
                return
            await self._event_bus.publish(
                ToolResultReady(
                    pending.turn_id,
                    pending.correlation_id,
                    pending.proposal.call_id,
                    "denied",
                    {"message": "用户拒绝了工具调用"},
                )
            )
            recovering = self._state_machine.transition(
                ConversationPhase.RECOVERING,
                pending.turn_id,
                pending.correlation_id,
            )
            await self._event_bus.publish(recovering)

    def _ensure_running(self) -> None:
        if self._stopped:
            raise RuntimeError("coordinator is stopped")

    @staticmethod
    def _validate_activation_source(source: str) -> str:
        if source not in _ACTIVATION_SOURCES:
            raise ValueError("激活来源无效")
        return source

    def _launch_pipeline(
        self,
        turn_id: TurnId,
        recording_correlation: CorrelationId,
        source: CancellationSource,
        *,
        activation_source: str,
    ) -> None:
        task = asyncio.create_task(
            self._run_pipeline(
                turn_id,
                recording_correlation,
                source.token,
                activation_source,
            ),
            name=f"voicepet-recording-{turn_id}-{recording_correlation}",
        )
        self._tasks[task] = source
        task.add_done_callback(self._task_finished)

    def _launch_text_pipeline(
        self,
        turn_id: TurnId,
        text: str,
        source: CancellationSource,
    ) -> None:
        task = asyncio.create_task(
            self._run_text_pipeline(turn_id, text, source.token),
            name=f"voicepet-text-{turn_id}",
        )
        self._tasks[task] = source
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.pop(task, None)
        # 主动读取异常以避免后台任务产生未检索异常警告
        if not task.cancelled():
            task.exception()

    async def _run_pipeline(
        self,
        turn_id: TurnId,
        recording_correlation: CorrelationId,
        token: CancellationToken,
        activation_source: str,
    ) -> None:
        try:
            # 录音任务显式携带轮次和操作标识便于诊断
            _ = recording_correlation
            if activation_source == "wake_word":
                await self._event_bus.publish(
                    WakeCommandPending(
                        turn_id,
                        recording_correlation,
                    )
                )
                try:
                    await self._play_wake_acknowledgement(
                        turn_id,
                        recording_correlation,
                        token,
                    )
                except (TtsError, TurnBudgetExceededError):
                    # 唤醒应答失败时仍然继续收音
                    pass
                if not self._accept_result(turn_id, token):
                    return
            try:
                async def started() -> None:
                    if self._accept_result(turn_id, token):
                        await self._event_bus.publish(
                            RecordingStarted(turn_id, recording_correlation)
                        )

                async def record() -> bytes:
                    recorder = self._audio_session.record_until_silence
                    options = {}
                    if activation_source in {"wake_word", "wake_followup"}:
                        options["include_preroll"] = False
                    # 新适配器在音频就绪后回调，保留旧录音适配器兼容入口
                    if "on_started" in inspect.signature(recorder).parameters:
                        options["on_started"] = started
                    else:
                        await started()
                    token.throw_if_cancelled()
                    return await recorder(token, **options)

                audio = await self._await_with_budget(record)
            except TurnBudgetExceededError as error:
                await self._handle_runtime_error(
                    turn_id,
                    recording_correlation,
                    token,
                    error,
                )
                return
            if not self._accept_result(turn_id, token):
                return
            if not audio:
                reset_event = self._state_machine.reset(recording_correlation)
                if reset_event is not None and not self._stopped:
                    await self._event_bus.publish(reset_event)
                return

            transcription_correlation = CorrelationId.new()
            transcribing = self._state_machine.transition(
                ConversationPhase.TRANSCRIBING,
                turn_id,
                transcription_correlation,
            )
            await self._event_bus.publish(transcribing)
            if not self._accept_result(turn_id, token):
                return

            try:
                text = await self._await_with_budget(
                    lambda: self._transcript_adapter.transcribe(audio, token)
                )
            except (AsrError, TurnBudgetExceededError) as error:
                await self._handle_runtime_error(
                    turn_id,
                    transcription_correlation,
                    token,
                    error,
                )
                return
            if not self._accept_result(turn_id, token):
                return

            normalized_text = prepare_transcript_for_source(
                text,
                source=activation_source,
                keyword=self._wake_keyword,
            )
            if not normalized_text:
                reset_event = self._state_machine.reset(
                    transcription_correlation
                )
                if reset_event is not None and not self._stopped:
                    await self._event_bus.publish(reset_event)
                return

            await self._event_bus.publish(
                TranscriptReady(
                    turn_id,
                    transcription_correlation,
                    normalized_text,
                )
            )
            if not self._accept_result(turn_id, token):
                return
            self._active_input_text = normalized_text

            thinking = self._state_machine.transition(
                ConversationPhase.THINKING,
                turn_id,
                transcription_correlation,
            )
            await self._event_bus.publish(thinking)
            if not self._accept_result(turn_id, token):
                return
            await self._process_input_text(turn_id, normalized_text, token)
        except AudioError as error:
            await self._handle_runtime_error(
                turn_id,
                recording_correlation,
                token,
                error,
            )
        except (CancelledError, asyncio.CancelledError):
            # 协作取消和任务级取消都属于正常控制流
            return

    async def _run_text_pipeline(
        self,
        turn_id: TurnId,
        text: str,
        token: CancellationToken,
    ) -> None:
        try:
            await self._process_input_text(turn_id, text, token)
        except (CancelledError, asyncio.CancelledError):
            return

    async def _process_input_text(
        self,
        turn_id: TurnId,
        text: str,
        token: CancellationToken,
    ) -> None:
        """复用语音和手动文本共有的记忆与模型处理链路"""

        if not self._accept_result(turn_id, token):
            return
        if self._memory_operations is not None:
            operation = await asyncio.to_thread(
                self._memory_operations.plan,
                text,
                str(turn_id),
            )
            if operation is not None:
                await self._handle_memory_operation(turn_id, operation, token)
                return
        if self._agent_gateway is not None:
            await self._run_agent_turn(turn_id, text, token, attachments=self._active_attachments)
            return
        if self._llm_provider is not None:
            history: tuple[Mapping[str, object], ...] = ()
            if self._session_context is not None:
                history = self._session_context.build_history()
            if self._memory_context is not None:
                history += await asyncio.to_thread(
                    self._memory_context.build_history, text
                )
            await self._run_llm(turn_id, text, token, history=history)

    async def _run_agent_turn(self, turn_id: TurnId, text: str, token: CancellationToken, *, attachments: tuple[LlmAttachment, ...] = ()) -> None:
        """把 Agent 面向用户的文本事件接入现有聊天和语音链路"""
        correlation_id = CorrelationId.new()
        chunks: list[str] = []
        try:
            agent_attachments = tuple(
                {"name": item.name, "path": item.path, "media_type": item.media_type, "kind": item.kind}
                for item in attachments
            )
            async for event in self._agent_gateway.run_turn(
                self._active_session_id or "default",
                text,
                turn_id=str(turn_id),
                attachments=agent_attachments,
            ):
                if token.is_cancelled:
                    await self._agent_gateway.cancel()
                    return
                if event.type is AgentEventType.TEXT_DELTA:
                    value = str(event.payload.get("text", ""))
                    chunks.append(value)
                    await self._event_bus.publish(TextDelta(turn_id, correlation_id, value))
                elif event.type is AgentEventType.COMMAND_STARTED:
                    await self._event_bus.publish(
                        AgentProgress(
                            turn_id,
                            correlation_id,
                            str(event.payload.get("message", "Agent 正在执行操作")),
                        )
                    )
                elif event.type is AgentEventType.COMMAND_COMPLETED:
                    await self._event_bus.publish(
                        AgentProgress(
                            turn_id,
                            correlation_id,
                            str(event.payload.get("message", "操作已完成")),
                        )
                    )
                if event.type is AgentEventType.APPROVAL_REQUEST:
                    approval_id = str(event.payload.get("request_id", ""))
                    if approval_id:
                        raw_options = event.payload.get("options", ())
                        options = tuple(item for item in raw_options if isinstance(item, Mapping)) if isinstance(raw_options, (list, tuple)) else ()
                        await self._event_bus.publish(AgentApprovalRequested(turn_id, correlation_id, approval_id, str(event.payload.get("message", "Agent 请求确认")), options))
                if event.type is AgentEventType.TURN_COMPLETED:
                    break
            response = "".join(chunks)
            if response and self._session_context is not None:
                self._session_context.add_turn(text, response)
            if response and self._session_archive is not None:
                await self._archive_completed_turn(turn_id, text, response, self._active_session_id, token)
            finished = self._state_machine.reset(correlation_id)
            if finished is not None and not self._stopped:
                await self._event_bus.publish(finished)
        except asyncio.CancelledError:
            await self._agent_gateway.cancel()
        except Exception as error:  # noqa: BLE001 Agent 失败只显示安全错误
            await self._handle_runtime_error(turn_id, correlation_id, token, error)

    async def _play_wake_acknowledgement(
        self,
        turn_id: TurnId,
        correlation_id: CorrelationId,
        token: CancellationToken,
    ) -> None:
        """在正式收音前播放简短唤醒应答"""

        # 固定唤醒应答独立于模型回复播报开关，播放完成后才进入录音
        if (
            self._speech_synthesizer is None
            or self._audio_player is None
        ):
            return
        audio = await self._await_with_budget(
            lambda: self._speech_synthesizer.synthesize(
                _WAKE_ACKNOWLEDGEMENT,
                token,
            )
        )
        if not self._accept_result(turn_id, token):
            return
        await self._event_bus.publish(
            SpeakRequested(
                turn_id,
                correlation_id,
                _WAKE_ACKNOWLEDGEMENT,
            )
        )
        if not self._accept_result(turn_id, token):
            return
        await self._await_with_budget(
            lambda: self._audio_player.play(audio, token)
        )

    async def _run_notice(
        self,
        turn_id: TurnId,
        correlation_id: CorrelationId,
        text: str,
        token: CancellationToken,
    ) -> None:
        assert self._speech_synthesizer is not None
        assert self._audio_player is not None
        try:
            audio = await self._await_with_budget(
                lambda: self._speech_synthesizer.synthesize(text, token)
            )
            if not self._accept_result(turn_id, token):
                return
            speaking = self._state_machine.transition(
                ConversationPhase.SPEAKING,
                turn_id,
                correlation_id,
            )
            await self._event_bus.publish(speaking)
            if not self._accept_result(turn_id, token):
                return
            await self._event_bus.publish(
                SpeakRequested(turn_id, correlation_id, text)
            )
            if not self._accept_result(turn_id, token):
                return
            await self._await_with_budget(
                lambda: self._audio_player.play(audio, token)
            )
            if not self._accept_result(turn_id, token):
                return
            finished = self._state_machine.transition(
                ConversationPhase.IDLE,
                turn_id,
                correlation_id,
            )
            await self._event_bus.publish(finished)
        except (TtsError, TurnBudgetExceededError):
            reset = self._state_machine.reset(correlation_id)
            if reset is not None and not self._stopped:
                await self._event_bus.publish(reset)
        except (CancelledError, asyncio.CancelledError):
            return

    async def _handle_memory_operation(
        self,
        turn_id: TurnId,
        operation: MemoryOperation,
        token: CancellationToken,
    ) -> None:
        correlation_id = CorrelationId.new()
        assert self._active_source is not None
        if operation.requires_confirmation:
            awaiting = self._state_machine.transition(
                ConversationPhase.AWAITING_APPROVAL,
                turn_id,
                correlation_id,
            )
            self._pending_memory = PendingMemoryApproval(
                turn_id,
                correlation_id,
                operation,
                self._active_source,
            )
            await self._event_bus.publish(awaiting)
            if self._accept_result(turn_id, token):
                await self._event_bus.publish(
                    ApprovalRequested(
                        turn_id,
                        correlation_id,
                        f"memory:{operation.action.value}",
                        operation.summary,
                        "隐私",
                    )
                )
            return
        pending = PendingMemoryApproval(
            turn_id,
            correlation_id,
            operation,
            self._active_source,
        )
        await self._execute_memory_operation(pending)

    async def _execute_memory_operation(
        self,
        pending: PendingMemoryApproval,
    ) -> None:
        assert self._memory_operations is not None
        if not self._accept_result(pending.turn_id, pending.source.token):
            return
        try:
            self._require_turn_budget().ensure_available()
        except TurnBudgetExceededError as error:
            await self._handle_runtime_error(
                pending.turn_id,
                pending.correlation_id,
                pending.source.token,
                error,
            )
            return
        try:
            result = await asyncio.to_thread(
                self._memory_operations.execute,
                pending.operation,
            )
        except Exception as error:  # noqa: BLE001 记忆存储属于本地持久化边界
            if not self._accept_result(pending.turn_id, pending.source.token):
                return
            message = (
                "记忆功能未启用，未保存这条内容"
                if isinstance(error, MemoryConfigurationError)
                and str(error) == "记忆写入已关闭"
                else "记忆操作失败"
            )
            await self._event_bus.publish(
                MemoryResultReady(
                    pending.turn_id,
                    pending.correlation_id,
                    "failed",
                    message,
                    0,
                )
            )
            recovering = self._state_machine.transition(
                ConversationPhase.RECOVERING,
                pending.turn_id,
                pending.correlation_id,
            )
            await self._event_bus.publish(recovering)
            return
        if not self._accept_result(pending.turn_id, pending.source.token):
            return
        await self._event_bus.publish(
            MemoryResultReady(
                pending.turn_id,
                pending.correlation_id,
                "success",
                result.safe_message,
                result.affected,
            )
        )
        if not self._accept_result(pending.turn_id, pending.source.token):
            return
        try:
            self._require_turn_budget().ensure_available()
        except TurnBudgetExceededError as error:
            await self._handle_runtime_error(
                pending.turn_id,
                pending.correlation_id,
                pending.source.token,
                error,
            )
            return
        finished = self._state_machine.reset(pending.correlation_id)
        if finished is not None and not self._stopped:
            await self._event_bus.publish(finished)

    async def _run_llm(
        self,
        turn_id: TurnId,
        input_text: str,
        token: CancellationToken,
        *,
        history: tuple[Mapping[str, object], ...] = (),
        include_attachments: bool = True,
    ) -> None:
        assert self._llm_provider is not None
        correlation_id = CorrelationId.new()
        chunks: list[str] = []
        speech_chunks: list[str] = []
        completed = False
        tool_call: LlmToolCall | None = None
        tool_calls: list[LlmToolCall] = []
        self._llm_history = history + ({"role": "user", "content": input_text},)
        try:
            budget = self._require_turn_budget()
            request = LlmRequest(
                self._llm_instructions,
                input_text,
                history=history,
                tools=tuple(
                    tool
                    for tool in self._llm_tools
                    if not (
                        self._pending_summary is not None
                        and tool.name == SHORT_TERM_SUMMARY_TOOL_NAME
                    )
                ) + (
                    (
                        ToolDefinition(
                            self._attachment_reader.manifest.name,
                            self._attachment_reader.manifest.description,
                            {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "offset": {"type": "integer"},
                                    "length": {"type": "integer"},
                                },
                                "required": ["path", "offset", "length"],
                                "additionalProperties": False,
                            },
                        ),
                    )
                    if self._attachment_reader is not None
                    else ()
                ),
                max_output_tokens=budget.output_token_limit(4096),
                attachments=(
                    self._active_attachments if include_attachments else ()
                ),
            )
            request_started = float(self._clock())
            async for event in self._stream_llm_with_budget(
                request,
                token,
                budget,
            ):
                if not self._accept_result(turn_id, token):
                    return
                if completed:
                    raise LlmProtocolError("LLM 完成事件后仍有流事件")
                if isinstance(event, LlmTextDelta):
                    chunks.append(event.text)
                    await self._event_bus.publish(
                        TextDelta(turn_id, correlation_id, event.text)
                    )
                elif isinstance(event, LlmToolCall):
                    tool_calls.append(event)
                    if tool_call is None:
                        tool_call = event
                elif isinstance(event, LlmCompleted):
                    try:
                        total_tokens = budget.record_usage(
                            event.input_tokens,
                            event.output_tokens,
                        )
                    except (TypeError, ValueError) as error:
                        raise LlmProtocolError("LLM token 统计无效") from error
                    duration_ms = round(
                        max(0.0, float(self._clock()) - request_started)
                        * 1000
                    )
                    await self._event_bus.publish(
                        LlmUsageRecorded(
                            turn_id,
                            correlation_id,
                            self._llm_model,
                            duration_ms,
                            event.input_tokens,
                            event.output_tokens,
                            total_tokens,
                        )
                    )
                    if event.incomplete and not tool_calls:
                        notice = (
                            "\n\n> 回复达到长度上限，以上内容可能不完整"
                            if chunks
                            else "回复达到长度上限，但没有生成可显示的正文，请缩短问题后重试"
                        )
                        chunks.append(notice)
                        await self._event_bus.publish(
                            TextDelta(turn_id, correlation_id, notice)
                        )
                    self._response_text = "".join(chunks)
                    speech_text = markdown_to_speech_text(
                        self._response_text
                    )
                    speech_chunks.extend(split_speech_text(speech_text))
                    completed = True
                if not self._accept_result(turn_id, token):
                    return
            if not completed:
                raise LlmProtocolError("LLM 流缺少完成事件")
            if len(tool_calls) > 1:
                if self._attachment_reader is None or any(
                    item.name != "read_file" for item in tool_calls
                ):
                    raise LlmProtocolError("单次响应包含多个工具提议")
                if len({item.call_id for item in tool_calls}) != len(tool_calls):
                    raise LlmProtocolError("附件读取调用标识重复")
                # 兼容服务可能一次返回多个读取请求，逐个校验路径并串行读取
                for item in tool_calls:
                    if not self._accept_result(turn_id, token):
                        return
                    budget.reserve_tool_call()
                    result = await self._attachment_reader.execute(item.arguments, token)
                    if not self._accept_result(turn_id, token):
                        return
                    await self._event_bus.publish(ToolResultReady(
                        turn_id, CorrelationId.new(), item.call_id,
                        result.status.value, {"message": result.safe_message},
                    ))
                    if not self._accept_result(turn_id, token):
                        return
                    self._append_tool_history(
                        ToolProposal(item.call_id, item.name, item.arguments), result
                    )
                if self._accept_result(turn_id, token):
                    await self._run_llm(
                        turn_id, "请根据工具结果继续完成当前请求", token,
                        history=self._llm_history,
                        include_attachments=False,
                    )
                return
            budget.ensure_available()
            if tool_call is not None:
                self._pending_tool_call = tool_call
                await self._dispatch_llm_tool(
                    turn_id,
                    correlation_id,
                    tool_call,
                    token,
                )
                return
            if (
                completed
                and self._response_text.strip()
                and self._session_context is not None
                and self._active_session_id == self._session_context.session_id
                and not self._session_turn_archived
                and self._accept_result(turn_id, token)
            ):
                self._session_context.add_turn(
                    self._active_input_text,
                    self._response_text,
                )
                self._session_turn_archived = True
            if (
                completed
                and self._response_text.strip()
                and self._session_archive is not None
                and not self._persistent_turn_archived
            ):
                await self._archive_completed_turn(
                    turn_id,
                    self._active_input_text,
                    self._response_text,
                    self._active_session_id,
                    token,
                )
            if completed and self._response_text.strip():
                if (
                    self._speech_enabled_for_active_turn()
                    and self._speech_synthesizer is not None
                    and self._audio_player is not None
                ):
                    await self._run_tts(turn_id, speech_chunks, token)
                else:
                    finished = self._state_machine.reset(correlation_id)
                    if finished is not None and not self._stopped:
                        await self._event_bus.publish(finished)
        except (LlmError, TurnBudgetExceededError) as error:
            await self._handle_runtime_error(
                turn_id,
                correlation_id,
                token,
                error,
            )

    async def _dispatch_llm_tool(
        self,
        turn_id: TurnId,
        correlation_id: CorrelationId,
        event: LlmToolCall,
        token: CancellationToken,
    ) -> None:
        if event.name == "read_file" and self._attachment_reader is not None:
            proposal = ToolProposal(event.call_id, event.name, event.arguments)
            self._require_turn_budget().reserve_tool_call()
            result = await self._attachment_reader.execute(event.arguments, token)
            await self._event_bus.publish(
                ToolResultReady(
                    turn_id,
                    CorrelationId.new(),
                    event.call_id,
                    result.status.value,
                    {"message": result.safe_message},
                )
            )
            if self._accept_result(turn_id, token):
                await self._continue_after_tool(turn_id, proposal, result, token)
            return
        if (
            event.name == MEMORY_CANDIDATE_TOOL_NAME
            and self._memory_candidates is not None
        ):
            await self._handle_memory_candidate(turn_id, event, token)
            return
        if (
            event.name == SHORT_TERM_SUMMARY_TOOL_NAME
            and self._short_term_summaries is not None
        ):
            await self._handle_short_term_summary(turn_id, event, token)
            return
        if self._policy_engine is not None:
            await self._handle_tool_proposal(turn_id, event, token)
            return
        awaiting = self._state_machine.transition(
            ConversationPhase.AWAITING_APPROVAL,
            turn_id,
            correlation_id,
        )
        await self._event_bus.publish(awaiting)
        if not self._accept_result(turn_id, token):
            return
        await self._event_bus.publish(
            ApprovalRequested(
                turn_id,
                correlation_id,
                event.call_id,
                event.name,
                "待评估",
            )
        )

    async def _archive_completed_turn(
        self,
        turn_id: TurnId,
        user_text: str,
        assistant_text: str,
        session_id: str | None,
        token: CancellationToken,
    ) -> None:
        assert self._session_archive is not None
        if not self._accept_result(turn_id, token):
            return
        budget = self._require_turn_budget()
        budget.ensure_available()
        try:
            archive_arguments = (
                (str(turn_id), user_text, assistant_text)
                if session_id is None
                else (str(turn_id), user_text, assistant_text, session_id)
            )
            attachment_view = tuple(
                {
                    "name": item.name,
                    "path": item.path,
                    "kind": item.kind,
                    "mediaType": item.media_type,
                }
                for item in self._active_attachments
            )
            archive_method = self._session_archive.archive_turn
            supports_attachments = False
            try:
                supports_attachments = "attachments" in inspect.signature(archive_method).parameters
            except (TypeError, ValueError):
                supports_attachments = True
            if not supports_attachments:
                await asyncio.to_thread(archive_method, *archive_arguments)
            else:
                await asyncio.to_thread(
                    archive_method,
                    *archive_arguments,
                    attachments=attachment_view,
                )
        except Exception as error:  # noqa: BLE001 会话归档失败不能阻断最终回复
            _ = error
            return
        if not self._accept_result(turn_id, token):
            try:
                await asyncio.to_thread(
                    self._session_archive.discard_turn,
                    str(turn_id),
                )
            except Exception as error:  # noqa: BLE001 迟到归档回滚失败不能破坏活动轮次
                _ = error
            return
        budget.ensure_available()
        if self._pending_summary is not None and self._short_term_summaries is not None:
            try:
                await asyncio.to_thread(
                    self._short_term_summaries.save,
                    str(turn_id),
                    self._pending_summary,
                )
            except Exception as error:  # noqa: BLE001 短期摘要失败不能阻断最终回复
                _ = error
            if not self._accept_result(turn_id, token):
                try:
                    await asyncio.to_thread(
                        self._session_archive.discard_turn,
                        str(turn_id),
                    )
                except Exception as error:  # noqa: BLE001 迟到摘要回滚失败不能破坏活动轮次
                    _ = error
                return
            budget.ensure_available()
        self._persistent_turn_archived = True
        if (
            session_id is not None
            and self._archive_completion is not None
            and self._accept_result(turn_id, token)
        ):
            try:
                self._archive_completion(session_id, str(turn_id))
            except Exception as error:  # noqa: BLE001 后台入队失败不能阻断前台回复
                _ = error

    async def _handle_short_term_summary(
        self,
        turn_id: TurnId,
        event: LlmToolCall,
        token: CancellationToken,
    ) -> None:
        assert self._short_term_summaries is not None
        proposal = ToolProposal(event.call_id, event.name, event.arguments)
        correlation_id = CorrelationId.new()
        try:
            self._require_turn_budget().reserve_tool_call()
            self._pending_summary = self._short_term_summaries.prepare(
                event.arguments
            )
        except (TypeError, ValueError):
            status = "denied"
            message = "短期摘要未保存"
            result = ToolExecutionResult(
                ToolExecutionStatus.DENIED,
                {"status": "rejected"},
                message,
            )
        else:
            status = "pending"
            message = "短期摘要将在本轮完成后保存"
            result = ToolExecutionResult(
                ToolExecutionStatus.SUCCESS,
                {"status": "pending"},
                message,
            )
        if not self._accept_result(turn_id, token):
            return
        await self._event_bus.publish(
            MemoryResultReady(
                turn_id,
                correlation_id,
                status,
                message,
                0,
            )
        )
        if not self._accept_result(turn_id, token):
            return
        await self._continue_after_tool(turn_id, proposal, result, token)

    async def _handle_memory_candidate(
        self,
        turn_id: TurnId,
        event: LlmToolCall,
        token: CancellationToken,
    ) -> None:
        assert self._memory_candidates is not None
        proposal = ToolProposal(event.call_id, event.name, event.arguments)
        correlation_id = CorrelationId.new()
        self._require_turn_budget().reserve_tool_call()
        try:
            record = await asyncio.to_thread(
                self._memory_candidates.create,
                event.arguments,
                str(turn_id),
            )
        except Exception as error:  # noqa: BLE001 候选记忆必须隔离模型参数和存储失败
            _ = error
            status = "denied"
            affected = 0
            message = "候选记忆未保存"
            result = ToolExecutionResult(
                ToolExecutionStatus.DENIED,
                {"status": "rejected"},
                message,
            )
        else:
            status = "candidate"
            affected = 1
            message = "已创建待用户审核的记忆候选"
            result = ToolExecutionResult(
                ToolExecutionStatus.SUCCESS,
                {"status": "candidate"},
                message,
            )
            if not self._accept_result(turn_id, token):
                await asyncio.to_thread(
                    self._memory_candidates.discard,
                    record.id,
                )
                return
        if not self._accept_result(turn_id, token):
            return
        await self._event_bus.publish(
            MemoryResultReady(
                turn_id,
                correlation_id,
                status,
                message,
                affected,
            )
        )
        if not self._accept_result(turn_id, token):
            return
        await self._continue_after_tool(turn_id, proposal, result, token)

    async def _handle_tool_proposal(
        self,
        turn_id: TurnId,
        event: LlmToolCall,
        token: CancellationToken,
    ) -> None:
        assert self._policy_engine is not None
        proposal = ToolProposal(event.call_id, event.name, event.arguments)
        correlation_id = CorrelationId.new()
        self._require_turn_budget().reserve_tool_call()
        decision = self._policy_engine.evaluate(proposal)
        if not decision.allowed:
            await self._event_bus.publish(
                ToolResultReady(
                    turn_id,
                    correlation_id,
                    proposal.call_id,
                    "denied",
                    {"message": decision.reason},
                )
            )
            if not self._accept_result(turn_id, token):
                return
            recovering = self._state_machine.transition(
                ConversationPhase.RECOVERING,
                turn_id,
                correlation_id,
            )
            await self._event_bus.publish(recovering)
            return
        assert self._active_source is not None
        if decision.requires_authorization:
            awaiting = self._state_machine.transition(
                ConversationPhase.AWAITING_APPROVAL,
                turn_id,
                correlation_id,
            )
            self._pending_approval = PendingApproval(
                turn_id,
                correlation_id,
                proposal,
                decision,
                self._active_source,
            )
            await self._event_bus.publish(awaiting)
            if not self._accept_result(turn_id, token):
                return
            await self._event_bus.publish(
                ApprovalRequested(
                    turn_id,
                    correlation_id,
                    proposal.call_id,
                    decision.summary,
                    decision.risk.label,
                )
            )
            return
        await self._execute_tool(
            turn_id,
            correlation_id,
            proposal,
            decision,
            None,
            token,
        )

    async def _execute_tool(
        self,
        turn_id: TurnId,
        correlation_id: CorrelationId,
        proposal: ToolProposal,
        decision: PolicyDecision,
        authorization: str | None,
        token: CancellationToken,
    ) -> None:
        assert self._tool_executor is not None
        if not self._accept_result(turn_id, token):
            return
        try:
            self._require_turn_budget().ensure_available()
        except TurnBudgetExceededError as error:
            await self._handle_runtime_error(
                turn_id,
                correlation_id,
                token,
                error,
            )
            return
        executing = self._state_machine.transition(
            ConversationPhase.EXECUTING_TOOL,
            turn_id,
            correlation_id,
        )
        await self._event_bus.publish(executing)
        if not self._accept_result(turn_id, token):
            return
        try:
            self._require_turn_budget().ensure_available()
        except TurnBudgetExceededError as error:
            await self._handle_runtime_error(
                turn_id,
                correlation_id,
                token,
                error,
            )
            return
        try:
            result = await self._tool_executor.execute(
                proposal,
                decision,
                authorization,
                token,
                audit_context=AuditContext(
                    str(turn_id),
                    str(correlation_id),
                    proposal.call_id,
                ),
            )
        except (CancelledError, asyncio.CancelledError):
            return
        except Exception as error:  # noqa: BLE001 Worker 客户端属于进程外边界
            _ = error
            if not self._accept_result(turn_id, token):
                return
            await self._event_bus.publish(
                runtime_error_event(
                    turn_id,
                    correlation_id,
                    error,
                    error_code="tool.execution",
                    retryable=False,
                )
            )
            recovering = self._state_machine.transition(
                ConversationPhase.RECOVERING,
                turn_id,
                correlation_id,
            )
            await self._event_bus.publish(recovering)
            if not self._accept_result(turn_id, token):
                return
            finished = self._state_machine.reset(correlation_id)
            if finished is not None and not self._stopped:
                await self._event_bus.publish(finished)
            return
        if not self._accept_result(turn_id, token):
            return
        await self._event_bus.publish(
            ToolResultReady(
                turn_id,
                correlation_id,
                proposal.call_id,
                result.status.value,
                result.payload,
            )
        )
        if not self._accept_result(turn_id, token):
            return
        thinking = self._state_machine.transition(
            ConversationPhase.THINKING,
            turn_id,
            correlation_id,
        )
        await self._event_bus.publish(thinking)
        if not self._accept_result(turn_id, token):
            return
        if self._llm_provider is not None:
            await self._continue_after_tool(
                turn_id,
                proposal,
                result,
                token,
            )

    async def _continue_after_tool(
        self,
        turn_id: TurnId,
        proposal: ToolProposal,
        result: ToolExecutionResult,
        token: CancellationToken,
    ) -> None:
        self._append_tool_history(proposal, result)
        await self._run_llm(
            turn_id,
            "请根据工具结果继续完成当前请求",
            token,
            history=self._llm_history,
            include_attachments=False,
        )

    def _append_tool_history(
        self, proposal: ToolProposal, result: ToolExecutionResult,
    ) -> None:
        # 保留此前所有分块结果，避免读取后一块时遗失前面的内容
        self._llm_history += (
            {
                "type": "function_call",
                "call_id": proposal.call_id,
                "name": proposal.tool_name,
                "arguments": canonical_json(proposal.arguments),
            },
            {
                "type": "function_call_output",
                "call_id": proposal.call_id,
                "output": canonical_json(
                    {
                        "status": result.status.value,
                        "payload": result.payload,
                        "safe_message": result.safe_message,
                    }
                ),
            },
        )

    async def _run_tts(
        self,
        turn_id: TurnId,
        speech_chunks: list[str],
        token: CancellationToken,
    ) -> None:
        assert self._speech_synthesizer is not None
        assert self._audio_player is not None
        correlation_id = CorrelationId.new()
        try:
            if not self._speech_enabled_for_active_turn():
                finished = self._state_machine.reset(correlation_id)
                if finished is not None and not self._stopped:
                    await self._event_bus.publish(finished)
                return
            synthesizing = self._state_machine.transition(
                ConversationPhase.SYNTHESIZING,
                turn_id,
                correlation_id,
            )
            await self._event_bus.publish(synthesizing)
            if not self._accept_result(turn_id, token):
                return

            audio_chunks: list[SynthesizedAudio] = []
            for text in speech_chunks:
                audio = await self._await_with_budget(
                    lambda text=text: self._speech_synthesizer.synthesize(
                        text,
                        token,
                    )
                )
                if not self._accept_result(turn_id, token):
                    return
                audio_chunks.append(audio)

            speaking = self._state_machine.transition(
                ConversationPhase.SPEAKING,
                turn_id,
                correlation_id,
            )
            await self._event_bus.publish(speaking)
            if not self._accept_result(turn_id, token):
                return

            for text, audio in zip(speech_chunks, audio_chunks, strict=True):
                await self._event_bus.publish(
                    SpeakRequested(turn_id, correlation_id, text)
                )
                if not self._accept_result(turn_id, token):
                    return
                await self._await_with_budget(
                    lambda audio=audio: self._audio_player.play(audio, token)
                )
                if not self._accept_result(turn_id, token):
                    return

            finished = self._state_machine.transition(
                ConversationPhase.IDLE,
                turn_id,
                correlation_id,
            )
            await self._event_bus.publish(finished)
        except (TtsError, TurnBudgetExceededError) as error:
            await self._handle_runtime_error(
                turn_id,
                correlation_id,
                token,
                error,
                enter_recovery=False,
            )

    def _speech_enabled_for_active_turn(self) -> bool:
        return self._speech_enabled and (
            not self._active_input_is_manual
            or self._manual_input_speech_enabled
        )

    async def _handle_runtime_error(
        self,
        turn_id: TurnId,
        correlation_id: CorrelationId,
        token: CancellationToken,
        error: AudioError | AsrError | LlmError | TtsError | TurnBudgetExceededError,
        *,
        enter_recovery: bool = True,
    ) -> None:
        if not self._accept_result(turn_id, token):
            return
        await self._event_bus.publish(
            runtime_error_event(turn_id, correlation_id, error)
        )
        if not self._accept_result(turn_id, token):
            return
        if not enter_recovery:
            finished = self._state_machine.reset(correlation_id)
            if finished is not None and not self._stopped:
                await self._event_bus.publish(finished)
            return
        recovering = self._state_machine.transition(
            ConversationPhase.RECOVERING,
            turn_id,
            correlation_id,
        )
        await self._event_bus.publish(recovering)
        if not self._accept_result(turn_id, token):
            return
        finished = self._state_machine.reset(correlation_id)
        if finished is not None and not self._stopped:
            await self._event_bus.publish(finished)

    def _require_turn_budget(self) -> TurnBudget:
        budget = self._turn_budget
        if budget is None:
            raise RuntimeError("当前没有活动轮次预算")
        return budget

    async def _await_with_budget(
        self,
        operation: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        """在当前轮次剩余时间内等待可安全取消的操作"""

        budget = self._require_turn_budget()
        budget.ensure_available()
        timeout = asyncio.timeout(budget.remaining_duration())
        try:
            async with timeout:
                return await operation()
        except TimeoutError as error:
            if timeout.expired():
                raise TurnBudgetExceededError("duration") from error
            raise

    async def _stream_llm_with_budget(
        self,
        request: LlmRequest,
        token: CancellationToken,
        budget: TurnBudget,
    ) -> AsyncIterator[object]:
        """在当前轮次剩余时间内消费 LLM 流"""

        assert self._llm_provider is not None
        budget.ensure_available()
        timeout = asyncio.timeout(budget.remaining_duration())
        try:
            async with timeout:
                async for event in self._llm_provider.stream(request, token):
                    yield event
        except TimeoutError as error:
            if timeout.expired():
                raise TurnBudgetExceededError("duration") from error
            raise

    def _accept_result(
        self,
        turn_id: TurnId,
        token: CancellationToken,
    ) -> bool:
        if token.is_cancelled:
            return False
        return self.is_current(turn_id)
