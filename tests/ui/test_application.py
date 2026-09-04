import os
from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

import ui.application as application_module
from core.asr import AsrModelState
from core.audio_types import AudioDeviceError
from core.config import AppConfig, ConfigError, ConfigLoadStatus, ConfigStore
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    CorrelationId,
    MemoryResultReady,
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TextInputSubmitted,
    ToolResultReady,
    TranscriptReady,
    TurnId,
)
from core.policy import ConfirmationMode
from core.runtime_errors import runtime_error_event
from core.state_machine import InvalidTransition
from core.tts import TtsPlaybackError, TtsVoiceOption
from core.wake import WakeRuntimeStatus
from core.wake_models import WakeDownloadProgress, WakeModelState
from ui.application import ApplicationController, run_ui
from ui.event_bridge import QtEventBridge
from ui.pet_shell import PetChoice
from ui.settings_window import SettingsWindow


def app_instance():
    return QApplication.instance() or QApplication([])


def test_model_download_dialog_has_visible_chinese_actions():
    app_instance()
    parent = SettingsWindow(AppConfig())

    dialog = application_module._build_model_download_dialog(
        parent,
        title="准备语音模型",
        message="需要下载模型",
    )

    assert dialog.windowTitle() == "准备语音模型"
    assert dialog.text() == "需要下载模型"
    assert dialog.button(QMessageBox.StandardButton.Yes).text() == "下载模型"
    assert dialog.button(QMessageBox.StandardButton.No).text() == "取消"
    assert "QMessageBox" in dialog.styleSheet()
    assert "background: #081827" in dialog.styleSheet()
    dialog.close()
    parent.close()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (QMessageBox.StandardButton.Yes, True),
        (QMessageBox.StandardButton.No, False),
    ],
)
def test_model_download_confirmation_maps_standard_button(
    monkeypatch,
    result,
    expected,
):
    app_instance()
    parent = SettingsWindow(AppConfig())
    monkeypatch.setattr(QMessageBox, "exec", lambda dialog: int(result))

    confirmed = application_module._confirm_model_download(
        parent,
        title="准备语音模型",
        message="需要下载模型",
    )

    assert confirmed is expected
    parent.close()


def test_session_clear_dialog_has_readable_chinese_danger_action():
    application = app_instance()
    parent = SettingsWindow(AppConfig())

    dialog = application_module._build_session_clear_dialog(parent)

    assert dialog.windowTitle() == "清空全部会话"
    assert "永久删除" in dialog.text()
    clear_button = dialog.button(QMessageBox.StandardButton.Yes)
    cancel_button = dialog.button(QMessageBox.StandardButton.No)
    assert clear_button.text() == "清空全部"
    assert clear_button.objectName() == "danger_action"
    assert cancel_button.text() == "取消"
    assert dialog.defaultButton() is cancel_button
    assert "background: #081827" in dialog.styleSheet()
    assert "#danger_action" in dialog.styleSheet()
    dialog.show()
    application.processEvents()
    icon_label = dialog.findChild(QLabel, "qt_msgboxex_icon_label")
    text_label = dialog.findChild(QLabel, "qt_msgbox_label")
    assert icon_label is not None
    assert text_label is not None
    assert icon_label.width() <= 64
    assert text_label.width() <= 460
    assert dialog.width() < 600
    dialog.close()
    parent.close()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (QMessageBox.StandardButton.Yes, True),
        (QMessageBox.StandardButton.No, False),
    ],
)
def test_session_clear_confirmation_maps_standard_button(
    monkeypatch,
    result,
    expected,
):
    app_instance()
    parent = SettingsWindow(AppConfig())
    monkeypatch.setattr(QMessageBox, "exec", lambda dialog: int(result))

    assert application_module._confirm_session_clear(parent) is expected
    parent.close()


def test_session_delete_dialog_has_chinese_safe_default():
    app_instance()
    parent = SettingsWindow(AppConfig())

    dialog = application_module._build_session_delete_dialog(parent)

    delete_button = dialog.button(QMessageBox.StandardButton.Yes)
    cancel_button = dialog.button(QMessageBox.StandardButton.No)
    assert dialog.windowTitle() == "删除会话"
    assert delete_button.text() == "删除会话"
    assert delete_button.objectName() == "danger_action"
    assert cancel_button.text() == "取消"
    assert dialog.defaultButton() is cancel_button
    dialog.close()
    parent.close()


class FakeRuntimeHost:
    def __init__(self):
        self.started = 0
        self.activations = []
        self.approvals = []
        self.rejections = 0
        self.memory_records = ()
        self.deleted_memories = []
        self.confirmed_memories = []
        self.resolved_memories = []
        self.exported_memories = []
        self.pet_sources = []
        self.installed_pet = None
        self.diagnostic_report = None
        self.diagnostic_exports = []
        self.closed = 0
        self.llm_configured = True
        self.asr_state = AsrModelState.READY
        self.asr_state_future = None
        self.download_future = Future()
        self.download_future.set_result(None)
        self.load_future = Future()
        self.load_future.set_result(None)
        self.notice_future = Future()
        self.notice_future.set_result(TurnId.new())
        self.asr_state_checks = 0
        self.downloads = 0
        self.loads = 0
        self.notices = []
        self.speech_toggles = []
        self.submitted_texts = []
        self.submit_text_future = None
        self.session_records = ()
        self.deleted_sessions = []
        self.cleared_sessions = 0
        self.active_session_id = str(TurnId.new())
        self.session_turns = {}
        self.activated_sessions = []
        self.new_sessions = 0
        self.tts_voices = ()
        self.tts_voice_checks = 0
        self.tts_voice_previews = []
        self.wake_state = WakeModelState.READY
        self.wake_status = WakeRuntimeStatus.LISTENING
        self.wake_state_checks = 0
        self.wake_configurations = []
        self.wake_downloads = 0
        self.wake_download_cancelled = 0
        self.wake_progress_callback = None
        self.wake_download_future = Future()
        self.wake_download_future.set_result(None)

    def start(self):
        self.started += 1

    def activate(self, source):
        self.activations.append(source)
        future = Future()
        future.set_result(TurnId.new())
        return future

    def speak_notice(self, text):
        self.notices.append(text)
        return self.notice_future

    def set_speech_enabled(self, enabled):
        self.speech_toggles.append(enabled)
        future = Future()
        future.set_result(None)
        return future

    def submit_text(self, text):
        self.submitted_texts.append(text)
        if self.submit_text_future is not None:
            return self.submit_text_future
        future = Future()
        future.set_result(TurnId.new())
        return future

    def list_tts_voices(self):
        self.tts_voice_checks += 1
        future = Future()
        future.set_result(self.tts_voices)
        return future

    def preview_tts_voice(self, voice_name):
        self.tts_voice_previews.append(voice_name)
        future = Future()
        future.set_result(None)
        return future

    def wake_model_state(self):
        self.wake_state_checks += 1
        future = Future()
        future.set_result(self.wake_state)
        return future

    def wake_runtime_status(self):
        future = Future()
        future.set_result(self.wake_status)
        return future

    def configure_wake_word(self, config):
        self.wake_configurations.append(config)
        future = Future()
        future.set_result(None)
        return future

    def download_wake_model(self, progress):
        self.wake_downloads += 1
        self.wake_progress_callback = progress
        return self.wake_download_future

    def cancel_wake_model_download(self):
        self.wake_download_cancelled += 1

    def asr_model_state(self):
        self.asr_state_checks += 1
        if self.asr_state_future is not None:
            return self.asr_state_future
        future = Future()
        future.set_result(self.asr_state)
        return future

    def download_asr_model(self):
        self.downloads += 1
        return self.download_future

    def load_asr_model(self):
        self.loads += 1
        return self.load_future

    def close(self):
        self.closed += 1

    def approve(self, mode):
        self.approvals.append(mode)
        future = Future()
        future.set_result(None)
        return future

    def reject(self):
        self.rejections += 1
        future = Future()
        future.set_result(None)
        return future

    def list_memories(self):
        future = Future()
        future.set_result(self.memory_records)
        return future

    def delete_memory(self, memory_id):
        self.deleted_memories.append(memory_id)
        future = Future()
        future.set_result(True)
        return future

    def confirm_memory(self, memory_id):
        self.confirmed_memories.append(memory_id)
        future = Future()
        future.set_result(SimpleNamespace(status=SimpleNamespace(value="conflicted")))
        return future

    def resolve_memory_conflict(self, memory_id):
        self.resolved_memories.append(memory_id)
        future = Future()
        future.set_result(SimpleNamespace(status=SimpleNamespace(value="confirmed")))
        return future

    def export_memories(self, destination):
        self.exported_memories.append(destination)
        future = Future()
        future.set_result(2)
        return future

    def list_sessions(self):
        future = Future()
        future.set_result(self.session_records)
        return future

    def current_session_id(self):
        future = Future()
        future.set_result(self.active_session_id)
        return future

    def list_session_turns(self, session_id):
        future = Future()
        future.set_result(self.session_turns.get(session_id, ()))
        return future

    def activate_session(self, session_id):
        self.active_session_id = session_id
        self.activated_sessions.append(session_id)
        future = Future()
        future.set_result(self.session_turns.get(session_id, ()))
        return future

    def new_session(self):
        self.active_session_id = str(TurnId.new())
        self.new_sessions += 1
        future = Future()
        future.set_result(self.active_session_id)
        return future

    def delete_session(self, session_id):
        self.deleted_sessions.append(session_id)
        future = Future()
        future.set_result(True)
        return future

    def clear_sessions(self):
        self.cleared_sessions += 1
        future = Future()
        future.set_result(2)
        return future

    def install_pet(self, source):
        self.pet_sources.append(source)
        future = Future()
        future.set_result(self.installed_pet)
        return future

    def run_diagnostics(self):
        future = Future()
        future.set_result(self.diagnostic_report)
        return future

    def export_diagnostics(self, destination):
        self.diagnostic_exports.append(destination)
        future = Future()
        future.set_result(None)
        return future


class FakeHotkey(QObject):
    activated = Signal()

    def __init__(self):
        super().__init__()
        self.closed = 0

    def close(self):
        self.closed += 1


class FailingConfigStore:
    def save(self, config):
        del config
        raise ConfigError("磁盘只读")


class FakeCredentialStore:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        return self.values.pop(name, None) is not None


def test_application_controller_connects_runtime_intent_events_and_close(tmp_path):
    application = app_instance()
    store = ConfigStore(tmp_path / "config.json")
    bus = EventBus()
    bridge = QtEventBridge(bus, (TextDelta,))
    runtime = FakeRuntimeHost()
    controller = ApplicationController(
        application,
        store,
        AppConfig(),
        runtime_host=runtime,
        event_bridge=bridge,
    )

    controller.start()
    controller.tray.wake_action.trigger()
    controller.pet.activation_requested.emit()
    __import__("asyncio").run(
        bus.publish(TextDelta(TurnId.new(), CorrelationId.new(), "你好"))
    )
    controller.close()

    assert runtime.started == 1
    assert runtime.activations == ["click", "click"]
    assert runtime.closed == 1
    assert controller.pet.bubble.text() == "你好"


def test_application_controller_routes_hotkey_activation_and_closes_once(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    hotkey = FakeHotkey()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
        global_hotkey=hotkey,
    )

    hotkey.activated.emit()
    controller.close()
    controller.close()

    assert runtime.activations == ["hotkey"]
    assert hotkey.closed == 1


def test_tray_manual_input_submits_text_and_stays_open_after_acceptance(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.tray.manual_input_action.trigger()
    assert controller.manual_input.isVisible()
    controller.manual_input.text_edit.setText("帮我整理待办")
    controller.manual_input.send_button.click()

    assert runtime.submitted_texts == ["帮我整理待办"]
    assert controller.manual_input.isVisible()
    assert controller.manual_input.text_edit.text() == ""
    controller.close()


def test_manual_input_keeps_text_and_explains_busy_runtime(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.submit_text_future = Future()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )
    controller.show_manual_input()
    controller.manual_input.text_edit.setText("第二条请求")
    controller.manual_input.send_button.click()

    runtime.submit_text_future.set_exception(
        InvalidTransition("cannot start text turn from THINKING")
    )
    QCoreApplication.processEvents()

    assert controller.manual_input.isVisible()
    assert controller.manual_input.text_edit.text() == "第二条请求"
    assert "正在处理其他请求" in controller.manual_input.error_label.text()
    controller.close()


def test_application_controller_maps_runtime_events_to_safe_bubble(tmp_path):
    application = app_instance()
    bus = EventBus()
    event_types = (
        StateChanged,
        TranscriptReady,
        TextInputSubmitted,
        TextDelta,
        ApprovalRequested,
        ToolResultReady,
        SpeakRequested,
        RuntimeErrorEvent,
    )
    bridge = QtEventBridge(bus, event_types)
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()

    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.IDLE,
                ConversationPhase.LISTENING,
            )
        )
    )
    assert controller.pet.bubble.text() == "正在聆听…"
    assert controller.pet.pet_animation is not None
    assert controller.pet.pet_animation.current_row == 6

    __import__("asyncio").run(
        bus.publish(TranscriptReady(turn_id, correlation_id, "打开设置"))
    )
    assert controller.pet.bubble.text() == "你：打开设置"

    __import__("asyncio").run(
        bus.publish(TextInputSubmitted(turn_id, correlation_id, "手动问题"))
    )
    assert controller.pet.bubble.text() == "你：手动问题"

    __import__("asyncio").run(
        bus.publish(
            ApprovalRequested(
                turn_id,
                correlation_id,
                "call-1",
                "移动文件",
                "R2",
            )
        )
    )
    assert controller.pet.bubble.text() == "需要确认 R2：移动文件"

    __import__("asyncio").run(
        bus.publish(
            ToolResultReady(
                turn_id,
                correlation_id,
                "call-1",
                "success",
                {"message": "移动完成"},
            )
        )
    )
    assert controller.pet.bubble.text() == "移动完成"
    assert not controller.confirmation.isVisible()

    __import__("asyncio").run(
        bus.publish(SpeakRequested(turn_id, correlation_id, "已经完成"))
    )
    assert controller.pet.bubble.text() == "已经完成"

    __import__("asyncio").run(
        bus.publish(
            runtime_error_event(
                turn_id,
                correlation_id,
                AudioDeviceError(
                    "C:\\Users\\A\\private.wav sk-private-value private prompt"
                ),
            )
        )
    )
    assert controller.pet.bubble.text() == (
        "麦克风设备不可用，请检查系统权限和设备连接"
    )
    assert "private" not in controller.pet.bubble.text()
    controller.close()


def test_internal_summary_status_does_not_replace_streamed_reply(tmp_path):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (TextDelta, MemoryResultReady))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()
    __import__("asyncio").run(
        bus.publish(TextDelta(turn_id, correlation_id, "这是模型回复"))
    )

    __import__("asyncio").run(
        bus.publish(
            MemoryResultReady(
                turn_id,
                correlation_id,
                "pending",
                "短期摘要将在本轮完成后保存",
                0,
            )
        )
    )

    assert controller.pet.bubble.text() == "这是模型回复"
    controller.close()


def test_tts_error_preserves_completed_reply_in_bubble(tmp_path):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (TextDelta, RuntimeErrorEvent))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()
    __import__("asyncio").run(
        bus.publish(TextDelta(turn_id, correlation_id, "这是模型回复"))
    )

    __import__("asyncio").run(
        bus.publish(
            runtime_error_event(
                turn_id,
                correlation_id,
                TtsPlaybackError("private playback detail"),
            )
        )
    )

    assert "这是模型回复" in controller.pet.bubble.text()
    assert "语音播放失败" in controller.pet.bubble.text()
    assert "private playback detail" not in controller.pet.bubble.text()
    controller.close()


def test_speak_chunk_does_not_replace_complete_streamed_reply(tmp_path):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (TextDelta, SpeakRequested))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()
    __import__("asyncio").run(
        bus.publish(TextDelta(turn_id, correlation_id, "完整的模型回复。第二句。"))
    )

    __import__("asyncio").run(
        bus.publish(SpeakRequested(turn_id, correlation_id, "第二句。"))
    )

    assert controller.pet.bubble.text() == "完整的模型回复。第二句。"
    controller.close()


def test_tts_states_do_not_replace_complete_streamed_reply(tmp_path):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (StateChanged, TextDelta))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()
    __import__("asyncio").run(
        bus.publish(TextDelta(turn_id, correlation_id, "完整回复"))
    )

    for previous, current in (
        (ConversationPhase.THINKING, ConversationPhase.SYNTHESIZING),
        (ConversationPhase.SYNTHESIZING, ConversationPhase.SPEAKING),
    ):
        __import__("asyncio").run(
            bus.publish(
                StateChanged(
                    turn_id,
                    correlation_id,
                    previous,
                    current,
                )
            )
        )
        assert controller.pet.bubble.text() == "完整回复"

    controller.close()


def test_tts_state_is_visible_without_model_reply(tmp_path):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (StateChanged,))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )

    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                TurnId.new(),
                CorrelationId.new(),
                ConversationPhase.IDLE,
                ConversationPhase.SYNTHESIZING,
            )
        )
    )

    assert controller.pet.bubble.text() == "正在准备语音…"
    controller.close()


def test_completed_reply_schedules_bubble_hide_after_idle(tmp_path, monkeypatch):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (StateChanged, TextDelta))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    scheduled = []
    monkeypatch.setattr(
        controller.pet,
        "schedule_message_hide",
        scheduled.append,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()

    __import__("asyncio").run(
        bus.publish(TextDelta(turn_id, correlation_id, "完成回复"))
    )
    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.SPEAKING,
                ConversationPhase.IDLE,
            )
        )
    )

    assert scheduled == [8000]
    controller.close()


def test_idle_without_reply_does_not_schedule_bubble_hide(tmp_path, monkeypatch):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (StateChanged,))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    scheduled = []
    monkeypatch.setattr(
        controller.pet,
        "schedule_message_hide",
        scheduled.append,
    )

    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                TurnId.new(),
                CorrelationId.new(),
                ConversationPhase.RECOVERING,
                ConversationPhase.IDLE,
            )
        )
    )

    assert scheduled == []
    controller.close()


def test_empty_transcript_replaces_transcribing_status_and_hides(tmp_path, monkeypatch):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (StateChanged,))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    scheduled = []
    monkeypatch.setattr(
        controller.pet,
        "schedule_message_hide",
        scheduled.append,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()

    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.LISTENING,
                ConversationPhase.TRANSCRIBING,
            )
        )
    )
    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.TRANSCRIBING,
                ConversationPhase.IDLE,
            )
        )
    )

    assert controller.pet.bubble.text() == "没有听清，请再试一次"
    assert scheduled == [2500]
    controller.close()


def test_runtime_error_survives_recovery_state_and_hides_after_idle(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (RuntimeErrorEvent, StateChanged))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    scheduled = []
    monkeypatch.setattr(
        controller.pet,
        "schedule_message_hide",
        scheduled.append,
    )
    turn_id = TurnId.new()
    correlation_id = CorrelationId.new()
    error = runtime_error_event(
        turn_id,
        correlation_id,
        AudioDeviceError("microphone disconnected"),
    )

    __import__("asyncio").run(bus.publish(error))
    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.TRANSCRIBING,
                ConversationPhase.RECOVERING,
            )
        )
    )
    __import__("asyncio").run(
        bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.RECOVERING,
                ConversationPhase.IDLE,
            )
        )
    )

    assert controller.pet.bubble.text() == error.safe_message
    assert scheduled == [8000]
    controller.close()


def test_runtime_messages_use_pet_shell_resize_boundary(tmp_path, monkeypatch):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (TextDelta,))
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        event_bridge=bridge,
    )
    messages = []
    monkeypatch.setattr(
        controller.pet,
        "show_message",
        messages.append,
        raising=False,
    )

    __import__("asyncio").run(
        bus.publish(TextDelta(TurnId.new(), CorrelationId.new(), "模型回复"))
    )

    assert messages == ["模型回复"]
    controller.close()


def test_application_controller_routes_confirmation_buttons_to_runtime(tmp_path):
    application = app_instance()
    bus = EventBus()
    bridge = QtEventBridge(bus, (ApprovalRequested,))
    runtime = FakeRuntimeHost()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
        event_bridge=bridge,
    )
    event = ApprovalRequested(
        TurnId.new(),
        CorrelationId.new(),
        "call-1",
        "将 report.txt 移动到 archive",
        "R2",
    )

    __import__("asyncio").run(bus.publish(event))
    assert controller.confirmation.isVisible()
    assert "report.txt" in controller.confirmation.summary_label.text()
    controller.confirmation.approve_button.click()
    assert runtime.approvals == [ConfirmationMode.UI]

    __import__("asyncio").run(bus.publish(event))
    controller.confirmation.reject_button.click()
    assert runtime.rejections == 1
    controller.close()


def test_application_controller_routes_memory_management_off_ui_thread(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    runtime = FakeRuntimeHost()
    record = type(
        "Record",
        (),
        {
            "id": "memory-1",
            "category": "preference",
            "content": "喜欢低音量播报",
            "status": type("Status", (), {"value": "confirmed"})(),
        },
    )()
    runtime.memory_records = (record,)
    destination = str(tmp_path / "memory.json")
    monkeypatch.setattr(
        "ui.application.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (destination, "JSON (*.json)"),
    )
    monkeypatch.setattr(
        "ui.application.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.settings.memory_refresh_button.click()
    assert controller.settings.memory_list.count() == 1
    controller.settings.memory_list.setCurrentRow(0)
    controller.settings.memory_confirm_button.click()
    controller.settings.memory_list.setCurrentRow(0)
    controller.settings.memory_resolve_button.click()
    controller.settings.memory_list.setCurrentRow(0)
    controller.settings.memory_delete_button.click()
    controller.settings.memory_export_button.click()

    assert runtime.deleted_memories == ["memory-1"]
    assert runtime.confirmed_memories == ["memory-1"]
    assert runtime.resolved_memories == ["memory-1"]
    assert runtime.exported_memories == [destination]
    controller.close()


def test_application_controller_routes_session_management(tmp_path, monkeypatch):
    application = app_instance()
    runtime = FakeRuntimeHost()
    session_id = runtime.active_session_id
    turn = SimpleNamespace(
        user_text="第一问",
        assistant_text="第一答",
        created_at=datetime(2026, 9, 4, 9, tzinfo=UTC),
    )
    runtime.session_turns[session_id] = (turn,)
    runtime.session_records = (
        SimpleNamespace(
            id=session_id,
            title="第一问",
            turn_count=1,
            last_assistant_text="第一答",
            created_at=datetime(2026, 9, 4, 9, tzinfo=UTC),
            updated_at=datetime(2026, 9, 4, 9, tzinfo=UTC),
            is_active=True,
        ),
    )
    monkeypatch.setattr(
        "ui.application._confirm_session_clear",
        lambda parent: True,
    )
    monkeypatch.setattr(
        "ui.application._confirm_session_delete",
        lambda parent: True,
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.settings.session_refresh_button.click()
    assert controller.settings.session_list.count() == 1
    controller.settings.session_list.setCurrentRow(0)
    assert "第一答" in controller.settings.session_detail.toPlainText()
    controller.settings.session_activate_button.click()
    assert runtime.activated_sessions == [session_id]
    assert "第一问" in controller.manual_input.conversation_text
    controller.manual_input.new_session_button.click()
    assert runtime.new_sessions == 1
    assert "这里还没有消息" in controller.manual_input.conversation_text
    controller.settings.session_list.setCurrentRow(0)
    controller.settings.session_delete_button.click()
    controller.settings.session_clear_button.click()

    assert runtime.deleted_sessions == [session_id]
    assert runtime.cleared_sessions == 1
    assert "已清理 2 条" in controller.settings.validation_message.text()
    controller.close()


def test_application_controller_imports_and_switches_pet_after_validation(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.installed_pet = Path("assets/pet/dpsk-girl").resolve()
    source = str(tmp_path / "new.codex-pet")
    monkeypatch.setattr(
        "ui.application.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (source, "Codex Pet (*.codex-pet *.zip)"),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
        catalog_loader=lambda: (
            PetChoice(
                "dpsk-girl",
                "迪普斯伊克",
                runtime.installed_pet,
                True,
            ),
        ),
    )

    controller.settings.pet_file_import_button.click()

    assert runtime.pet_sources == [source]
    assert controller.pet.using_pet_asset is True
    assert controller.settings.selected_pet_id() == "dpsk-girl"
    assert "保存设置" in controller.settings.validation_message.text()
    controller.close()


def test_application_controller_previews_pet_selection_without_saving(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    choices = (
        PetChoice("dpsk-girl", "迪普斯伊克", tmp_path / "built-in", True),
        PetChoice("forest-cat", "森林猫", tmp_path / "forest-cat", False),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        catalog_loader=lambda: choices,
    )
    loaded = []
    monkeypatch.setattr(
        controller.pet,
        "load_pet_directory",
        lambda directory: loaded.append(Path(directory)) or True,
    )

    controller.settings.pet_combo.setCurrentIndex(1)

    assert loaded == [choices[1].directory]
    assert controller.settings.selected_pet_id() == "forest-cat"
    assert not (tmp_path / "config.json").exists()
    controller.close()


def test_application_controller_rolls_back_failed_pet_preview(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    choices = (
        PetChoice("dpsk-girl", "迪普斯伊克", tmp_path / "built-in", True),
        PetChoice("broken-pet", "损坏形象", tmp_path / "broken", False),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        catalog_loader=lambda: choices,
    )
    monkeypatch.setattr(controller.pet, "load_pet_directory", lambda path: False)

    controller.settings.pet_combo.setCurrentIndex(1)

    assert controller.settings.selected_pet_id() == "dpsk-girl"
    assert "加载失败" in controller.settings.validation_message.text()
    controller.close()


def test_application_controller_refreshes_catalog_after_pet_import(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    existing = PetChoice(
        "dpsk-girl",
        "迪普斯伊克",
        tmp_path / "built-in",
        True,
    )
    imported = PetChoice(
        "forest-cat",
        "森林猫",
        tmp_path / "data" / "pets" / "forest-cat",
        False,
    )
    catalog = [(existing,)]
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        catalog_loader=lambda: catalog[0],
    )
    loaded = []
    monkeypatch.setattr(
        controller.pet,
        "load_pet_directory",
        lambda directory: loaded.append(Path(directory)) or True,
    )
    catalog[0] = (existing, imported)
    future = Future()
    future.set_result(imported.directory)

    controller._pet_import_finished(future)

    assert controller.settings.pet_combo.count() == 2
    assert controller.settings.selected_pet_id() == "forest-cat"
    assert loaded == [imported.directory]
    controller.close()


def test_application_controller_runs_and_exports_local_diagnostics(tmp_path, monkeypatch):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.diagnostic_report = type(
        "Report",
        (),
        {
            "overall_status": type("Status", (), {"value": "healthy"})(),
            "results": (),
        },
    )()
    destination = str(tmp_path / "diagnostics.zip")
    monkeypatch.setattr(
        "ui.application.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (destination, "ZIP (*.zip)"),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.settings.diagnostics_run_button.click()
    controller.settings.diagnostics_export_button.click()

    assert "healthy" in controller.settings.diagnostics_summary.text()
    assert runtime.diagnostic_exports == [destination]
    controller.close()


def test_application_controller_connects_tray_settings_save_and_quit(tmp_path):
    application = app_instance()
    store = ConfigStore(tmp_path / "config.json")
    controller = ApplicationController(application, store, AppConfig())

    controller.tray.settings_action.trigger()
    assert controller.settings.isVisible()

    controller.settings.save_button.click()
    assert store.load().status is ConfigLoadStatus.LOADED

    controller.tray.quit_action.trigger()
    assert controller.is_closed is True


def test_application_controller_reports_config_persistence_failure():
    application = app_instance()
    controller = ApplicationController(
        application,
        FailingConfigStore(),
        AppConfig(),
    )

    controller.settings.save_button.click()

    assert "保存失败" in controller.settings.validation_message.text()
    assert "磁盘只读" in controller.settings.validation_message.text()
    controller.close()


def test_application_controller_applies_persisted_topmost_setting(tmp_path):
    application = app_instance()
    config = AppConfig(
        ui=replace(AppConfig().ui, always_on_top=False),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        config,
    )

    assert not controller.pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    controller.settings.always_on_top_checkbox.setChecked(True)
    controller.settings.save_button.click()
    assert controller.pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert "重启" not in controller.settings.validation_message.text()
    controller.close()


def test_application_controller_applies_voice_toggle_immediately(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.settings.speech_enabled_checkbox.setChecked(False)
    controller.settings.save_button.click()

    assert runtime.speech_toggles == [False]
    assert "立即生效" in controller.settings.validation_message.text()
    controller.close()


def test_application_controller_loads_and_previews_chinese_voices(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.tts_voices = (
        TtsVoiceOption("zh-CN-XiaoxiaoNeural", "zh-CN", "Female"),
        TtsVoiceOption("zh-HK-WanLungNeural", "zh-HK", "Male"),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.show_settings()
    QCoreApplication.processEvents()

    assert runtime.tts_voice_checks == 1
    assert controller.settings.voice_combo.count() == 2
    assert "中国大陆" in controller.settings.voice_combo.itemText(0)
    controller.settings.voice_preview_button.click()
    QCoreApplication.processEvents()
    assert runtime.tts_voice_previews == ["zh-CN-XiaoxiaoNeural"]
    assert controller.settings.voice_preview_button.text() == "试听"
    assert controller.settings.voice_catalog_status.text() == "试听完成"
    controller.close()


def test_application_controller_marks_runtime_changes_for_restart_once(tmp_path):
    application = app_instance()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
    )

    controller.settings.asr_model_edit.setText("medium")
    controller.settings.save_button.click()

    assert "重新启动" in controller.settings.validation_message.text()
    controller.settings.save_button.click()
    assert controller.settings.validation_message.text() == "设置已保存"
    controller.close()


def test_application_controller_saves_and_removes_encrypted_api_key(tmp_path):
    application = app_instance()
    credentials = FakeCredentialStore()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        credential_store=credentials,
    )

    controller.settings.api_key_edit.setText("sk-private-value")
    controller.settings.api_key_save_button.click()
    assert credentials.values == {"openai_api_key": "sk-private-value"}
    assert "重启" in controller.settings.validation_message.text()

    controller.settings.api_key_delete_button.click()
    assert credentials.values == {}
    controller.close()


def test_run_ui_smoke_constructs_and_closes_without_event_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert run_ui(
        smoke_test=True,
        hotkey_factory=lambda: (_ for _ in ()).throw(
            AssertionError("smoke registered global hotkey")
        ),
        credential_store_factory=lambda path: (_ for _ in ()).throw(
            AssertionError(f"smoke accessed credentials: {path}")
        ),
    ) == 0
    assert not (tmp_path / "VoicePet" / "config.json").exists()


def test_run_ui_normal_mode_builds_starts_and_closes_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime = FakeRuntimeHost()
    hotkey = FakeHotkey()
    built = []

    def runtime_builder(config, event_bus, data_root, *, api_key, pet_directory):
        built.append((config, event_bus, data_root, api_key, pet_directory))
        return object()

    def host_factory(services):
        assert services is not None
        return runtime

    result = run_ui(
        runtime_builder=runtime_builder,
        runtime_host_factory=host_factory,
        credential_store_factory=lambda path: type(
            "Credentials",
            (),
            {"get": lambda self, name: "sk-runtime-secret"},
        )(),
        hotkey_factory=lambda: hotkey,
        event_loop=lambda: 17,
    )

    assert result == 17
    assert len(built) == 1
    assert built[0][2] == (tmp_path / "VoicePet").resolve()
    assert built[0][3] == "sk-runtime-secret"
    assert built[0][4].name == "dpsk-girl"
    assert runtime.started == 1
    assert runtime.closed == 1
    assert hotkey.closed == 1


def test_settings_has_only_personal_runtime_pages():
    app_instance()
    settings = SettingsWindow(AppConfig())

    assert settings.navigation_labels == (
        "语音",
        "AI",
        "会话",
        "桌宠",
        "隐私",
        "诊断",
    )


def test_missing_llm_speaks_configuration_notice_without_asr(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.llm_configured = False
    runtime.notice_future = Future()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.pet.activation_requested.emit()
    controller.pet.activation_requested.emit()

    message = "请先在设置中配置 API Key，保存后重启 VoicePet"
    assert controller.pet.bubble.text() == message
    assert runtime.notices == [message]
    assert runtime.asr_state_checks == 0
    assert runtime.activations == []
    assert controller.pet._message_hide_timer.isActive()
    controller.close()


def test_missing_llm_notice_restarts_hide_timer_after_speech(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.llm_configured = False
    runtime.notice_future = Future()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.pet.activation_requested.emit()
    controller.pet.cancel_message_hide()
    runtime.notice_future.set_result(TurnId.new())
    application.processEvents()

    assert controller.pet._message_hide_timer.isActive()
    controller.close()


def test_missing_llm_only_shows_notice_when_voice_is_disabled(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.llm_configured = False
    config = replace(
        AppConfig(),
        tts=replace(AppConfig().tts, enabled=False),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        config,
        runtime_host=runtime,
    )

    controller.pet.activation_requested.emit()

    assert "配置 API Key" in controller.pet.bubble.text()
    assert runtime.notices == []
    assert runtime.asr_state_checks == 0
    assert controller.pet._message_hide_timer.isActive()
    controller.close()


def test_start_prompts_once_for_missing_enabled_wake_model(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.wake_state = WakeModelState.MISSING
    prompts = []
    monkeypatch.setattr(
        application_module,
        "_confirm_model_download",
        lambda parent, **options: prompts.append(options) or False,
        raising=False,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("模型下载不应再使用静态确认框")
        ),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.start()
    controller.start()
    application.processEvents()

    assert prompts == [
        {
            "title": "准备中文唤醒模型",
            "message": "语音唤醒需要下载中文关键词模型，约 31.1 MiB。是否现在下载？",
        }
    ]
    assert runtime.wake_downloads == 0
    controller.close()


def test_missing_wake_model_confirmation_starts_download(tmp_path, monkeypatch):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.wake_state = WakeModelState.MISSING
    monkeypatch.setattr(
        application_module,
        "_confirm_model_download",
        lambda parent, **options: True,
        raising=False,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("模型下载不应再使用静态确认框")
        ),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.start()
    application.processEvents()

    assert runtime.wake_downloads == 1
    controller.close()


def test_ready_wake_model_reports_runtime_start_failure(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.wake_status = WakeRuntimeStatus.FAILED
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.start()
    application.processEvents()

    assert "启动失败" in controller.settings.wake_model_status.text()
    controller.close()


def test_wake_download_reports_progress_and_finishes_without_restart(
    tmp_path,
):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.wake_state = WakeModelState.MISSING
    runtime.wake_download_future = Future()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.settings.wake_model_download_button.click()
    runtime.wake_progress_callback(WakeDownloadProgress(16_327_433, 32_654_866))
    application.processEvents()

    assert controller.settings.wake_model_progress.value() == 50
    assert runtime.wake_downloads == 1
    runtime.wake_download_future.set_result(None)
    application.processEvents()
    assert runtime.wake_configurations[-1] == AppConfig().wake_word
    assert "监听" in controller.settings.wake_model_status.text()
    controller.close()


def test_wake_download_can_be_cancelled_from_settings(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.wake_download_future = Future()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.settings.wake_model_download_button.click()
    controller.settings.wake_model_cancel_button.click()

    assert runtime.wake_download_cancelled == 1
    controller.close()


def test_application_applies_wake_configuration_immediately(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )
    controller.settings.wake_keyword_edit.setText("你好海蓝")

    controller.settings.save_button.click()

    assert runtime.wake_configurations[-1].keyword == "你好海蓝"
    assert "立即生效" in controller.settings.validation_message.text()
    controller.close()


def test_start_preloads_cached_asr_without_activation(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.asr_state = AsrModelState.CACHED
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.start()
    application.processEvents()

    assert runtime.asr_state_checks == 1
    assert runtime.loads == 1
    assert runtime.activations == []
    controller.close()


def test_activation_waits_for_startup_asr_preload(tmp_path):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.asr_state = AsrModelState.CACHED
    runtime.load_future = Future()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.start()
    application.processEvents()
    controller._activate_runtime("wake_word")
    controller._activate_runtime("click")

    assert runtime.asr_state_checks == 1
    assert runtime.loads == 1
    assert runtime.activations == []
    runtime.load_future.set_result(None)
    application.processEvents()
    assert runtime.activations == ["click"]
    controller.close()


def test_missing_asr_requires_confirmation_and_cancel_stays_idle(
    tmp_path,
    monkeypatch,
):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.asr_state = AsrModelState.MISSING
    prompts = []
    monkeypatch.setattr(
        application_module,
        "_confirm_model_download",
        lambda parent, **options: prompts.append(options) or False,
        raising=False,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("模型下载不应再使用静态确认框")
        ),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller.pet.activation_requested.emit()
    application.processEvents()

    assert prompts == [
        {
            "title": "准备语音模型",
            "message": "首次使用需要下载语音模型 small，约 464 MiB。是否现在下载？",
        }
    ]
    assert controller.pet.bubble.text() == "已取消语音模型下载"
    assert runtime.downloads == 0
    assert runtime.activations == []
    controller.close()


def test_asr_download_load_and_single_resumed_activation(tmp_path, monkeypatch):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.asr_state = AsrModelState.MISSING
    runtime.download_future = Future()
    runtime.load_future = Future()
    monkeypatch.setattr(
        application_module,
        "_confirm_model_download",
        lambda parent, **options: True,
        raising=False,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("模型下载不应再使用静态确认框")
        ),
    )
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller._activate_runtime("hotkey")
    controller._activate_runtime("click")
    application.processEvents()
    assert runtime.asr_state_checks == 1
    assert runtime.downloads == 1
    assert "正在下载" in controller.pet.bubble.text()

    runtime.download_future.set_result(None)
    application.processEvents()
    assert runtime.loads == 1
    assert "正在加载" in controller.pet.bubble.text()

    runtime.load_future.set_result(None)
    application.processEvents()
    assert runtime.activations == ["click"]
    assert controller.pet.bubble.text() == "语音模型准备完成"
    controller.close()


def test_cached_and_ready_asr_skip_unneeded_steps(tmp_path, monkeypatch):
    application = app_instance()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("已缓存模型不应请求下载确认")
        ),
    )
    cached = FakeRuntimeHost()
    cached.asr_state = AsrModelState.CACHED
    cached_controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "cached.json"),
        AppConfig(),
        runtime_host=cached,
    )

    cached_controller._activate_runtime("click")
    application.processEvents()
    assert cached.downloads == 0
    assert cached.loads == 1
    assert cached.activations == ["click"]
    cached_controller.close()

    ready = FakeRuntimeHost()
    ready_controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "ready.json"),
        AppConfig(),
        runtime_host=ready,
    )
    ready_controller._activate_runtime("hotkey")
    application.processEvents()
    assert ready.downloads == 0
    assert ready.loads == 0
    assert ready.activations == ["hotkey"]
    ready_controller.close()


def test_asr_preparation_failure_is_safe_retryable_and_late_result_is_ignored(
    tmp_path,
):
    application = app_instance()
    runtime = FakeRuntimeHost()
    runtime.asr_state_future = Future()
    controller = ApplicationController(
        application,
        ConfigStore(tmp_path / "config.json"),
        AppConfig(),
        runtime_host=runtime,
    )

    controller._activate_runtime("click")
    runtime.asr_state_future.set_exception(RuntimeError(str(tmp_path / "private")))
    application.processEvents()
    assert controller.pet.bubble.text() == "语音模型准备失败，请检查网络后重试"
    assert str(tmp_path) not in controller.pet.bubble.text()

    runtime.asr_state_future = Future()
    controller._activate_runtime("hotkey")
    assert runtime.asr_state_checks == 2
    controller.close()
    runtime.asr_state_future.set_result(AsrModelState.READY)
    application.processEvents()
    assert runtime.activations == []
