from concurrent.futures import Future

import pytest
from PySide6.QtCore import QObject
from test_qml_application import build_controller, completed

from core.events import (
    ConversationPhase,
    CorrelationId,
    RecordingStarted,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TranscriptReady,
    TurnId,
)
from core.runtime_errors import runtime_error_event


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


def test_parallel_session_turn_end_clears_its_running_state(speech_app):
    controller, _runtime, chat, _pet = speech_app
    chat.activate_session("session-a")
    assert chat.activeSessionId == "session-a"

    # 会话 A 先跑起来，随后用户切到会话 B 并提交，A 转为后台继续跑
    first = TurnId.new()
    controller.handle_runtime_event(StateChanged(
        first, CorrelationId.new(), ConversationPhase.IDLE, ConversationPhase.THINKING,
        "session-a",
    ))
    chat.activate_session("session-b")
    chat.begin_turn("session-b")
    second = TurnId.new()
    controller.handle_runtime_event(StateChanged(
        second, CorrelationId.new(), ConversationPhase.IDLE, ConversationPhase.THINKING,
        "session-b",
    ))
    assert chat.processing is True
    assert chat.anyProcessing is True

    # 新会话先结束，后台的旧会话随后才结束，两边都要收掉自己的运行中标记
    controller.handle_runtime_event(StateChanged(
        second, CorrelationId.new(), ConversationPhase.THINKING, ConversationPhase.IDLE,
        "session-b",
    ))
    controller.handle_runtime_event(StateChanged(
        first, CorrelationId.new(), ConversationPhase.THINKING, ConversationPhase.IDLE,
        "session-a",
    ))

    assert chat.anyProcessing is False


def test_switching_to_running_session_restores_pet_state(qapp):
    controller, _runtime, tray, qml, _shell, chat, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        animation = pet.property("animation")
        first = TurnId.new()
        chat.activate_session("session-a")
        controller.handle_runtime_event(StateChanged(
            first, CorrelationId.new(), ConversationPhase.IDLE, ConversationPhase.THINKING,
            "session-a",
        ))
        assert animation.row == 7

        # 会话 A 转入后台后切到会话 B，桌宠要跟着当前会话变成执行状态
        chat.activate_session("session-b")
        second = TurnId.new()
        controller.handle_runtime_event(StateChanged(
            second, CorrelationId.new(), ConversationPhase.IDLE, ConversationPhase.EXECUTING_TOOL,
            "session-b",
        ))
        assert animation.row == 4

        # A 在后台继续推进状态时桌宠不受影响，B 结束后先回到空闲
        controller.handle_runtime_event(StateChanged(
            first, CorrelationId.new(), ConversationPhase.THINKING, ConversationPhase.AWAITING_APPROVAL,
            "session-a",
        ))
        assert animation.row == 4
        controller.handle_runtime_event(StateChanged(
            second, CorrelationId.new(), ConversationPhase.EXECUTING_TOOL, ConversationPhase.IDLE,
            "session-b",
        ))
        assert animation.row == 0
        assert tray.listening is False

        # 切回仍在等待确认的 A 时，桌宠与托盘要恢复该会话的状态而不是停在空闲
        chat.activate_session("session-a")

        assert animation.row == 6
        assert tray.listening is True
    finally:
        controller.close()


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


def test_history_playback_displays_spoken_text_and_keeps_it_after_completion(speech_app):
    controller, _, chat, pet = speech_app
    reply(controller, "上一轮回复")
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.SYNTHESIZING,
    ))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.SYNTHESIZING, ConversationPhase.SPEAKING,
    ))
    controller.handle_runtime_event(SpeakRequested(turn, correlation, "这条聊天记录正在播放"))
    assert pet.property("speech") == "这条聊天记录正在播放"
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.SPEAKING, ConversationPhase.IDLE,
    ))
    assert pet.property("speech") == "这条聊天记录正在播放"
    assert chat.messageModel.rowCount() == 1


def test_reply_playback_shows_each_spoken_segment_and_ignores_stale_speech(speech_app):
    controller, _, _, pet = speech_app
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.THINKING,
    ))
    controller.handle_runtime_event(TextDelta(turn, correlation, "第一句。第二句。"))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.THINKING, ConversationPhase.SYNTHESIZING,
    ))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.SYNTHESIZING, ConversationPhase.SPEAKING,
    ))
    controller.handle_runtime_event(SpeakRequested(turn, correlation, "第一句。"))
    controller.handle_runtime_event(RecordingStarted(turn, correlation))
    controller.handle_runtime_event(SpeakRequested(TurnId.new(), correlation, "旧轮次提示"))
    controller.handle_runtime_event(SpeakRequested(turn, correlation, "第二句。"))

    # 播报推进到哪一段气泡就显示哪一段，旧轮次的播报不能覆盖
    assert pet.property("speech") == "第二句。"


def test_normal_reply_equal_to_wake_acknowledgement_is_not_replaced(speech_app):
    controller, _, _, pet = speech_app
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.SYNTHESIZING,
    ))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.SYNTHESIZING, ConversationPhase.SPEAKING,
    ))
    controller.handle_runtime_event(SpeakRequested(turn, correlation, "我在，请说"))
    assert pet.property("speech") == "我在，请说"


def test_delta_from_new_turn_never_appends_previous_turn_text(speech_app):
    controller, _, _, pet = speech_app
    reply(controller, "旧回复")
    controller.handle_runtime_event(TextDelta(TurnId.new(), CorrelationId.new(), "新回复"))
    assert pet.property("speech") == "新回复"


@pytest.mark.parametrize("operation", ["switch", "new"])
def test_background_voice_turn_finishes_without_touching_the_pet(speech_app, operation):
    controller, runtime, chat, pet = speech_app
    chat.activate_session("voice-session")
    session_id = chat.activeSessionId
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.LISTENING, session_id,
    ))
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.TRANSCRIBING, ConversationPhase.THINKING, session_id,
    ))
    assert chat.processing is True

    runtime.new_session = lambda: completed("new-session")
    if operation == "switch":
        chat.activate_session("other-session")
        assert chat.activeSessionId == "other-session"
    else:
        chat.new_session()
        assert chat.activeSessionId == "new-session"
    assert chat.processing is False
    assert pet.property("speech") == ""

    # 后台会话静默跑完，不驱动桌宠气泡
    controller.handle_runtime_event(
        TextDelta(turn, correlation, "语音回复", "", session_id)
    )
    assert pet.property("speech") == ""
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.THINKING, ConversationPhase.IDLE, session_id,
    ))

    # 切回原会话能看到后台跑完的完整回复
    chat.activate_session("voice-session")
    assert chat.activeSessionId == "voice-session"
    assert [item["markdown"] for item in chat.messageModel._items] == ["语音回复"]


def test_background_session_error_stays_silent_and_keeps_the_reason(speech_app):
    controller, runtime, chat, pet = speech_app
    chat.activate_session("voice-session")
    session_id = chat.activeSessionId
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.THINKING, session_id,
    ))
    runtime.new_session = lambda: completed("new-session")
    chat.new_session()

    controller.handle_runtime_event(
        runtime_error_event(turn, correlation, RuntimeError("boom"), session_id=session_id)
    )

    # 后台失败不打断前台，也不驱动桌宠气泡
    assert pet.property("speech") == ""
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.RECOVERING, ConversationPhase.IDLE, session_id,
    ))

    # 切回原会话能看到失败原因
    chat.activate_session("voice-session")
    assert [item["markdown"] for item in chat.messageModel._items] == [
        "运行时操作失败，请运行诊断检查"
    ]


def test_voice_transcript_is_recorded_in_the_active_session(speech_app):
    controller, _runtime, chat, _pet = speech_app
    chat.activate_session("voice-session")
    session_id = chat.activeSessionId
    turn, correlation = TurnId.new(), CorrelationId.new()
    controller.handle_runtime_event(StateChanged(
        turn, correlation, ConversationPhase.IDLE, ConversationPhase.LISTENING, session_id,
    ))
    controller.handle_runtime_event(
        TranscriptReady(turn, correlation, "你好小蓝", session_id)
    )

    # 语音转写必须落到发起它的会话，否则点桌宠说话看不到自己的提问
    assert [item["markdown"] for item in chat.messageModel._items] == ["你好小蓝"]
