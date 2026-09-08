from concurrent.futures import Future

import pytest
from PySide6.QtCore import QObject
from test_qml_application import build_controller, completed

from core.events import (
    ConversationPhase,
    CorrelationId,
    StateChanged,
    TextDelta,
    TurnId,
)


@pytest.fixture
def speech_app(qapp):
    controller, runtime, _tray, qml, _shell, chat, *_ = build_controller(qapp)
    controller.start()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")
    yield controller, runtime, chat, pet
    controller.close()


def reply(controller, text):
    turn = TurnId.new()
    correlation = CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.THINKING,
    ))
    controller.handle_runtime_event(TextDelta(turn, correlation, text))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.THINKING, ConversationPhase.IDLE,
    ))
    return turn


def test_manual_turn_replaces_previous_reply(speech_app):
    controller, _, _, pet = speech_app
    reply(controller, "上一轮回复")
    reply(controller, "本轮回复")
    assert pet.property("speech") == "本轮回复"


def test_long_reply_is_not_truncated_in_pet_bubble(speech_app):
    controller, _, _, pet = speech_app
    long_reply = "完整回复" * 200

    reply(controller, long_reply)

    assert pet.property("speech") == long_reply


def test_new_manual_turn_clears_bubble_before_first_delta(speech_app):
    controller, _, _, pet = speech_app
    reply(controller, "上一轮回复")
    controller.handle_runtime_event(StateChanged(
        TurnId.new(), CorrelationId.new(), ConversationPhase.IDLE, ConversationPhase.THINKING,
    ))
    assert pet.property("speech") == ""


@pytest.mark.parametrize("operation", ["switch", "new"])
def test_session_change_hides_bubble_and_starts_fresh(speech_app, operation):
    controller, runtime, chat, pet = speech_app
    reply(controller, "旧会话回复")
    runtime.new_session = lambda: completed("new-session")
    if operation == "switch":
        chat.activate_session("other-session")
    else:
        chat.new_session()
    assert pet.property("speech") == ""
    reply(controller, "新会话回复")
    assert pet.property("speech") == "新会话回复"


def test_submit_hides_old_reply_while_runtime_accepts_request(speech_app):
    controller, _, chat, pet = speech_app
    reply(controller, "旧回复")
    chat.submit("下一问")
    assert pet.property("speech") == ""


def test_failed_session_switch_keeps_current_bubble(speech_app):
    controller, runtime, chat, pet = speech_app
    reply(controller, "当前回复")
    failure = Future()
    failure.set_exception(ValueError("missing session"))
    runtime.activate_session = lambda session_id: failure
    chat.activate_session("missing")
    assert pet.property("speech") == "当前回复"


def test_same_turn_phase_change_keeps_streaming_text(speech_app):
    controller, _, _, pet = speech_app
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.THINKING,
    ))
    controller.handle_runtime_event(TextDelta(turn, correlation, "第一段"))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.THINKING, ConversationPhase.SYNTHESIZING,
    ))
    controller.handle_runtime_event(TextDelta(turn, correlation, "第二段"))
    assert pet.property("speech") == "第一段第二段"


def test_delta_from_new_turn_never_appends_previous_turn_text(speech_app):
    controller, _, _, pet = speech_app
    reply(controller, "旧回复")
    controller.handle_runtime_event(TextDelta(TurnId.new(), CorrelationId.new(), "新回复"))
    assert pet.property("speech") == "新回复"


@pytest.mark.parametrize("operation", ["switch", "new"])
def test_voice_waiting_for_first_delta_keeps_session_locked(speech_app, operation):
    controller, runtime, chat, pet = speech_app
    chat.activate_session("voice-session")
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.LISTENING,
    ))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.TRANSCRIBING, ConversationPhase.THINKING,
    ))
    runtime.new_session = lambda: completed("new-session")
    if operation == "switch":
        chat.activate_session("other-session")
    else:
        chat.new_session()
    assert chat.activeSessionId == "voice-session"
    assert chat.processing
    controller.handle_runtime_event(TextDelta(turn, correlation, "语音回复"))
    assert pet.property("speech") == "语音回复"
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.THINKING, ConversationPhase.IDLE,
    ))
    assert not chat.processing
    chat.activate_session("other-session")
    assert chat.activeSessionId == "other-session"
    assert pet.property("speech") == ""
