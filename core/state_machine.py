"""不执行 I/O 的对话阶段状态机"""

from __future__ import annotations

from .events import ConversationPhase, CorrelationId, StateChanged, TurnId


class InvalidTransition(RuntimeError):
    """请求的状态变化不符合当前对话阶段"""


_LEGAL_TRANSITIONS: dict[ConversationPhase, frozenset[ConversationPhase]] = {
    ConversationPhase.IDLE: frozenset(),
    ConversationPhase.LISTENING: frozenset(
        {ConversationPhase.TRANSCRIBING, ConversationPhase.RECOVERING}
    ),
    ConversationPhase.TRANSCRIBING: frozenset(
        {ConversationPhase.THINKING, ConversationPhase.RECOVERING}
    ),
    ConversationPhase.THINKING: frozenset(
        {
            ConversationPhase.AWAITING_APPROVAL,
            ConversationPhase.EXECUTING_TOOL,
            ConversationPhase.SYNTHESIZING,
            ConversationPhase.RECOVERING,
        }
    ),
    ConversationPhase.AWAITING_APPROVAL: frozenset(
        {ConversationPhase.EXECUTING_TOOL, ConversationPhase.RECOVERING}
    ),
    ConversationPhase.EXECUTING_TOOL: frozenset(
        {ConversationPhase.THINKING, ConversationPhase.RECOVERING}
    ),
    ConversationPhase.SYNTHESIZING: frozenset(
        {ConversationPhase.SPEAKING, ConversationPhase.RECOVERING}
    ),
    ConversationPhase.SPEAKING: frozenset(
        {ConversationPhase.IDLE, ConversationPhase.RECOVERING}
    ),
    ConversationPhase.RECOVERING: frozenset({ConversationPhase.RECOVERING}),
}


class ConversationStateMachine:
    """维护一个活动轮次及其合法阶段变化"""

    def __init__(self) -> None:
        self._phase = ConversationPhase.IDLE
        self._turn_id: TurnId | None = None

    @property
    def phase(self) -> ConversationPhase:
        return self._phase

    @property
    def turn_id(self) -> TurnId | None:
        return self._turn_id

    def start_turn(self, correlation_id: CorrelationId) -> StateChanged:
        if self._phase is not ConversationPhase.IDLE:
            raise InvalidTransition(
                f"cannot start turn from {self._phase.value}"
            )

        turn_id = TurnId.new()
        self._turn_id = turn_id
        self._phase = ConversationPhase.LISTENING
        return StateChanged(
            turn_id,
            correlation_id,
            ConversationPhase.IDLE,
            ConversationPhase.LISTENING,
        )

    def start_notice(self, correlation_id: CorrelationId) -> StateChanged:
        """从空闲状态创建直接进入语音合成的提示轮次"""

        if self._phase is not ConversationPhase.IDLE:
            raise InvalidTransition(
                f"cannot start notice from {self._phase.value}"
            )
        turn_id = TurnId.new()
        self._turn_id = turn_id
        self._phase = ConversationPhase.SYNTHESIZING
        return StateChanged(
            turn_id,
            correlation_id,
            ConversationPhase.IDLE,
            ConversationPhase.SYNTHESIZING,
        )

    def start_text_turn(self, correlation_id: CorrelationId) -> StateChanged:
        """从空闲状态创建直接进入思考阶段的文本轮次"""

        if self._phase is not ConversationPhase.IDLE:
            raise InvalidTransition(
                f"cannot start text turn from {self._phase.value}"
            )
        turn_id = TurnId.new()
        self._turn_id = turn_id
        self._phase = ConversationPhase.THINKING
        return StateChanged(
            turn_id,
            correlation_id,
            ConversationPhase.IDLE,
            ConversationPhase.THINKING,
        )

    def transition(
        self,
        target: ConversationPhase,
        turn_id: TurnId,
        correlation_id: CorrelationId,
    ) -> StateChanged:
        if turn_id != self._turn_id:
            raise InvalidTransition("stale turn cannot change state")
        if target not in _LEGAL_TRANSITIONS[self._phase]:
            raise InvalidTransition(
                f"cannot transition from {self._phase.value} to {target.value}"
            )

        previous = self._phase
        event = StateChanged(turn_id, correlation_id, previous, target)
        self._phase = target
        if target is ConversationPhase.IDLE:
            self._turn_id = None
        return event

    def interrupt(self, correlation_id: CorrelationId) -> StateChanged:
        if self._phase is ConversationPhase.IDLE:
            raise InvalidTransition("cannot interrupt an idle state machine")

        previous = self._phase
        # 先替换轮次再发布事件以保证观察者只能看到新轮次
        turn_id = TurnId.new()
        self._turn_id = turn_id
        self._phase = ConversationPhase.LISTENING
        return StateChanged(
            turn_id,
            correlation_id,
            previous,
            ConversationPhase.LISTENING,
        )

    def reset(self, correlation_id: CorrelationId) -> StateChanged | None:
        if self._phase is ConversationPhase.IDLE:
            return None

        # 重置事件仍归属于被结束的活动轮次
        assert self._turn_id is not None
        event = StateChanged(
            self._turn_id,
            correlation_id,
            self._phase,
            ConversationPhase.IDLE,
        )
        self._phase = ConversationPhase.IDLE
        self._turn_id = None
        return event
