"""对话轮次编排与过期结果隔离"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from .agent_types import AgentEventType
from .asr import AsrError
from .audio_types import AudioError
from .cancellation import CancellationSource, CancellationToken, CancelledError
from .config import normalize_wake_keyword
from .event_bus import EventBus
from .events import (
    AgentActivityCompleted,
    AgentActivityOutput,
    AgentActivityStarted,
    AgentApprovalRequested,
    AgentProgress,
    ApprovalRequested,
    ConversationPhase,
    CorrelationId,
    MemoryResultReady,
    RecordingStarted,
    SpeakRequested,
    TextDelta,
    TextInputSubmitted,
    TranscriptReady,
    TurnId,
    WakeCommandPending,
)
from .llm import LlmAttachment
from .memory import MemoryConfigurationError
from .memory_intents import MemoryOperation, MemoryOperationService
from .runtime_errors import runtime_error_event
from .state_machine import ConversationStateMachine
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
_MAX_ACTIVITY_TITLE_CHARS = 400
_FAILED_ACTIVITY_STATUSES = frozenset({"failed", "declined", "cancelled", "error"})


def agent_activity_kind(event: object) -> str:
    """命令执行与工具调用在聊天记录里使用不同的展示条目"""

    return "command" if getattr(event, "type", None) is AgentEventType.COMMAND_STARTED else "tool"


def agent_item_id(event: object) -> str:
    """Codex 条目标识可能缺失，聊天条目按标识分开时用它对齐同一条目"""

    value = getattr(event, "item_id", None)
    return value if isinstance(value, str) else ""


def agent_thinking_id(event: object) -> str:
    """同一条 Codex 推理条目收敛成聊天里的一个过程条目"""

    item = agent_item_id(event)
    return f"{item}:thinking" if item else ""


def agent_activity_title(event: object) -> str:
    """优先展示真实命令或工具名，缺失时退回可读状态"""

    payload = getattr(event, "payload", None) or {}
    for key in ("command", "tool"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_ACTIVITY_TITLE_CHARS]
    return str(payload.get("message", "Agent 正在执行操作"))[:_MAX_ACTIVITY_TITLE_CHARS]


def agent_activity_status(event: object) -> str:
    """把 Codex 的条目终态收敛为聊天条目使用的两种取值"""

    payload = getattr(event, "payload", None) or {}
    status = str(payload.get("status", "completed")).casefold()
    return "failed" if status in _FAILED_ACTIVITY_STATUSES else "completed"


def simplify_asr_text(text: str) -> str:
    """将语音识别结果统一转换为简体中文"""

    try:
        from opencc import OpenCC
    except ImportError:
        return text
    return OpenCC("t2s").convert(text)


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
        memory_context: MemoryContextProvider | None = None,
        session_context: SessionContextProvider | None = None,
        session_archive: SessionArchiveProvider | None = None,
        archive_completion: Callable[[str, str], None] | None = None,
        memory_operations: MemoryOperationService | None = None,
        llm_instructions: str = "你是 VoicePet 桌面助手",
        speech_synthesizer: SpeechSynthesizer | None = None,
        audio_player: AudioPlayer | None = None,
        speech_enabled: bool = True,
        manual_input_speech_enabled: bool = False,
        wake_keyword: str = "你好，小蓝",
        continuous_conversation: bool = False,
        followup_timeout: float = 8.0,
        agent_gateway: object | None = None,
        max_turn_duration: float = 120.0,
        max_turn_tokens: int = 16_384,
        clock: Callable[[], float] | None = None,
        shutdown_timeout: float = 2.0,
        is_foreground: Callable[[], bool] | None = None,
        speech_lock: asyncio.Lock | None = None,
    ) -> None:
        budget_limits = TurnBudgetLimits(
            max_duration=max_turn_duration,
            max_tokens=max_turn_tokens,
        )
        if type(speech_enabled) is not bool:
            raise TypeError("语音播报开关必须是布尔值")
        if type(manual_input_speech_enabled) is not bool:
            raise TypeError("手动输入语音播报开关必须是布尔值")
        normalize_wake_keyword(wake_keyword)
        if followup_timeout <= 0:
            raise ValueError("连续对话等待时间必须大于零")
        self._audio_session = audio_session
        self._transcript_adapter = transcript_adapter
        self._event_bus = event_bus
        self._session_id = (
            session_context.session_id if session_context is not None else ""
        )
        self._state_machine = state_machine or ConversationStateMachine(
            self._session_id
        )
        # 并行会话里只有前台会话可以发声和驱动桌宠，后台会话静默跑完
        self._is_foreground = is_foreground
        self._speech_lock = speech_lock
        self._agent_gateway = agent_gateway
        self._memory_context = memory_context
        self._session_context = session_context
        self._session_archive = session_archive
        self._archive_completion = archive_completion
        self._memory_operations = memory_operations
        self._llm_instructions = llm_instructions
        self._speech_synthesizer = speech_synthesizer
        self._audio_player = audio_player
        self._speech_enabled = speech_enabled
        self._manual_input_speech_enabled = manual_input_speech_enabled
        self._active_input_is_manual = False
        self._wake_keyword = wake_keyword
        self._continuous_conversation = continuous_conversation
        self._followup_timeout = followup_timeout
        self._active_activation_source = "click"
        self._budget_limits = budget_limits
        self._clock = clock or time.monotonic
        self._turn_budget: TurnBudget | None = None
        self._shutdown_timeout = shutdown_timeout
        self._tasks: dict[asyncio.Task[None], CancellationSource] = {}
        self._active_source: CancellationSource | None = None
        self._response_text = ""
        self._active_input_text = ""
        self._active_attachments: tuple[LlmAttachment, ...] = ()
        self._active_session_id: str | None = None
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_memory: PendingMemoryApproval | None = None
        self._approval_lock = asyncio.Lock()
        self._stopped = False

    def is_current(self, turn_id: TurnId) -> bool:
        return not self._stopped and self._state_machine.turn_id == turn_id

    def _foreground(self) -> bool:
        """没有前台判定的旧装配始终按前台处理"""

        return self._is_foreground is None or bool(self._is_foreground())

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

    async def start_listening(self, source: str = "click") -> TurnId:
        self._ensure_running()
        activation_source = self._validate_activation_source(source)
        self._active_activation_source = activation_source
        self._response_text = ""
        self._active_input_text = ""
        self._active_attachments = ()
        self._active_input_is_manual = False
        self._active_session_id = (
            self._session_context.session_id
            if self._session_context is not None
            else None
        )
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_memory = None
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
        self._active_activation_source = "manual_text"
        self._response_text = ""
        self._active_input_text = normalized
        self._active_attachments = tuple(attachments)
        self._active_input_is_manual = True
        self._active_session_id = (
            self._session_context.session_id
            if self._session_context is not None
            else None
        )
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_memory = None
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
            if self._agent_gateway is not None:
                tasks = tuple(task for task, source in self._tasks.items()
                              if source is self._active_source and task is not asyncio.current_task())
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        finished = self._state_machine.reset(CorrelationId.new())
        self._active_source = None
        self._pending_memory = None
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
        normalized = markdown_to_speech_text(text) if isinstance(text, str) else ""
        if not normalized or len(normalized) > 200_000:
            raise ValueError("提示文本为空或过长")
        if self._speech_synthesizer is None or self._audio_player is None:
            raise RuntimeError("语音提示当前不可用")
        if self.phase is not ConversationPhase.IDLE:
            raise RuntimeError("当前正在处理其他语音操作")

        self._response_text = normalized
        self._active_activation_source = "notice"
        self._active_input_text = ""
        self._active_input_is_manual = False
        self._active_session_id = None
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_memory = None
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
        self._active_activation_source = activation_source
        if self._active_source is not None:
            self._active_source.cancel("user_interrupt")

        self._response_text = ""
        self._active_input_text = ""
        self._active_input_is_manual = False
        self._active_attachments = ()
        self._active_session_id = (
            self._session_context.session_id
            if self._session_context is not None
            else None
        )
        self._session_turn_archived = False
        self._persistent_turn_archived = False
        self._pending_memory = None
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
        self._pending_memory = None

    async def approve_pending(self) -> None:
        self._ensure_running()
        async with self._approval_lock:
            memory_pending = self._pending_memory
            if memory_pending is None:
                raise RuntimeError("当前没有待确认的操作")
            self._pending_memory = None
            await self._execute_memory_operation(memory_pending)

    async def reject_pending(self) -> None:
        self._ensure_running()
        async with self._approval_lock:
            memory_pending = self._pending_memory
            if memory_pending is None:
                raise RuntimeError("当前没有待确认的操作")
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
                    self._session_id,
                )
            )
            # 用户拒绝不是执行失败，结束原轮次后恢复待机
            if not self._accept_result(
                memory_pending.turn_id,
                memory_pending.source.token,
            ):
                return
            await self.cancel_active_turn()

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

                if activation_source == "wake_followup":
                    try:
                        audio = await asyncio.wait_for(
                            self._await_with_budget(record),
                            timeout=self._followup_timeout,
                        )
                    except TimeoutError:
                        audio = b""
                else:
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
                simplify_asr_text(text),
                source=activation_source,
                keyword=self._wake_keyword,
            )
            if activation_source == "wake_followup" and normalized_text in {
                "再见", "结束对话", "退出", "不用了", "停止对话",
            }:
                normalized_text = ""
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

    async def _run_agent_turn(self, turn_id: TurnId, text: str, token: CancellationToken, *, attachments: tuple[LlmAttachment, ...] = ()) -> None:
        """把 Agent 面向用户的文本事件接入现有聊天和语音链路"""
        correlation_id = CorrelationId.new()
        chunks: list[str] = []
        stream = None
        try:
            memory_history = ()
            if self._memory_context is not None:
                memory_history = await self._await_with_budget(
                    lambda: asyncio.to_thread(self._memory_context.build_history, text)
                )
            if not self._accept_result(turn_id, token):
                return
            agent_attachments = tuple(
                {"name": item.name, "path": item.path, "media_type": item.media_type, "kind": item.kind}
                for item in attachments
            )
            stream = self._agent_gateway.run_turn(
                self._active_session_id or "default",
                text,
                turn_id=str(turn_id),
                attachments=agent_attachments,
                context="\n\n".join(str(item["content"]) for item in memory_history),
            )
            completed = False
            activity_id = ""
            thinking_id = ""
            while True:
                try:
                    event = await self._await_with_budget(lambda: anext(stream))
                except StopAsyncIteration:
                    break
                if token.is_cancelled:
                    await self._agent_gateway.cancel(str(turn_id))
                    return
                if event.type is AgentEventType.TEXT_DELTA:
                    value = str(event.payload.get("text", ""))
                    chunks.append(value)
                    await self._event_bus.publish(TextDelta(turn_id, correlation_id, value, agent_item_id(event), self._session_id))
                elif event.type is AgentEventType.REASONING_DELTA:
                    # Agent 的中间过程文字独立成条，命令输出保留在各自的条目里
                    value = str(event.payload.get("text", ""))
                    if value:
                        current = agent_thinking_id(event) or thinking_id or f"{turn_id}:thinking"
                        if current != thinking_id:
                            thinking_id = current
                            await self._event_bus.publish(
                                AgentActivityStarted(
                                    turn_id,
                                    correlation_id,
                                    thinking_id,
                                    "thinking",
                                    "正在思考",
                                    self._session_id,
                                )
                            )
                        await self._event_bus.publish(
                            AgentActivityOutput(
                                turn_id,
                                correlation_id,
                                thinking_id,
                                value,
                                self._session_id,
                            )
                        )
                elif event.type is AgentEventType.REASONING_COMPLETED:
                    if thinking_id:
                        await self._event_bus.publish(
                            AgentActivityCompleted(
                                turn_id,
                                correlation_id,
                                thinking_id,
                                "completed",
                                self._session_id,
                            )
                        )
                        thinking_id = ""
                elif event.type in {AgentEventType.COMMAND_STARTED, AgentEventType.TOOL_STARTED}:
                    # 每个工具条目拥有独立标识，聊天记录里各自成条
                    activity_id = agent_item_id(event) or f"{turn_id}:{getattr(event, 'seq', 0)}"
                    await self._event_bus.publish(
                        AgentActivityStarted(
                            turn_id,
                            correlation_id,
                            activity_id,
                            agent_activity_kind(event),
                            agent_activity_title(event),
                            self._session_id,
                        )
                    )
                    if self._foreground():
                        await self._event_bus.publish(
                            AgentProgress(
                                turn_id,
                                correlation_id,
                                str(event.payload.get("message", "Agent 正在执行操作")),
                            )
                        )
                elif event.type is AgentEventType.COMMAND_OUTPUT_DELTA:
                    await self._event_bus.publish(
                        AgentActivityOutput(
                            turn_id,
                            correlation_id,
                            agent_item_id(event) or activity_id or f"{turn_id}:{getattr(event, 'seq', 0)}",
                            str(event.payload.get("text", "")),
                            self._session_id,
                        )
                    )
                elif event.type in {AgentEventType.COMMAND_COMPLETED, AgentEventType.TOOL_COMPLETED}:
                    if activity_id:
                        await self._event_bus.publish(
                            AgentActivityCompleted(
                                turn_id,
                                correlation_id,
                                agent_item_id(event) or activity_id,
                                agent_activity_status(event),
                                self._session_id,
                            )
                        )
                    if self._foreground():
                        await self._event_bus.publish(
                            AgentProgress(
                                turn_id,
                                correlation_id,
                                str(event.payload.get("message", "操作已完成")),
                            )
                        )
                elif event.type is AgentEventType.FILE_CHANGE and activity_id:
                    await self._event_bus.publish(
                        AgentActivityCompleted(
                            turn_id,
                            correlation_id,
                            agent_item_id(event) or activity_id,
                            agent_activity_status(event),
                            self._session_id,
                        )
                    )
                if event.type is AgentEventType.APPROVAL_REQUEST:
                    approval_id = str(event.payload.get("request_id", ""))
                    if approval_id:
                        raw_options = event.payload.get("options", ())
                        options = tuple(item for item in raw_options if isinstance(item, Mapping)) if isinstance(raw_options, (list, tuple)) else ()
                        await self._event_bus.publish(AgentApprovalRequested(turn_id, correlation_id, approval_id, str(event.payload.get("message", "Agent 请求确认")), options))
                if event.type is AgentEventType.TURN_COMPLETED:
                    if event.payload.get("status", "completed") != "completed":
                        raise RuntimeError("Agent 轮次未成功完成")
                    completed = True
                    break
                if event.type in {AgentEventType.ERROR, AgentEventType.CANCELLED}:
                    raise RuntimeError("Agent 轮次中断")
            if thinking_id:
                # 轮次正常结束时兜底收尾，缺失条目终态的推理过程不会停在执行中
                await self._event_bus.publish(
                    AgentActivityCompleted(
                        turn_id,
                        correlation_id,
                        thinking_id,
                        "completed",
                        self._session_id,
                    )
                )
                thinking_id = ""
            if not completed:
                raise RuntimeError("Agent 流缺少完成事件")
            if not self._accept_result(turn_id, token):
                return
            response = "".join(chunks)
            self._response_text = response
            if response and self._session_context is not None:
                self._session_context.add_turn(text, response)
            if response and self._session_archive is not None:
                await self._archive_completed_turn(turn_id, text, response, self._active_session_id, token)
            if not self._accept_result(turn_id, token):
                return
            if (response and self._speech_enabled_for_active_turn()
                    and self._speech_synthesizer is not None and self._audio_player is not None):
                await self._run_tts(turn_id, split_speech_text(markdown_to_speech_text(response)), token)
            else:
                finished = self._state_machine.reset(correlation_id)
                if finished is not None and not self._stopped:
                    await self._event_bus.publish(finished)
            if (
                self._continuous_conversation
                and self._active_activation_source in {"wake_word", "wake_followup"}
                and not self._stopped
            ):
                await self.start_listening("wake_followup")
        except asyncio.CancelledError:
            await self._agent_gateway.cancel(str(turn_id))
        except Exception as error:  # noqa: BLE001 Agent 失败只显示安全错误
            await self._handle_runtime_error(turn_id, correlation_id, token, error)
        finally:
            if stream is not None:
                await stream.aclose()

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
            lambda: self._play_audio(audio, token)
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
                lambda: self._play_audio(audio, token)
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
        # 全自动模式下，用户明确说“记住/忘记”时直接执行本地记忆操作；
        # 其它模式保留隐私确认弹窗。
        full_auto = False
        if self._agent_gateway is not None:
            try:
                from .agent_types import AgentApprovalMode
                full_auto = self._agent_gateway.global_mode() is AgentApprovalMode.FULL_AUTO
            except (AttributeError, TypeError, ValueError):
                full_auto = False
        if operation.requires_confirmation and not full_auto:
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
                    self._session_id,
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
                self._session_id,
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
                    lambda audio=audio: self._play_audio(audio, token)
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
        return self._foreground() and self._speech_enabled and (
            not self._active_input_is_manual
            or self._manual_input_speech_enabled
        )

    async def _play_audio(self, audio: SynthesizedAudio, token: CancellationToken) -> None:
        """并行会话共享同一个音频设备，播放阶段必须串行"""

        assert self._audio_player is not None
        lock = self._speech_lock
        if lock is None:
            await self._audio_player.play(audio, token)
            return
        async with lock:
            await self._audio_player.play(audio, token)

    async def _handle_runtime_error(
        self,
        turn_id: TurnId,
        correlation_id: CorrelationId,
        token: CancellationToken,
        error: AudioError | AsrError | TtsError | TurnBudgetExceededError,
        *,
        enter_recovery: bool = True,
    ) -> None:
        if not self._accept_result(turn_id, token):
            return
        await self._event_bus.publish(
            runtime_error_event(
                turn_id,
                correlation_id,
                error,
                session_id=self._session_id,
            )
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

    def _accept_result(
        self,
        turn_id: TurnId,
        token: CancellationToken,
    ) -> bool:
        if token.is_cancelled:
            return False
        return self.is_current(turn_id)
