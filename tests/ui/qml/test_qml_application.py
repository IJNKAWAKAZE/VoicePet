from concurrent.futures import Future
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QSignalSpy, QTest

from core.config import AppConfig
from core.events import (
    AgentProgress,
    ApprovalRequested,
    ConversationPhase,
    CorrelationId,
    RecordingStarted,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TurnId,
)
from core.window_state import WindowPosition
from ui.pet_animation_model import PetAnimationModel, PetInteractionController
from ui.pet_catalog import PetChoice
from ui.qml_application import QmlApplicationController
from ui.qml_resources import qml_root
from ui.qml_runtime import QmlRuntime
from ui.viewmodels.app_shell import AppShellViewModel
from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.diagnostics import DiagnosticsViewModel
from ui.viewmodels.dialogs import DialogCoordinator
from ui.viewmodels.memories import MemoryViewModel
from ui.viewmodels.pets import PetViewModel
from ui.viewmodels.settings import SettingsViewModel
from ui.viewmodels.theme import ThemeViewModel
from ui.window_coordinator import WindowCoordinator


def completed(value=None):
    future = Future()
    future.set_result(value)
    return future


class Runtime:
    def __init__(self):
        self.started = 0
        self.closed = 0
        self.activations = []
        self.cancellations = 0
        self.approvals = []
        self.rejections = 0
        self.speech_enabled = []
        self.manual_speech_enabled = []
        self.wake_configs = []
        self.session_list_requests = 0
        self.pet_catalog_loads = 0

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def activate(self, source):
        self.activations.append(source)
        return completed()

    def cancel_active_turn(self, session_id: str = ""):
        self.cancellations += 1
        return completed()

    def approve(self):
        self.approvals.append(None)
        return completed()

    def reject(self):
        self.rejections += 1
        return completed()

    def set_speech_enabled(self, enabled):
        self.speech_enabled.append(enabled)
        return completed()

    def set_manual_input_speech_enabled(self, enabled):
        self.manual_speech_enabled.append(enabled)
        return completed()

    def configure_wake_word(self, config):
        self.wake_configs.append(config)
        return completed()

    def list_sessions(self):
        self.session_list_requests += 1
        return completed(())

    def load_pet_catalog(self):
        self.pet_catalog_loads += 1
        return (
            PetChoice(
                "kawakaze",
                "江风 Q版",
                Path("assets/pet/kawakaze").resolve(),
                True,
            ),
        )

    def __getattr__(self, name):
        return lambda *args: completed(())


class Tray(QObject):
    context_menu_requested = Signal(QPoint)
    open_requested = Signal()
    settings_requested = Signal()
    listen_toggle_requested = Signal()
    quit_requested = Signal()
    pet_visibility_requested = Signal()
    restore_interaction_requested = Signal()
    manual_input_requested = Signal()

    def __init__(self):
        super().__init__()
        self.shown = 0
        self.closed = 0
        self.listening = False
        self.visible = False

    def show(self):
        self.shown += 1
        self.visible = True

    def close(self):
        self.closed += 1
        self.visible = False

    @property
    def is_visible(self):
        return self.visible

    def set_listening(self, listening):
        self.listening = listening


class Store:
    def __init__(self):
        self.saved = []

    def save(self, config):
        self.saved.append(config)


class PositionStore:
    def __init__(self, position=None):
        self.position = position
        self.saved = []

    def load(self):
        return self.position

    def save(self, position):
        self.saved.append(position)


class StartupManager:
    def __init__(self):
        self.values = []

    def set_enabled(self, enabled):
        self.values.append(enabled)


class GlobalHotkey(QObject):
    activated = Signal()

    def __init__(self):
        super().__init__()
        self.shortcuts = []
        self.enabled = []
        self.closed = 0

    def try_replace(self, shortcut):
        self.shortcuts.append(shortcut)

    def set_enabled(self, enabled):
        self.enabled.append(enabled)

    def close(self):
        self.closed += 1


def build_controller(
    qapp,
    *,
    saved_position=None,
    startup_manager=None,
    global_hotkey=None,
):
    runtime = Runtime()
    tray = Tray()
    config = AppConfig()
    dialogs = DialogCoordinator()
    shell = AppShellViewModel()
    settings = SettingsViewModel(config, Store())
    chat = ChatViewModel(runtime)
    memories = MemoryViewModel(runtime, dialogs)
    pets = PetViewModel(runtime, settings, runtime.load_pet_catalog)
    diagnostics = DiagnosticsViewModel(runtime)
    theme = ThemeViewModel(config.ui, lambda ui: None)
    animation = PetAnimationModel(Path("assets/pet/kawakaze").resolve())
    interaction = PetInteractionController(double_click_interval=5)
    qml = QmlRuntime(
        qapp,
        {
            "appShell": shell,
            "themeViewModel": theme,
            "dialogCoordinator": dialogs,
            "chatViewModel": chat,
            "memoryViewModel": memories,
            "settingsViewModel": settings,
            "petViewModel": pets,
            "diagnosticsViewModel": diagnostics,
            "petAnimation": animation,
            "petInteraction": interaction,
        },
        qml_root() / "Main.qml",
    )
    position_store = PositionStore(saved_position)
    windows = WindowCoordinator(can_recover_click_through=lambda: tray.is_visible)
    controller = QmlApplicationController(
        qapp,
        runtime,
        qml,
        shell,
        chat,
        dialogs,
        animation,
        interaction,
        tray,
        memories=memories,
        global_hotkey=global_hotkey,
        settings=settings,
        pets=pets,
        startup_manager=startup_manager,
        window_coordinator=windows,
        position_store=position_store,
    )
    return (
        controller,
        runtime,
        tray,
        qml,
        shell,
        chat,
        dialogs,
        interaction,
        settings,
        position_store,
    )


def test_start_shows_pet_and_tray_without_forcing_main_window(qapp):
    controller, runtime, tray, qml, *_ = build_controller(qapp)

    controller.start()
    root = qml.root_objects[0]
    pet = root.findChild(QObject, "petWindow")

    assert runtime.started == 1
    assert tray.shown == 1
    assert root.property("visible") is False
    assert pet.property("visible") is True
    controller.close()


def test_start_refreshes_sessions_and_pet_catalog(qapp):
    controller, runtime, *_ = build_controller(qapp)

    controller.start()

    assert runtime.session_list_requests == 1
    assert runtime.pet_catalog_loads == 1
    controller.close()


def test_selecting_pet_reloads_desktop_and_settings_preview(qapp):
    controller, _runtime, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    pet_window = qml.root_objects[0].findChild(QObject, "petWindow")
    animation = pet_window.property("animation")
    changed = QSignalSpy(animation.sheetChanged)

    controller._pets.select_pet("kawakaze")

    assert changed.count() == 1
    assert Path(animation.sheetUrl.toLocalFile()).name == "spritesheet.webp"
    controller.close()


def test_tray_and_pet_double_click_open_requested_main_section(qapp):
    (
        controller,
        _runtime,
        tray,
        qml,
        shell,
        _chat,
        _dialogs,
        interaction,
        *_rest,
    ) = build_controller(qapp)
    controller.start()

    tray.settings_requested.emit()
    assert shell.current_section == "settings"
    interaction.double_click(1, 1, 1)

    assert shell.current_section == "chat"
    assert qml.root_objects[0].property("visible") is True
    controller.close()


def test_tray_manual_input_opens_chat_section(qapp):
    controller, _runtime, tray, qml, shell, *_ = build_controller(qapp)
    controller.start()
    shell.navigate("settings")

    tray.manual_input_requested.emit()

    assert shell.current_section == "chat"
    assert qml.root_objects[0].property("visible") is True
    controller.close()


def test_pet_click_starts_when_idle_and_cancels_when_active(qapp):
    (
        controller,
        runtime,
        _tray,
        _qml,
        _shell,
        _chat,
        _dialogs,
        interaction,
        *_rest,
    ) = build_controller(qapp)
    controller.start()
    interaction.listenToggleRequested.emit()
    turn_id = TurnId.new()
    controller.handle_runtime_event(
        StateChanged(turn_id, CorrelationId.new(), ConversationPhase.IDLE, ConversationPhase.LISTENING)
    )
    interaction.listenToggleRequested.emit()

    assert runtime.activations == ["click"]
    assert runtime.cancellations == 1
    controller.close()


def test_runtime_delta_updates_chat_and_hidden_approval_opens_window(qapp):
    (
        controller,
        runtime,
        _tray,
        qml,
        _shell,
        chat,
        dialogs,
        _interaction,
        *_rest,
    ) = build_controller(qapp)
    controller.start()
    turn_id = TurnId.new()
    correlation = CorrelationId.new()

    controller.handle_runtime_event(TextDelta(turn_id, correlation, "正在处理"))
    controller.handle_runtime_event(
        ApprovalRequested(turn_id, correlation, "call-1", "打开记事本", "低")
    )

    assert chat.message_model.rowCount() == 1
    confirmation = qml.root_objects[0].findChild(QObject, "toolConfirmationWindow")
    assert confirmation.property("visible") is True
    dialogs.resolve_confirmation("call-1", True)
    qapp.processEvents()
    assert len(runtime.approvals) == 1
    assert confirmation.property("visible") is False
    controller.close()


def test_visible_main_window_opens_embedded_confirmation_sheet(qapp):
    controller, runtime, _tray, qml, shell, _chat, dialogs, *_ = build_controller(
        qapp
    )
    controller.start()
    shell.show_main("chat")
    turn_id = TurnId.new()

    controller.handle_runtime_event(
        ApprovalRequested(
            turn_id,
            CorrelationId.new(),
            "call-visible",
            "打开记事本",
            "高",
        )
    )

    root = qml.root_objects[0]
    sheet = root.findChild(QObject, "mainConfirmationSheet")
    standalone = root.findChild(QObject, "toolConfirmationWindow")
    assert sheet is not None
    assert sheet.property("visible") is True
    assert standalone.property("visible") is False

    dialogs.resolve_confirmation("call-visible", True)
    qapp.processEvents()
    assert sheet.property("visible") is False
    assert len(runtime.approvals) == 1
    controller.close()


def test_pet_right_click_no_longer_opens_a_menu(qapp):
    controller, runtime, _tray, qml, _shell, *rest = build_controller(qapp)
    interaction = rest[2]
    controller.start()
    root = qml.root_objects[0]
    pet = root.findChild(QObject, "petWindow")
    assert root.findChild(QObject, "petMenuWindow") is None
    menu = root.findChild(QObject, "trayMenuWindow")

    interaction.quickMenuRequested.emit(10, 12)
    assert menu.property("visible") is False
    assert runtime.activations == []
    assert pet.property("visible") is True
    controller.close()


def test_pet_position_is_restored_and_drag_updates_are_persisted(qapp):
    built = build_controller(qapp, saved_position=WindowPosition(120, 140))
    controller, _runtime, _tray, qml, *_middle, interaction, _settings, store = built
    controller.start()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")

    assert pet.property("x") == 120
    assert pet.property("y") == 140

    interaction.positionChanged.emit(15, -8)

    assert pet.property("x") == 135
    assert pet.property("y") == 132
    assert store.saved[-1] == WindowPosition(135, 132)
    controller.close()


def test_pet_preferences_apply_immediately_and_tray_restores_input(qapp):
    built = build_controller(qapp)
    controller, _runtime, tray, qml, *_middle, settings, _store = built
    controller.start()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")
    speech = pet.findChild(QObject, "petSpeechWindow")

    settings.set_field("ui", "pet_scale", 1.25)
    settings.set_field("ui", "always_on_top", False)
    settings.set_field("ui", "pet_click_through", True)

    assert pet.property("petScale") == 1.25
    assert pet.property("alwaysOnTop") is False
    assert pet.flags() & Qt.WindowType.WindowTransparentForInput
    assert not speech.flags() & Qt.WindowType.WindowStaysOnTopHint
    assert speech.flags() & Qt.WindowType.WindowTransparentForInput

    tray.restore_interaction_requested.emit()

    assert settings.config.ui.pet_click_through is False
    assert not pet.flags() & Qt.WindowType.WindowTransparentForInput
    assert not speech.flags() & Qt.WindowType.WindowTransparentForInput
    controller.close()


def test_saved_settings_apply_startup_speech_and_wake_runtime(qapp):
    startup = StartupManager()
    built = build_controller(qapp, startup_manager=startup)
    controller, runtime, _tray, _qml, *_middle, settings, _store = built
    controller.start()

    settings.set_field("ui", "start_at_login", True)
    settings.set_field("tts", "enabled", False)
    settings.set_field("tts", "manual_input_enabled", True)
    settings.set_field("wake_word", "sensitivity", 0.7)
    settings.save_draft()

    assert startup.values == [True]
    assert runtime.speech_enabled == [False]
    assert runtime.manual_speech_enabled == [True]
    assert runtime.wake_configs[-1].sensitivity == 0.7
    controller.close()


def test_hotkey_settings_replace_then_disable_registration(qapp):
    hotkey = GlobalHotkey()
    built = build_controller(qapp, global_hotkey=hotkey)
    controller, _runtime, _tray, _qml, *_middle, settings, _store = built
    controller.start()

    settings.set_field("ui", "global_hotkey", "Ctrl+Shift+Space")
    settings.set_field("ui", "global_hotkey_enabled", False)

    assert hotkey.shortcuts == ["Ctrl+Shift+Space"]
    assert hotkey.enabled == [False]
    controller.close()


def test_agent_progress_only_updates_the_pet_bubble(qapp):
    controller, _runtime, _tray, qml, _shell, chat, *_ = build_controller(qapp)
    controller.start()
    turn_id = TurnId.new()
    correlation = CorrelationId.new()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")

    controller.handle_runtime_event(TextDelta(turn_id, correlation, "正在处理"))
    controller.handle_runtime_event(
        AgentProgress(turn_id, correlation, "已经完成")
    )

    assert pet.property("speech") == "已经完成"
    assert chat.message_model.rowCount() == 1
    assert pet.findChild(QObject, "petSpeechBubble") is not None
    controller.close()


def test_each_reply_segment_gets_its_own_pet_bubble(qapp):
    controller, _runtime, _tray, qml, _shell, chat, *_ = build_controller(qapp)
    controller.start()
    turn_id = TurnId.new()
    correlation = CorrelationId.new()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")

    controller.handle_runtime_event(TextDelta(turn_id, correlation, "先看", "item-1"))
    controller.handle_runtime_event(TextDelta(turn_id, correlation, "目录", "item-1"))
    assert pet.property("speech") == "先看目录"

    controller.handle_runtime_event(TextDelta(turn_id, correlation, "改完了", "item-2"))

    # 同一轮的下一段另起一条气泡，不跟上一段拼成一大段
    assert pet.property("speech") == "改完了"
    assert chat.message_model.rowCount() == 2
    controller.close()


def test_spoken_segments_replace_the_pet_bubble_one_by_one(qapp):
    controller, _runtime, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    turn_id = TurnId.new()
    correlation = CorrelationId.new()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")
    controller.handle_runtime_event(StateChanged(
        turn_id,
        correlation,
        ConversationPhase.SYNTHESIZING,
        ConversationPhase.SPEAKING,
    ))

    controller.handle_runtime_event(SpeakRequested(turn_id, correlation, "第一段"))
    assert pet.property("speech") == "第一段"
    controller.handle_runtime_event(SpeakRequested(turn_id, correlation, "第二段"))

    # 播报推进到哪一段，气泡就显示哪一段
    assert pet.property("speech") == "第二段"
    controller.close()


def test_pet_listening_hint_waits_for_audio_and_clears_when_recording_ends(qapp):
    controller, _runtime, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        turn = TurnId.new()
        correlation = CorrelationId.new()
        controller.handle_runtime_event(StateChanged(
            turn, correlation, ConversationPhase.IDLE, ConversationPhase.LISTENING))
        assert pet.property("speech") == ""
        controller.handle_runtime_event(SpeakRequested(turn, correlation, "我在，请说"))
        assert pet.property("speech") == "我在"
        controller.handle_runtime_event(RecordingStarted(turn, correlation))
        assert pet.property("speech") == "我在听…"
        controller.handle_runtime_event(StateChanged(
            turn, correlation, ConversationPhase.LISTENING, ConversationPhase.TRANSCRIBING))
        assert pet.property("speech") == ""
        controller.handle_runtime_event(RecordingStarted(turn, correlation))
        assert pet.property("speech") == ""
    finally:
        controller.close()


def test_replacing_pet_speech_restarts_auto_hide_timer(qapp, wait_for):
    controller, _runtime, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")
    bubble = pet.findChild(QObject, "petSpeechBubble")
    bubble.setProperty("timeoutMs", 1000)

    pet.setProperty("speech", "第一条")
    QTest.qWait(400)
    pet.setProperty("speech", "第二条")
    # 第一条的定时器若没有随第二条重启，会在 600ms 内触发并清空文本；
    # 这里等过它的触发点再检查，给慢机器留出足够余量
    QTest.qWait(700)

    assert pet.property("speech") == "第二条"
    assert wait_for(lambda: pet.property("speech") == "")
    controller.close()


def test_new_listening_turn_does_not_reuse_previous_speech_text(qapp):
    controller, _runtime, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    turn_id = TurnId.new()
    correlation = CorrelationId.new()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")

    controller.handle_runtime_event(TextDelta(turn_id, correlation, "上一轮"))
    controller.handle_runtime_event(
        StateChanged(
            TurnId.new(),
            CorrelationId.new(),
            ConversationPhase.IDLE,
            ConversationPhase.LISTENING,
        )
    )
    controller.handle_runtime_event(
        TextDelta(TurnId.new(), CorrelationId.new(), "新回复")
    )

    assert pet.property("speech") == "新回复"
    controller.close()


def test_close_releases_runtime_qml_and_tray(qapp):
    controller, runtime, tray, qml, *_ = build_controller(qapp)
    controller.start()

    controller.close()
    controller.close()

    assert runtime.closed == 1
    assert tray.closed == 1
    assert qml.root_objects == ()


def test_showing_the_pet_raises_it_back_to_the_top(qapp):
    built = build_controller(qapp)
    controller, _runtime, _tray, qml, *_middle, _settings, _store = built
    controller.start()
    pet = qml.root_objects[0].findChild(QObject, "petWindow")
    speech = pet.findChild(QObject, "petSpeechWindow")
    raised = []
    controller._window_coordinator.raise_window = (
        lambda window: raised.append(window) or True
    )

    controller._handle_pet_menu_action("visibility")
    assert pet.property("visible") is False
    raised.clear()
    controller._handle_pet_menu_action("visibility")

    # 重新显示桌宠不会自动回到置顶带最前，必须主动抬一次
    assert pet.property("visible") is True
    assert raised == [pet, speech]
    controller.close()


def test_disabled_topmost_leaves_window_order_alone(qapp):
    built = build_controller(qapp)
    controller, _runtime, _tray, _qml, *_middle, settings, _store = built
    controller.start()
    raised = []
    controller._window_coordinator.raise_window = (
        lambda window: raised.append(window) or True
    )
    settings.set_field("ui", "always_on_top", False)
    raised.clear()

    controller._handle_pet_menu_action("visibility")
    controller._handle_pet_menu_action("visibility")

    assert raised == []
    controller.close()
