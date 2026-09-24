"""QML 主窗口、独立桌宠与 Runtime 事件的应用级编排"""

from __future__ import annotations

from typing import Any, Protocol

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, QSize, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QJSValue

from core.events import (
    AgentApprovalRequested,
    AgentProgress,
    ApprovalRequested,
    ConversationPhase,
    MemoryChanged,
    MemoryMaintenanceChanged,
    MemoryResultReady,
    RecordingStarted,
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TranscriptReady,
    TurnId,
    WakeCommandPending,
)
from core.window_state import WindowPosition, WindowStateError

from .markdown import sanitize_markdown
from .pet_animation import PetAssetError
from .pet_animation_model import PetAnimationModel, PetInteractionController
from .qml_resources import QmlResourceError
from .qml_runtime import QmlRuntime
from .tray_menu import TrayMenuDismissal
from .viewmodels.app_shell import AppShellViewModel
from .viewmodels.chat import ChatViewModel
from .viewmodels.dialogs import (
    WINDOW_CHANNEL,
    AgentInteractionRequest,
    ConfirmationRequest,
    DialogCoordinator,
)
from .viewmodels.memories import MemoryViewModel
from .viewmodels.pets import PetViewModel
from .viewmodels.settings import SettingsViewModel
from .window_coordinator import WindowCoordinator

# 单条回复短于这个长度时逐条落地：每帧开销很小，保留逐字出现的观感
_STREAM_COALESCE_AFTER = 400


class QmlRuntimeHostProtocol(Protocol):
    def start(self) -> None: ...

    def activate(self, source: str) -> Any: ...

    def cancel_active_turn(self) -> Any: ...

    def set_speech_enabled(self, enabled: bool) -> Any: ...

    def set_audio_muted(self, muted: bool) -> Any: ...

    def approve(self) -> Any: ...

    def reject(self) -> Any: ...

    def resolve_agent_approval(self, approval_id: str, decision: str) -> Any: ...

    def close(self) -> None: ...


class QmlApplicationController(QObject):
    """不持有页面 Item，只协调顶层窗口和 ViewModel"""

    settingsApplyFailed = Signal(str)
    _muteFinished = Signal(bool, bool)
    _memoryReconfigureFinished = Signal(int, bool)

    def __init__(
        self,
        application: QObject,
        runtime_host: QmlRuntimeHostProtocol | None,
        qml_runtime: QmlRuntime,
        app_shell: AppShellViewModel,
        chat: ChatViewModel,
        dialogs: DialogCoordinator,
        pet_animation: PetAnimationModel,
        pet_interaction: PetInteractionController,
        tray: QObject,
        *,
        memories: MemoryViewModel | None = None,
        event_bridge: QObject | None = None,
        global_hotkey: QObject | None = None,
        settings: SettingsViewModel | None = None,
        pets: PetViewModel | None = None,
        startup_manager: object | None = None,
        window_coordinator: WindowCoordinator | None = None,
        position_store: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._application = application
        self._runtime_host = runtime_host
        self._qml_runtime = qml_runtime
        self._app_shell = app_shell
        self._chat = chat
        self._dialogs = dialogs
        self._memories = memories
        self._pet_animation = pet_animation
        self._pet_interaction = pet_interaction
        self._tray = tray
        self._event_bridge = event_bridge
        self._global_hotkey = global_hotkey
        self._settings = settings
        self._pets = pets
        self._startup_manager = startup_manager
        self._last_config = settings.config if settings is not None else None
        self._window_coordinator = window_coordinator or WindowCoordinator()
        self._position_store = position_store
        self._main_window: QObject | None = None
        self._pet_window: QObject | None = None
        self._pet_drag_position: QPointF | None = None
        self._pet_speech_window: QObject | None = None
        self._pet_menu_window: QObject | None = None
        self._tray_menu_dismissal = None
        self._confirmation_window: QObject | None = None
        self._phase = ConversationPhase.IDLE
        self._phase_turn_id: TurnId | None = None
        self._session_turns: dict[str, TurnId] = {}
        self._session_phases: dict[str, ConversationPhase] = {}
        self._speech_text = ""
        self._speech_turn_id: TurnId | None = None
        self._speech_item_id = ""
        self._muted = False
        self._mute_busy = False
        self._closed = False
        self._memory_reconfigure_pending = None
        self._memory_reconfigure_waiting = False
        self._memory_reconfigure_generation = 0
        self._memory_reconfigure_failed = False
        self._pending_text_deltas: list[tuple[str, str, object, str]] = []
        self._streamed_chars: dict[str, int] = {}
        # 流式增量按帧合并，避免每个 token 都重排整段回复
        self._text_delta_timer = QTimer(self)
        self._text_delta_timer.setSingleShot(True)
        self._text_delta_timer.timeout.connect(self._flush_text_deltas)
        self._memoryReconfigureFinished.connect(self._finish_memory_reconfigure)
        self._muteFinished.connect(self._finish_mute)
        pet_interaction.listenToggleRequested.connect(self._toggle_listening)
        pet_interaction.showMainRequested.connect(lambda: self.show_main("chat"))
        pet_interaction.positionChanged.connect(self._move_pet)
        pet_interaction.dragFinished.connect(self._finish_pet_drag)
        app_shell.showMainRequested.connect(self._show_main_window)
        app_shell.hideMainRequested.connect(self._hide_main_window)
        chat.errorOccurred.connect(lambda message: self._dialogs.toast(message, "warning"))
        self._dialogs.agentInteractionResolved.connect(self._resolve_agent_interaction)
        chat.voiceRecordingChanged.connect(self._sync_manual_voice_state)
        chat.activeSessionIdChanged.connect(self._reset_pet_speech)
        chat.activeSessionIdChanged.connect(self._restore_pet_phase)
        chat.processingChanged.connect(self._reset_speech_on_submission)
        chat.viewMemoryRequested.connect(lambda: self.show_main("memories"))
        if settings is not None:
            settings.configChanged.connect(self._apply_saved_settings)
        if pets is not None:
            pets.previewRequested.connect(self._preview_pet)
            pets.errorOccurred.connect(
                lambda message: self._dialogs.toast(message, "warning")
            )
        self._connect_optional_signal(tray, "open_requested", lambda: self.show_main("chat"))
        self._connect_optional_signal(tray, "settings_requested", lambda: self.show_main("settings"))
        self._connect_optional_signal(tray, "listen_toggle_requested", self._toggle_listening)
        self._connect_optional_signal(tray, "pet_visibility_requested", self._toggle_pet_visibility)
        self._connect_optional_signal(tray, "restore_interaction_requested", self._restore_interaction)
        self._connect_optional_signal(
            tray,
            "manual_input_requested",
            lambda: self.show_main("chat"),
        )
        self._connect_optional_signal(tray, "quit_requested", self.close)
        self._connect_optional_signal(tray, "context_menu_requested", self._show_tray_menu)
        self._window_coordinator.errorOccurred.connect(
            lambda message: self._dialogs.toast(message, "warning")
        )
        self.settingsApplyFailed.connect(
            lambda message: self._dialogs.toast(message, "warning")
        )
        if event_bridge is not None:
            signal = getattr(event_bridge, "event_received", None)
            if signal is not None:
                signal.connect(self.handle_runtime_event)
        if global_hotkey is not None:
            signal = getattr(global_hotkey, "activated", None)
            if signal is not None:
                signal.connect(self._toggle_listening)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @Slot(str, object)
    def _resolve_agent_interaction(self, approval_id: str, result: object) -> None:
        """把 Agent 窗口的选择回传到 Worker"""
        if self._runtime_host is None:
            return
        decision = str(result)
        if decision not in {"accept", "decline", "cancel"}:
            decision = "decline"
        try:
            self._runtime_host.resolve_agent_approval(approval_id, decision)
        except (RuntimeError, ValueError):
            self._dialogs.toast("Agent 审批回传失败", "warning")

    def start(self) -> None:
        self._qml_runtime.load()
        roots = self._qml_runtime.root_objects
        if not roots:
            raise RuntimeError("QML 主窗口不存在")
        self._main_window = roots[0]
        self._pet_window = self._main_window.findChild(QObject, "petWindow")
        if self._pet_window is not None:
            self._pet_speech_window = self._pet_window.findChild(
                QObject, "petSpeechWindow"
            )
            for signal_name in ("xChanged", "yChanged", "widthChanged", "heightChanged", "screenChanged"):
                getattr(self._pet_window, signal_name).connect(self._update_pet_screen_area)
            for screen in QGuiApplication.screens():
                screen.availableGeometryChanged.connect(self._update_pet_screen_area)
            self._update_pet_screen_area()
        self._pet_menu_window = self._main_window.findChild(
            QObject, "trayMenuWindow"
        )
        self._confirmation_window = self._main_window.findChild(
            QObject, "toolConfirmationWindow"
        )
        if self._pet_menu_window is not None:
            self._tray_menu_dismissal = TrayMenuDismissal(self._pet_menu_window, self)
            action_signal = getattr(self._pet_menu_window, "actionRequested", None)
            if action_signal is not None:
                action_signal.connect(self._handle_pet_menu_action)
            enable_menu = getattr(self._tray, "set_custom_menu_enabled", None)
            if callable(enable_menu):
                enable_menu(True)
        if self._runtime_host is not None:
            self._runtime_host.start()
            if not getattr(self._runtime_host, "audio_available", True):
                self._dialogs.toast("麦克风不可用，语音输入已停用，可直接用文字对话", "warning")
        self._chat.load_global_agent_mode()
        self._chat.refresh_sessions()
        self._chat.restore_current_session()
        if self._pets is not None:
            self._pets.refresh()
        self._restore_pet_position()
        show = getattr(self._tray, "show", None)
        if callable(show):
            show()
        self._apply_pet_preferences()
        if self._pet_window is not None:
            self._pet_window.setProperty("visible", True)
        self._assert_pet_topmost()

    def show_main(self, section: str = "chat") -> None:
        self._app_shell.show_main(section)

    @Slot(str)
    def _show_main_window(self, section: str) -> None:
        del section
        if self._main_window is None:
            return
        self._main_window.setProperty("visible", True)
        for method_name in ("raise_", "requestActivate"):
            method = getattr(self._main_window, method_name, None)
            if callable(method):
                method()

    @Slot()
    def _hide_main_window(self) -> None:
        if self._main_window is not None:
            self._main_window.setProperty("visible", False)

    @Slot()
    def _toggle_listening(self) -> None:
        if self._runtime_host is None:
            self._dialogs.toast("语音服务当前不可用", "warning")
            return
        try:
            if self._phase is ConversationPhase.IDLE:
                self._runtime_host.activate("click")
            else:
                self._runtime_host.cancel_active_turn()
        except RuntimeError as error:
            # 麦克风缺失等已知原因使用运行时给出的安全说明
            self._dialogs.set_page_error(
                "chat",
                getattr(error, "safe_message", "") or "语音操作提交失败，请重试",
            )

    @Slot(QPoint)
    def _show_tray_menu(self, anchor: QPoint) -> None:
        if self._pet_window is None or self._pet_menu_window is None:
            return
        ui = self._settings.config.ui if self._settings is not None else None
        self._pet_menu_window.setProperty("entries", [
            {"id": "open", "label": "打开主面板"},
            {"id": "listen", "label": "开始聆听" if self._phase is ConversationPhase.IDLE else "停止聆听"},
            self._mute_menu_entry(),
            {"id": "visibility", "label": "隐藏桌宠" if self._pet_window.property("visible") else "显示桌宠"},
            {"id": "clickThrough", "label": "关闭鼠标穿透" if ui and ui.pet_click_through else "开启鼠标穿透"},
            {"id": "topmost", "label": "取消置顶" if ui and ui.always_on_top else "保持置顶"},
            {"id": "quit", "label": "退出 VoicePet"},
        ])
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        menu_width = int(self._pet_menu_window.property("width"))
        menu_height = int(self._pet_menu_window.property("height"))
        x = anchor.x()
        y = anchor.y()
        if screen is not None:
            available = screen.availableGeometry()
            x = min(max(x, available.left()), available.right() - menu_width + 1)
            y = min(max(y, available.top()), available.bottom() - menu_height + 1)
        self._pet_menu_window.setProperty("x", x)
        self._pet_menu_window.setProperty("y", y)
        self._pet_menu_window.setProperty("visible", True)
        raise_window = getattr(self._pet_menu_window, "raise_", None)
        if callable(raise_window):
            raise_window()
        activate = getattr(self._pet_menu_window, "requestActivate", None)
        if callable(activate) and QGuiApplication.platformName() != "windows":
            activate()

    @Slot(str)
    def _handle_pet_menu_action(self, action: str) -> None:
        if action == "open" or action == "manual":
            self.show_main("chat")
        elif action == "settings":
            self.show_main("settings")
        elif action == "visibility":
            self._toggle_pet_visibility()
        elif action == "restore":
            self._restore_interaction()
        elif action == "quit":
            self.close()
        elif action == "listen":
            self._toggle_listening()
        elif action == "mute":
            self._toggle_mute()
        elif action == "clickThrough" and self._settings is not None:
            enabled = not self._settings.config.ui.pet_click_through
            self._settings.set_field("ui", "pet_click_through", enabled)
        elif action == "topmost" and self._settings is not None:
            enabled = not self._settings.config.ui.always_on_top
            self._settings.set_field("ui", "always_on_top", enabled)
        elif action == "hide" and self._pet_window is not None:
            self._pet_window.setProperty("visible", False)

    def _toggle_mute(self) -> None:
        if self._runtime_host is None or self._mute_busy:
            return
        candidate = not self._muted
        self._mute_busy = True
        self._refresh_mute_menu_entry()
        try:
            future = self._runtime_host.set_audio_muted(candidate)
        except (AttributeError, RuntimeError, TypeError):
            self._finish_mute(candidate, False)
            return

        def done(completed: object) -> None:
            try:
                completed.result()
                succeeded = True
            except Exception:  # noqa: BLE001 后台失败仅向界面发布安全状态
                succeeded = False
            if not self._closed:
                self._muteFinished.emit(candidate, succeeded)

        future.add_done_callback(done)

    @Slot(bool, bool)
    def _finish_mute(self, candidate: bool, succeeded: bool) -> None:
        if self._closed:
            return
        self._mute_busy = False
        if succeeded:
            # 临时静音只影响播放器，不覆盖用户保存的回复播报偏好
            self._muted = candidate
        else:
            self._dialogs.toast("静音切换失败，请重试", "warning")
        self._refresh_mute_menu_entry()

    def _mute_menu_entry(self) -> dict[str, object]:
        label = "正在切换静音…" if self._mute_busy else "取消静音" if self._muted else "静音"
        return {"id": "mute", "label": label, "enabled": not self._mute_busy}

    def _refresh_mute_menu_entry(self) -> None:
        if self._pet_menu_window is not None:
            entries = self._pet_menu_window.property("entries") or []
            if isinstance(entries, QJSValue):
                entries = entries.toVariant()
            self._pet_menu_window.setProperty("entries", [
                self._mute_menu_entry() if item.get("id") == "mute" else item
                for item in entries
            ])

    @Slot()
    def _toggle_pet_visibility(self) -> None:
        if self._pet_window is None:
            return
        visible = bool(self._pet_window.property("visible"))
        self._pet_window.setProperty("visible", not visible)
        if not visible:
            # 重新显示不会自动回到置顶带最前，需要再抬一次
            self._assert_pet_topmost()

    @Slot()
    def _restore_interaction(self) -> None:
        if self._pet_window is None:
            return
        if self._settings is not None:
            self._settings.set_field("ui", "pet_click_through", False)
            return
        self._window_coordinator.set_click_through(self._pet_window, False)

    @Slot()
    def _apply_pet_preferences(self) -> None:
        if self._pet_window is None or self._settings is None:
            return
        ui_config = self._settings.config.ui
        self._pet_window.setProperty("petScale", ui_config.pet_scale)
        self._pet_window.setProperty("alwaysOnTop", ui_config.always_on_top)
        windows = [self._pet_window]
        if self._pet_speech_window is not None:
            windows.append(self._pet_speech_window)
        applied = all(
            self._window_coordinator.set_click_through(
                window,
                ui_config.pet_click_through,
            )
            for window in windows
        )
        if ui_config.pet_click_through and not applied:
            for window in windows:
                self._window_coordinator.set_click_through(window, False)
            self._settings.set_field("ui", "pet_click_through", False)
        self._assert_pet_topmost()

    def _assert_pet_topmost(self) -> None:
        """置顶窗口之间也有上下关系，显示桌宠或改设置后主动抬一次"""

        if self._settings is None or not self._settings.config.ui.always_on_top:
            return
        for window in (self._pet_window, self._pet_speech_window):
            if window is not None:
                self._window_coordinator.raise_window(window)

    @Slot()
    def _apply_saved_settings(self) -> None:
        if self._settings is None:
            return
        previous = self._last_config
        current = self._settings.config
        self._last_config = current
        self._apply_pet_preferences()
        if previous is None:
            return
        if (
            previous.ui.start_at_login != current.ui.start_at_login
            and self._startup_manager is not None
        ):
            try:
                self._startup_manager.set_enabled(current.ui.start_at_login)
            except (AttributeError, OSError, RuntimeError):
                self.settingsApplyFailed.emit(
                    "设置已保存，但开机启动设置失败，请检查系统权限"
                )
        if self._global_hotkey is not None:
            if previous.ui.global_hotkey != current.ui.global_hotkey:
                replace_shortcut = getattr(self._global_hotkey, "try_replace", None)
                if callable(replace_shortcut):
                    try:
                        replace_shortcut(current.ui.global_hotkey)
                    except (OSError, RuntimeError, ValueError, TypeError):
                        self.settingsApplyFailed.emit("全局快捷键更新失败")
            if (
                previous.ui.global_hotkey_enabled
                != current.ui.global_hotkey_enabled
            ):
                set_enabled = getattr(self._global_hotkey, "set_enabled", None)
                if callable(set_enabled):
                    try:
                        set_enabled(current.ui.global_hotkey_enabled)
                    except (OSError, RuntimeError, ValueError, TypeError):
                        self.settingsApplyFailed.emit("全局快捷键开关失败")
        if self._runtime_host is None:
            return
        if previous.llm != current.llm:
            self._memory_reconfigure_pending = current.privacy
            self._memory_reconfigure_waiting = True
            self._memory_reconfigure_generation += 1
            generation = self._memory_reconfigure_generation
            try:
                future = self._runtime_host.invalidate_memory()
            except (AttributeError, RuntimeError):
                self._finish_memory_reconfigure(generation, False)
            else:
                def done(completed):
                    try:
                        completed.result()
                    except Exception:  # noqa: BLE001 旧服务暂停结果经信号回到主线程
                        self._memoryReconfigureFinished.emit(generation, False)
                    else:
                        self._memoryReconfigureFinished.emit(generation, True)
                future.add_done_callback(done)
        elif previous.privacy != current.privacy:
            if self._memory_reconfigure_waiting:
                self._memory_reconfigure_pending = current.privacy
            elif not self._memory_reconfigure_failed:
                self._configure_memory_now(current.privacy)
        runtime_changes = (
            (
                previous.tts.enabled != current.tts.enabled,
                lambda: self._runtime_host.set_speech_enabled(current.tts.enabled),
                "语音播报切换失败，重启后生效",
            ),
            (
                previous.tts.manual_input_enabled != current.tts.manual_input_enabled,
                lambda: self._runtime_host.set_manual_input_speech_enabled(
                    current.tts.manual_input_enabled
                ),
                "手动输入播报切换失败，重启后生效",
            ),
            (
                previous.wake_word != current.wake_word,
                lambda: self._runtime_host.configure_wake_word(current.wake_word),
                "语音唤醒应用失败，请重试",
            ),
        )
        for changed, starter, failure_message in runtime_changes:
            if not changed:
                continue
            try:
                future = starter()
            except (AttributeError, RuntimeError):
                self.settingsApplyFailed.emit(failure_message)
                continue
            future.add_done_callback(
                lambda completed, message=failure_message: self._report_setting_result(
                    completed,
                    message,
                )
            )

    def _configure_memory_now(self, privacy) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.configure_memory(privacy)
        except (AttributeError, RuntimeError):
            self.settingsApplyFailed.emit("记忆设置应用失败，请退出并重启")
            return
        future.add_done_callback(lambda completed: self._report_setting_result(completed, "记忆设置应用失败，请退出并重启"))

    @Slot(int, bool)
    def _finish_memory_reconfigure(self, generation: int, succeeded: bool) -> None:
        if self._closed or generation != self._memory_reconfigure_generation:
            return
        self._memory_reconfigure_failed = not succeeded
        privacy = self._memory_reconfigure_pending
        self._memory_reconfigure_pending = None
        self._memory_reconfigure_waiting = False
        if not succeeded:
            self.settingsApplyFailed.emit("旧服务的记忆整理暂停失败，请退出并重启")
        elif privacy is not None:
            self._configure_memory_now(privacy)

    def _report_setting_result(self, future: object, message: str) -> None:
        try:
            future.result()
        except Exception:  # noqa: BLE001
            self.settingsApplyFailed.emit(message)

    @Slot(str)
    def _preview_pet(self, pet_id: str) -> None:
        if self._pets is None:
            return
        directory = self._pets.directory_for(pet_id)
        if directory is None:
            self._dialogs.toast("桌宠形象不存在", "warning")
            return
        try:
            self._pet_animation.load_directory(directory)
        except (OSError, ValueError, PetAssetError, QmlResourceError):
            self._dialogs.toast("桌宠形象加载失败，已保留当前形象", "warning")

    @Slot(float, float)
    def _move_pet(self, delta_x: float, delta_y: float) -> None:
        if self._pet_window is None:
            return
        if self._pet_drag_position is None:
            self._pet_drag_position = QPointF(
                self._pet_window.property("x"), self._pet_window.property("y")
            )
        # 保留缩放屏幕产生的小数位移，避免每帧取整累积跟手误差。
        self._pet_drag_position += QPointF(delta_x, delta_y)
        # 拖动过程中允许跨越屏幕接缝；逐帧约束会吞掉越界增量，卡在原屏。
        self._set_pet_position(self._pet_drag_position.toPoint())

    @Slot()
    def _finish_pet_drag(self) -> None:
        self._pet_drag_position = None
        if self._pet_window is None:
            return
        position = QPoint(
            int(self._pet_window.property("x")),
            int(self._pet_window.property("y")),
        )
        self._set_pet_position(self._clamp_pet_position(position))

    def _set_pet_position(self, position: QPoint) -> None:
        self._pet_window.setProperty("x", position.x())
        self._pet_window.setProperty("y", position.y())
        if self._position_store is None:
            return
        try:
            self._position_store.save(WindowPosition(position.x(), position.y()))
        except (TypeError, ValueError, WindowStateError):
            self._dialogs.toast("桌宠位置保存失败", "warning")

    def _restore_pet_position(self) -> None:
        if self._pet_window is None or self._position_store is None:
            return
        try:
            position = self._position_store.load()
        except (OSError, RuntimeError, ValueError):
            position = None
        if isinstance(position, WindowPosition):
            clamped = self._clamp_pet_position(QPoint(position.x, position.y))
            self._pet_window.setProperty("x", clamped.x())
            self._pet_window.setProperty("y", clamped.y())

    def _clamp_pet_position(self, position: QPoint) -> QPoint:
        if self._pet_window is None:
            return QPoint(position)
        screens = [screen.availableGeometry() for screen in QGuiApplication.screens()]
        size = QSize(
            int(self._pet_window.property("width")),
            int(self._pet_window.property("height")),
        )
        return self._window_coordinator.clamp_pet_position(
            position,
            size,
            screens,
        )

    @Slot()
    def _update_pet_screen_area(self) -> None:
        if self._closed or self._pet_window is None:
            return
        center = QPoint(
            int(self._pet_window.property("x") + self._pet_window.property("width") / 2),
            int(self._pet_window.property("y") + self._pet_window.property("height") / 2),
        )
        # 使用桌宠实际所在屏幕的可用矩形，包含副屏负坐标和任务栏占用
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        if screen is not None:
            self._pet_window.setProperty("availableArea", QRectF(screen.availableGeometry()))

    def _set_pet_speech(self, message: str) -> None:
        if self._pet_window is not None:
            self._pet_window.setProperty("speech", sanitize_markdown(message))

    @Slot()
    def _sync_manual_voice_state(self) -> None:
        # 手动录音不经过对话状态机，因此由聊天 ViewModel 直接同步桌宠
        if self._chat.voiceRecording:
            self._pet_animation.set_phase(ConversationPhase.LISTENING)
            self._set_pet_speech("正在聆听，请说话…")
        elif self._phase is ConversationPhase.IDLE:
            self._pet_animation.set_phase(ConversationPhase.IDLE)
            self._set_pet_speech("")

    @Slot()
    def _reset_pet_speech(self) -> None:
        # 切换会话时同步清理可见气泡和增量缓存，防止旧回复再次出现
        self._speech_text = ""
        self._speech_turn_id = None
        self._speech_item_id = ""
        self._set_pet_speech("")

    @Slot()
    def _restore_pet_phase(self) -> None:
        """切回仍在执行的会话时，把桌宠与托盘恢复到该会话的当前状态"""

        session_id = self._chat.activeSessionId
        phase = self._session_phases.get(session_id, ConversationPhase.IDLE)
        self._phase = phase
        self._phase_turn_id = (
            None if phase is ConversationPhase.IDLE else self._session_turns.get(session_id)
        )
        # 聆听沿用待机动画，与前台事件保持同一套映射
        self._pet_animation.set_phase(
            ConversationPhase.IDLE if phase is ConversationPhase.LISTENING else phase
        )
        setter = getattr(self._tray, "set_listening", None)
        if callable(setter):
            setter(phase is not ConversationPhase.IDLE)

    @Slot()
    def _reset_speech_on_submission(self) -> None:
        if self._chat.processing:
            self._reset_pet_speech()

    def _queue_text_delta(
        self, session_id: str, item_id: str, turn_id: object, text: str
    ) -> None:
        """流式增量按帧合并：首个增量立即生效，同一窗口内其余增量攒成一次更新"""

        key = session_id or ""
        streamed = self._streamed_chars.get(key, 0) + len(text)
        self._streamed_chars[key] = streamed
        if streamed <= _STREAM_COALESCE_AFTER:
            self._apply_text_delta(session_id, item_id, turn_id, text)
            return
        self._pending_text_deltas.append((session_id, item_id, turn_id, text))
        if self._text_delta_timer.isActive():
            return
        self._flush_text_deltas()
        self._text_delta_timer.start(self._flush_interval_ms())

    @Slot()
    def _flush_text_deltas(self) -> None:
        pending = self._pending_text_deltas
        if not pending:
            return
        self._pending_text_deltas = []
        merged: list[tuple[str, str, object, str]] = []
        for session_id, item_id, turn_id, text in pending:
            head = (session_id, item_id, turn_id)
            if merged and merged[-1][:3] == head:
                merged[-1] = (*head, merged[-1][3] + text)
            else:
                merged.append((*head, text))
        for session_id, item_id, turn_id, text in merged:
            self._apply_text_delta(session_id, item_id, turn_id, text)

    def _settle_text_deltas(self) -> None:
        self._text_delta_timer.stop()
        self._flush_text_deltas()

    def _flush_interval_ms(self) -> int:
        """整段重排的开销随文本变长，按长度放缓刷新，避免主线程被流式更新占满"""

        longest = max(self._streamed_chars.values(), default=0)
        if longest <= 4_000:
            return 60
        if longest <= 10_000:
            return 120
        return 240

    def _apply_text_delta(
        self, session_id: str, item_id: str, turn_id: object, text: str
    ) -> None:
        self._chat.append_assistant_delta(text, item_id, session_id)
        if not self._is_foreground_session(session_id):
            return
        if turn_id != self._speech_turn_id:
            self._reset_pet_speech()
            self._speech_turn_id = turn_id
            self._speech_item_id = item_id
        elif item_id != self._speech_item_id:
            # 同一轮的下一段回复另起一条气泡，不跟上一段拼在一起
            self._speech_item_id = item_id
            self._speech_text = ""
        self._speech_text += text
        self._set_pet_speech(self._speech_text)

    @Slot(object)
    def handle_runtime_event(self, event: object) -> None:
        if not isinstance(event, TextDelta):
            # 状态类事件之前先落下攒着的增量，避免文本落后于阶段切换
            self._settle_text_deltas()
        if isinstance(event, StateChanged):
            # 并行会话的状态变化各归各的轮次，迟到事件不能收掉别人的收尾
            if self._is_stale_turn(event):
                return
            # 记住每个会话最近的状态，切回后台会话时据此恢复桌宠
            session_key = event.session_id or ""
            if event.current is ConversationPhase.IDLE:
                self._session_phases.pop(session_key, None)
                self._streamed_chars.pop(session_key, None)
            else:
                self._session_phases[session_key] = event.current
            if not self._is_foreground_session(event.session_id):
                # 后台会话只更新自己的聊天条目，不驱动宠物、托盘和气泡
                if event.current is not ConversationPhase.IDLE:
                    self._chat.begin_turn(event.session_id)
                else:
                    self._chat.finish_assistant(event.session_id)
                return
            if event.current is not ConversationPhase.IDLE:
                self._chat.begin_turn(event.session_id)
            # 手动输入直接进入思考阶段，不能只在开始聆听时重置缓存
            if event.current is not ConversationPhase.IDLE and event.turn_id != self._speech_turn_id:
                self._reset_pet_speech()
                self._speech_turn_id = event.turn_id
            self._phase = event.current
            self._phase_turn_id = (
                None if event.current is ConversationPhase.IDLE else event.turn_id
            )
            self._pet_animation.set_phase(
                ConversationPhase.IDLE
                if event.current is ConversationPhase.LISTENING else event.current
            )
            if event.previous is ConversationPhase.LISTENING:
                self._set_pet_speech("")
            if event.current is ConversationPhase.LISTENING:
                self._speech_text = ""
                self._set_pet_speech("")
            listening = event.current is not ConversationPhase.IDLE
            setter = getattr(self._tray, "set_listening", None)
            if callable(setter):
                setter(listening)
            if event.current is ConversationPhase.IDLE:
                self._chat.finish_assistant(event.session_id)
            return
        if isinstance(event, WakeCommandPending):
            if event.turn_id == self._phase_turn_id:
                self._set_pet_speech("")
            return
        if isinstance(event, RecordingStarted):
            if event.turn_id == self._phase_turn_id and self._phase is ConversationPhase.LISTENING:
                self._pet_animation.set_phase(ConversationPhase.LISTENING)
                self._set_pet_speech("我在听…")
            return
        if isinstance(event, SpeakRequested):
            if event.turn_id != self._phase_turn_id:
                return
            # 唤醒应答播放期间展示与语音一致的简短气泡
            if self._phase is ConversationPhase.LISTENING and event.text == "我在，请说":
                self._set_pet_speech("我在")
            elif self._phase is ConversationPhase.SPEAKING:
                # 播报按段推进，气泡跟着当前播报的段落走，历史重播同样一段一弹
                self._set_pet_speech(event.text)
            return
        if isinstance(event, TranscriptReady):
            self._chat.append_user_message(event.text, event.session_id)
            self._set_pet_speech("")
            return
        if isinstance(event, TextDelta):
            self._queue_text_delta(
                event.session_id, event.item_id, event.turn_id, event.text
            )
            return
        if isinstance(event, ApprovalRequested):
            self._set_pet_speech(f"需要确认 {event.risk}：{event.summary}")
            self._queue_approval(event)
            return
        if isinstance(event, MemoryResultReady):
            # 仅展示当前显式确认的结果，自动提取候选和摘要不插入聊天回复
            if (
                self._phase is ConversationPhase.AWAITING_APPROVAL
                and event.turn_id == self._speech_turn_id
                and event.status in {"success", "denied", "failed"}
            ):
                self._chat.append_assistant_delta(
                    event.message, session_id=event.session_id
                )
                self._speech_text = event.message
                self._set_pet_speech(self._speech_text)
            return
        if isinstance(event, MemoryChanged):
            if self._memories is not None:
                self._memories.refresh()
                self._memories.refresh_changes()
            if event.session_id == self._chat.memorySessionId:
                self._chat.refresh_memory_changes()
            return
        if isinstance(event, MemoryMaintenanceChanged):
            if self._memories is not None:
                self._memories.set_maintenance_status(event.status, event.message)
                self._memories.refresh_summaries()
            return
        if isinstance(event, AgentProgress):
            # 执行状态只显示在桌宠气泡，避免污染最终聊天回复；后台会话不会发布该事件
            self._set_pet_speech(event.message)
            return
        if isinstance(event, AgentApprovalRequested):
            self._dialogs.request_agent_interaction(
                AgentInteractionRequest(event.approval_id, "Agent 请求确认", event.summary, options=event.options)
            )
            return
        if isinstance(event, RuntimeErrorEvent):
            if not self._is_foreground_session(event.session_id):
                # 后台会话不打断当前操作，失败原因留在它自己的聊天记录里
                self._chat.append_assistant_delta(
                    event.safe_message, session_id=event.session_id
                )
                return
            self._dialogs.set_page_error("chat", event.safe_message)
            self._set_pet_speech(event.safe_message)

    def _is_foreground_session(self, session_id: object) -> bool:
        """没有会话标识的旧链路按前台处理，后台会话不驱动桌宠和提示"""

        if not isinstance(session_id, str) or not session_id:
            return True
        active = self._chat.activeSessionId
        # 聊天还没恢复出当前会话时无法判断归属，按前台处理
        return not active or session_id == active

    def _is_stale_turn(self, event: StateChanged) -> bool:
        """按会话跟踪当前轮次，并行会话里迟到的结束事件不能收掉新轮次"""

        key = event.session_id or ""
        if event.current is not ConversationPhase.IDLE:
            self._session_turns[key] = event.turn_id
            return False
        tracked = self._session_turns.pop(key, None)
        # 同一会话已经有更晚的轮次在跑，旧轮次的结束事件直接丢弃
        return tracked is not None and tracked != event.turn_id

    def _queue_approval(self, event: ApprovalRequested) -> None:
        request = ConfirmationRequest(
            str(event.tool_call_id),
            "允许这项操作？",
            event.summary,
            "操作将由本机工具执行",
            False,
            event.risk,
            channel=WINDOW_CHANNEL,
        )
        approve = None
        reject = None
        if self._runtime_host is not None:
            approve = lambda: self._runtime_host.approve()
            reject = lambda: self._runtime_host.reject()
        self._dialogs.confirm(request, approve, reject)
        main_visible = bool(
            self._main_window is not None and self._main_window.property("visible")
        )
        if not main_visible and self._confirmation_window is not None:
            self._confirmation_window.setProperty("visible", True)

    @Slot()
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._tray_menu_dismissal is not None:
            self._tray_menu_dismissal.close()
        if self._runtime_host is not None:
            try:
                self._runtime_host.close()
            except RuntimeError:
                pass
        if self._event_bridge is not None:
            close = getattr(self._event_bridge, "close", None)
            if callable(close):
                close()
        if self._global_hotkey is not None:
            close = getattr(self._global_hotkey, "close", None)
            if callable(close):
                close()
        close_tray = getattr(self._tray, "close", None)
        if callable(close_tray):
            close_tray()
        self._qml_runtime.close()
        quit_application = getattr(self._application, "quit", None)
        if callable(quit_application):
            quit_application()

    @staticmethod
    def _connect_optional_signal(source: QObject, name: str, callback: Any) -> None:
        signal = getattr(source, name, None)
        if signal is not None:
            signal.connect(callback)
