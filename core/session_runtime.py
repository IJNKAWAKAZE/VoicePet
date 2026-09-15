"""按会话维护进程内上下文、记忆装配和轮次协调器"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .events import ConversationPhase
from .llm import LlmAttachment
from .session_context import SessionContext


class SessionCoordinator(Protocol):
    """注册表只依赖协调器的最小对外接口"""

    @property
    def phase(self) -> ConversationPhase: ...

    async def submit_text(
        self, text: str, attachments: tuple[LlmAttachment, ...] = ()
    ) -> object: ...


CoordinatorBuilder = Callable[..., Any]
MemoryContextBuilder = Callable[[SessionContext], Any]


class SessionCoordinatorPool:
    """按会话并发运行协调器，并把语音和桌宠留给当前前台会话"""

    def __init__(
        self,
        coordinator_builder: CoordinatorBuilder,
        *,
        memory_context_builder: MemoryContextBuilder | None = None,
        context_factory: Callable[[], SessionContext] = SessionContext,
        speech_enabled: bool = True,
        manual_input_speech_enabled: bool = False,
    ) -> None:
        self._coordinator_builder = coordinator_builder
        self._memory_context_builder = memory_context_builder
        self._context_factory = context_factory
        self._contexts: dict[str, SessionContext] = {}
        self._coordinators: dict[str, Any] = {}
        self._memory_contexts: dict[str, Any] = {}
        self._memory_enabled = True
        self._speech_enabled = bool(speech_enabled)
        self._manual_input_speech_enabled = bool(manual_input_speech_enabled)
        self._current = self._start_context()

    # ---- 会话上下文边界：SessionDataManager 沿用单上下文接口 ----

    @property
    def session_id(self) -> str:
        return self._current.session_id

    @property
    def current_session_id(self) -> str:
        """返回语音和文字共同使用的当前会话"""

        return self._current.session_id

    def build_history(self) -> tuple[Any, ...]:
        return self._current.build_history()

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        self._current.add_turn(user_text, assistant_text)

    def activate(
        self,
        session_id: str,
        turns: tuple[tuple[str, str], ...],
    ) -> None:
        """切换前台会话并恢复它的会话上下文"""

        context = self._ensure_session(session_id)
        context.activate(session_id, turns)
        self._current = context

    def clear(self) -> None:
        """结束当前会话并在同一位置创建空白会话"""

        self._forget(self._current.session_id)
        self._current = self._start_context()

    def discard(self, session_id: str) -> None:
        """删除会话后释放它的进程内状态，当前会话仍由 clear 处理"""

        self._forget(session_id)

    def reset(self) -> None:
        """丢弃全部会话状态并回到一个空白的前台会话"""

        self._contexts.clear()
        self._coordinators.clear()
        self._memory_contexts.clear()
        self._current = self._start_context()

    # ---- 前台会话与协调器 ----

    @property
    def coordinator(self) -> Any:
        return self.coordinator_for(self._current.session_id)

    def coordinator_for(self, session_id: str) -> Any:
        self._ensure_session(session_id)
        return self._coordinators[session_id]

    def is_foreground(self, session_id: str) -> bool:
        return session_id == self._current.session_id

    def session_ids(self) -> tuple[str, ...]:
        return tuple(self._contexts)

    def configure(self, enabled: bool) -> None:
        """记忆开关对所有已存在会话同时生效，并作为新会话的默认值"""

        self._memory_enabled = bool(enabled)
        for context in self._memory_contexts.values():
            apply = getattr(context, "configure", None)
            if callable(apply):
                apply(self._memory_enabled)

    async def stop(self) -> None:
        for coordinator in tuple(self._coordinators.values()):
            await coordinator.stop()

    # ---- Runtime 使用的前台服务 ----

    @property
    def phase(self) -> ConversationPhase:
        return self.coordinator.phase

    @property
    def speech_enabled(self) -> bool:
        return self._speech_enabled

    @property
    def manual_input_speech_enabled(self) -> bool:
        return self._manual_input_speech_enabled

    async def submit_text(
        self,
        text: str,
        attachments: tuple[LlmAttachment, ...] = (),
    ) -> object:
        return await self.coordinator.submit_text(text, attachments)

    async def cancel_active_turn(self, session_id: str = "") -> None:
        """只取消目标会话的轮次，缺省时取消当前前台会话"""

        await self.coordinator_for(session_id or self.session_id).cancel_active_turn()

    async def speak_notice(self, text: str) -> object:
        return await self.coordinator.speak_notice(text)

    async def capture_manual_transcript(self) -> str:
        return await self.coordinator.capture_manual_transcript()

    async def start_listening(self, source: str = "click") -> object:
        await self._release_other_recordings()
        return await self.coordinator.start_listening(source)

    async def interrupt(self, source: str = "click") -> object:
        await self._release_other_recordings()
        return await self.coordinator.interrupt(source)

    async def set_speech_enabled(self, enabled: bool) -> None:
        self._speech_enabled = bool(enabled)
        for coordinator in tuple(self._coordinators.values()):
            await coordinator.set_speech_enabled(self._speech_enabled)

    async def set_manual_input_speech_enabled(self, enabled: bool) -> None:
        self._manual_input_speech_enabled = bool(enabled)
        for coordinator in tuple(self._coordinators.values()):
            await coordinator.set_manual_input_speech_enabled(
                self._manual_input_speech_enabled
            )

    async def approve_pending(self) -> None:
        await self._pending_coordinator().approve_pending()

    async def reject_pending(self) -> None:
        await self._pending_coordinator().reject_pending()

    # ---- 内部 ----

    def _start_context(self) -> SessionContext:
        context = self._context_factory()
        self._contexts[context.session_id] = context
        return context

    def _ensure_session(self, session_id: str) -> SessionContext:
        context = self._contexts.get(session_id)
        if context is None:
            context = self._context_factory()
            context.activate(session_id, ())
            self._contexts[session_id] = context
        memory_context = self._memory_contexts.get(session_id)
        if memory_context is None:
            memory_context = self._build_memory_context(context)
            self._memory_contexts[session_id] = memory_context
        if session_id not in self._coordinators:
            self._coordinators[session_id] = self._coordinator_builder(
                session_id,
                context,
                memory_context,
                speech_enabled=self._speech_enabled,
                manual_input_speech_enabled=self._manual_input_speech_enabled,
            )
        return context

    def _build_memory_context(self, context: SessionContext) -> Any:
        if self._memory_context_builder is None:
            return None
        built = self._memory_context_builder(context)
        apply = getattr(built, "configure", None)
        if callable(apply):
            apply(self._memory_enabled)
        return built

    def _forget(self, session_id: str) -> None:
        self._contexts.pop(session_id, None)
        self._coordinators.pop(session_id, None)
        self._memory_contexts.pop(session_id, None)

    def _pending_coordinator(self) -> Any:
        for coordinator in self._coordinators.values():
            if getattr(coordinator, "_pending_memory", None) is not None:
                return coordinator
        return self.coordinator

    async def _release_other_recordings(self) -> None:
        """麦克风是独占资源，前台开始收音前先结束其他会话的录音轮次"""

        current = self.session_id
        for session_id, coordinator in tuple(self._coordinators.items()):
            if session_id == current:
                continue
            if coordinator.phase in {
                ConversationPhase.LISTENING,
                ConversationPhase.TRANSCRIBING,
            }:
                await coordinator.cancel_active_turn()
