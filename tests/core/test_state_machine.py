from dataclasses import FrozenInstanceError

import pytest

from core import (
    ActivationController,
    AsrRuntimeStatus,
    AudioCaptureService,
    AudioFormat,
    CancellationSource,
    Coordinator,
    EdgeTtsSynthesizer,
    EventBus,
    FallbackSpeechSynthesizer,
    FasterWhisperTranscriptAdapter,
    LlmCompleted,
    LlmRequest,
    MockProvider,
    OpenAIResponsesProvider,
    SentenceChunker,
    SoundDeviceInputBackend,
    SynthesizedAudio,
    VadAudioSession,
    WakeWordMonitor,
    WebRtcVadDetector,
    WindowsMciAudioPlayer,
    WindowsSapiSynthesizer,
)
from core import (
    ConversationStateMachine as ExportedStateMachine,
)
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    CorrelationId,
    ErrorSeverity,
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TextInputSubmitted,
    TranscriptReady,
    TurnId,
)
from core.state_machine import ConversationStateMachine, InvalidTransition


def test_runtime_foundation_is_available_from_core_package():
    assert AudioCaptureService.__name__ == "AudioCaptureService"
    assert AudioFormat.__name__ == "AudioFormat"
    assert ActivationController.__name__ == "ActivationController"
    assert AsrRuntimeStatus.__name__ == "AsrRuntimeStatus"
    assert CancellationSource.__name__ == "CancellationSource"
    assert Coordinator.__name__ == "Coordinator"
    assert EventBus.__name__ == "EventBus"
    assert EdgeTtsSynthesizer.__name__ == "EdgeTtsSynthesizer"
    assert FallbackSpeechSynthesizer.__name__ == "FallbackSpeechSynthesizer"
    assert FasterWhisperTranscriptAdapter.__name__ == (
        "FasterWhisperTranscriptAdapter"
    )
    assert LlmCompleted.__name__ == "LlmCompleted"
    assert LlmRequest.__name__ == "LlmRequest"
    assert MockProvider.__name__ == "MockProvider"
    assert OpenAIResponsesProvider.__name__ == "OpenAIResponsesProvider"
    assert SentenceChunker.__name__ == "SentenceChunker"
    assert SoundDeviceInputBackend.__name__ == "SoundDeviceInputBackend"
    assert SynthesizedAudio.__name__ == "SynthesizedAudio"
    assert VadAudioSession.__name__ == "VadAudioSession"
    assert WakeWordMonitor.__name__ == "WakeWordMonitor"
    assert WebRtcVadDetector.__name__ == "WebRtcVadDetector"
    assert WindowsMciAudioPlayer.__name__ == "WindowsMciAudioPlayer"
    assert WindowsSapiSynthesizer.__name__ == "WindowsSapiSynthesizer"
    assert ExportedStateMachine is ConversationStateMachine


def test_identifiers_are_non_empty_and_states_are_stable():
    turn = TurnId.new()
    correlation = CorrelationId.new()

    assert str(turn)
    assert str(correlation)
    assert ConversationPhase.IDLE.value == "IDLE"
    assert ConversationPhase.AWAITING_APPROVAL.value == "AWAITING_APPROVAL"

    event = StateChanged(
        turn_id=turn,
        correlation_id=correlation,
        previous=ConversationPhase.IDLE,
        current=ConversationPhase.LISTENING,
    )
    assert event.turn_id == turn
    assert event.correlation_id == correlation


def test_shared_identifiers_and_events_are_immutable():
    turn = TurnId.new()
    correlation = CorrelationId.new()
    event = StateChanged(
        turn_id=turn,
        correlation_id=correlation,
        previous=ConversationPhase.IDLE,
        current=ConversationPhase.LISTENING,
    )

    with pytest.raises(FrozenInstanceError):
        turn.value = turn.value
    with pytest.raises(FrozenInstanceError):
        correlation.value = correlation.value
    with pytest.raises(FrozenInstanceError):
        event.current = ConversationPhase.IDLE


def test_every_runtime_event_carries_both_identifiers():
    turn = TurnId.new()
    correlation = CorrelationId.new()
    events = [
        TranscriptReady(turn, correlation, "hello"),
        TextInputSubmitted(turn, correlation, "hello"),
        TextDelta(turn, correlation, "hel"),
        ApprovalRequested(turn, correlation, "call-1", "Open app", "R1"),
        SpeakRequested(turn, correlation, "hello"),
        RuntimeErrorEvent(
            turn,
            correlation,
            "audio.device",
            "audio",
            ErrorSeverity.WARNING,
            True,
            False,
            "音频失败",
            {},
        ),
    ]

    assert all(event.turn_id == turn for event in events)
    assert all(event.correlation_id == correlation for event in events)


def advance(
    machine: ConversationStateMachine,
    turn_id: TurnId,
    target: ConversationPhase,
) -> StateChanged:
    correlation = CorrelationId.new()
    event = machine.transition(target, turn_id, correlation)
    assert event.correlation_id == correlation
    return event


def machine_at(
    phase: ConversationPhase,
) -> tuple[ConversationStateMachine, TurnId]:
    machine = ConversationStateMachine()
    turn_id = machine.start_turn(CorrelationId.new()).turn_id
    if phase is ConversationPhase.LISTENING:
        return machine, turn_id
    if phase is ConversationPhase.RECOVERING:
        advance(machine, turn_id, ConversationPhase.RECOVERING)
        return machine, turn_id

    advance(machine, turn_id, ConversationPhase.TRANSCRIBING)
    if phase is ConversationPhase.TRANSCRIBING:
        return machine, turn_id
    advance(machine, turn_id, ConversationPhase.THINKING)
    if phase is ConversationPhase.THINKING:
        return machine, turn_id
    if phase in {
        ConversationPhase.AWAITING_APPROVAL,
        ConversationPhase.EXECUTING_TOOL,
    }:
        advance(machine, turn_id, ConversationPhase.AWAITING_APPROVAL)
        if phase is ConversationPhase.EXECUTING_TOOL:
            advance(machine, turn_id, ConversationPhase.EXECUTING_TOOL)
        return machine, turn_id

    advance(machine, turn_id, ConversationPhase.SYNTHESIZING)
    if phase is ConversationPhase.SPEAKING:
        advance(machine, turn_id, ConversationPhase.SPEAKING)
    return machine, turn_id


def test_normal_turn_follows_the_complete_response_path():
    machine = ConversationStateMachine()
    start_correlation = CorrelationId.new()

    started = machine.start_turn(start_correlation)
    turn_id = started.turn_id

    assert started.previous is ConversationPhase.IDLE
    assert started.current is ConversationPhase.LISTENING
    assert started.correlation_id == start_correlation
    assert machine.turn_id == turn_id

    advance(machine, turn_id, ConversationPhase.TRANSCRIBING)
    advance(machine, turn_id, ConversationPhase.THINKING)
    advance(machine, turn_id, ConversationPhase.SYNTHESIZING)
    advance(machine, turn_id, ConversationPhase.SPEAKING)
    finished = advance(machine, turn_id, ConversationPhase.IDLE)

    assert finished.turn_id == turn_id
    assert machine.phase is ConversationPhase.IDLE
    assert machine.turn_id is None


def test_tool_path_returns_to_thinking():
    machine = ConversationStateMachine()
    turn_id = machine.start_turn(CorrelationId.new()).turn_id
    advance(machine, turn_id, ConversationPhase.TRANSCRIBING)
    advance(machine, turn_id, ConversationPhase.THINKING)
    advance(machine, turn_id, ConversationPhase.AWAITING_APPROVAL)
    advance(machine, turn_id, ConversationPhase.EXECUTING_TOOL)

    event = advance(machine, turn_id, ConversationPhase.THINKING)

    assert event.current is ConversationPhase.THINKING


def test_notice_starts_directly_in_synthesizing_only_from_idle():
    machine = ConversationStateMachine()
    correlation = CorrelationId.new()

    event = machine.start_notice(correlation)

    assert event.previous is ConversationPhase.IDLE
    assert event.current is ConversationPhase.SYNTHESIZING
    assert event.correlation_id == correlation
    assert machine.turn_id == event.turn_id
    with pytest.raises(InvalidTransition):
        machine.start_notice(CorrelationId.new())


def test_text_turn_starts_directly_in_thinking_only_from_idle():
    machine = ConversationStateMachine()
    correlation = CorrelationId.new()

    event = machine.start_text_turn(correlation)

    assert event.previous is ConversationPhase.IDLE
    assert event.current is ConversationPhase.THINKING
    assert event.correlation_id == correlation
    assert machine.turn_id == event.turn_id
    with pytest.raises(InvalidTransition):
        machine.start_text_turn(CorrelationId.new())


@pytest.mark.parametrize(
    "phase",
    [
        ConversationPhase.LISTENING,
        ConversationPhase.TRANSCRIBING,
        ConversationPhase.THINKING,
        ConversationPhase.AWAITING_APPROVAL,
        ConversationPhase.EXECUTING_TOOL,
        ConversationPhase.SYNTHESIZING,
        ConversationPhase.SPEAKING,
        ConversationPhase.RECOVERING,
    ],
)
def test_interrupt_replaces_any_active_turn(phase):
    machine, old_turn = machine_at(phase)
    correlation = CorrelationId.new()

    event = machine.interrupt(correlation)

    assert event.previous is phase
    assert event.current is ConversationPhase.LISTENING
    assert event.turn_id != old_turn
    assert event.correlation_id == correlation
    assert machine.turn_id == event.turn_id


@pytest.mark.parametrize(
    "phase",
    [
        ConversationPhase.LISTENING,
        ConversationPhase.TRANSCRIBING,
        ConversationPhase.THINKING,
        ConversationPhase.AWAITING_APPROVAL,
        ConversationPhase.EXECUTING_TOOL,
        ConversationPhase.SYNTHESIZING,
        ConversationPhase.SPEAKING,
    ],
)
def test_any_non_recovering_active_phase_can_enter_recovery(phase):
    machine, turn_id = machine_at(phase)

    event = advance(machine, turn_id, ConversationPhase.RECOVERING)

    assert event.previous is phase
    assert event.current is ConversationPhase.RECOVERING


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (ConversationPhase.IDLE, ConversationPhase.SPEAKING),
        (ConversationPhase.LISTENING, ConversationPhase.EXECUTING_TOOL),
        (ConversationPhase.AWAITING_APPROVAL, ConversationPhase.SPEAKING),
    ],
)
def test_illegal_transitions_raise(source, target):
    machine = ConversationStateMachine()
    if source is ConversationPhase.IDLE:
        with pytest.raises(InvalidTransition):
            machine.transition(target, TurnId.new(), CorrelationId.new())
        return

    machine, turn_id = machine_at(source)
    with pytest.raises(InvalidTransition):
        machine.transition(target, turn_id, CorrelationId.new())


def test_stale_turn_cannot_change_state():
    machine = ConversationStateMachine()
    current_turn = machine.start_turn(CorrelationId.new()).turn_id

    with pytest.raises(InvalidTransition, match="stale turn"):
        machine.transition(
            ConversationPhase.TRANSCRIBING,
            TurnId.new(),
            CorrelationId.new(),
        )

    assert machine.turn_id == current_turn
    assert machine.phase is ConversationPhase.LISTENING


def test_reset_returns_active_turn_to_idle_and_is_idempotent():
    machine = ConversationStateMachine()
    turn_id = machine.start_turn(CorrelationId.new()).turn_id
    correlation = CorrelationId.new()

    event = machine.reset(correlation)

    assert event is not None
    assert event.turn_id == turn_id
    assert event.correlation_id == correlation
    assert event.current is ConversationPhase.IDLE
    assert machine.phase is ConversationPhase.IDLE
    assert machine.turn_id is None
    assert machine.reset(CorrelationId.new()) is None
