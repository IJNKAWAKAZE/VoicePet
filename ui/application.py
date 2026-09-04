"""Qt 主进程窗口、托盘与配置保存生命周期"""

from __future__ import annotations

import sys
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

from core.asr import AsrModelState
from core.config import AppConfig, ConfigError, ConfigStore, default_config_path
from core.config_application import ConfigApplicationMode, classify_config_change
from core.credentials import CredentialError, DpapiCredentialStore
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    MemoryResultReady,
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TextInputSubmitted,
    ToolResultReady,
    TranscriptReady,
    WakeCommandPending,
)
from core.policy import ConfirmationMode
from core.runtime import RuntimeHost, RuntimeServices
from core.runtime_factory import build_default_runtime
from core.startup import StartupError, WindowsStartupManager
from core.state_machine import InvalidTransition
from core.wake import WakeRuntimeStatus
from core.wake_models import WakeDownloadProgress, WakeModelState
from core.window_state import (
    WindowPosition,
    WindowPositionStore,
    WindowStateError,
)

from .confirm_dialog import ConfirmationDialog
from .event_bridge import QtEventBridge
from .global_hotkey import create_global_hotkey
from .manual_input import ManualInputDialog
from .pet_shell import (
    PetChoice,
    PetShellWindow,
    discover_pet_choices,
    resolve_active_pet_directory,
)
from .settings_window import SettingsWindow
from .tray import TrayController

_CONFIRMATION_DIALOG_STYLE = """
QMessageBox { background: #081827; color: #EAF6F5; font-family: "Microsoft YaHei UI"; }
QMessageBox QLabel { color: #EAF6F5; padding: 6px 4px; }
QMessageBox QLabel#qt_msgbox_label { min-width: 360px; max-width: 460px; }
QMessageBox QLabel#qt_msgboxex_icon_label { min-width: 48px; max-width: 48px; padding: 6px 0; }
QMessageBox QPushButton { min-width: 96px; min-height: 32px; border-radius: 6px; padding: 4px 14px; }
QMessageBox QPushButton#download_model_action { background: #65D6D0; color: #071521; border: 1px solid #65D6D0; font-weight: 700; }
QMessageBox QPushButton#download_model_action:hover { background: #86E5DF; }
QMessageBox QPushButton#danger_action { background: #D65D66; color: #FFFFFF; border: 1px solid #D65D66; font-weight: 700; }
QMessageBox QPushButton#danger_action:hover { background: #E97880; border-color: #E97880; }
QMessageBox QPushButton#danger_action:pressed { background: #B94B54; border-color: #B94B54; }
QMessageBox QPushButton#cancel_action { background: #102B3F; color: #EAF6F5; border: 1px solid #52758A; }
QMessageBox QPushButton#cancel_action:hover { border-color: #65D6D0; }
"""


def _build_model_download_dialog(
    parent: QWidget,
    *,
    title: str,
    message: str,
) -> QMessageBox:
    """构建文字始终可见的模型下载确认框"""

    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setWindowTitle(title)
    dialog.setText(message)
    dialog.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    confirm_button = dialog.button(QMessageBox.StandardButton.Yes)
    cancel_button = dialog.button(QMessageBox.StandardButton.No)
    confirm_button.setObjectName("download_model_action")
    confirm_button.setText("下载模型")
    cancel_button.setObjectName("cancel_action")
    cancel_button.setText("取消")
    dialog.setDefaultButton(QMessageBox.StandardButton.No)
    dialog.setEscapeButton(cancel_button)
    dialog.setStyleSheet(_CONFIRMATION_DIALOG_STYLE)
    return dialog


def _confirm_model_download(
    parent: QWidget,
    *,
    title: str,
    message: str,
) -> bool:
    """显示模型下载确认框并返回明确选择"""

    dialog = _build_model_download_dialog(
        parent,
        title=title,
        message=message,
    )
    result = QMessageBox.StandardButton(dialog.exec())
    return result == QMessageBox.StandardButton.Yes


def _build_session_clear_dialog(parent: QWidget) -> QMessageBox:
    """构建深色且使用中文按钮的会话清空确认框"""

    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setWindowTitle("清空全部会话")
    dialog.setText("这会永久删除全部近期会话和短期摘要，是否继续？")
    dialog.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    clear_button = dialog.button(QMessageBox.StandardButton.Yes)
    cancel_button = dialog.button(QMessageBox.StandardButton.No)
    clear_button.setObjectName("danger_action")
    clear_button.setText("清空全部")
    cancel_button.setObjectName("cancel_action")
    cancel_button.setText("取消")
    dialog.setDefaultButton(QMessageBox.StandardButton.No)
    dialog.setEscapeButton(cancel_button)
    dialog.setStyleSheet(_CONFIRMATION_DIALOG_STYLE)
    return dialog


def _confirm_session_clear(parent: QWidget) -> bool:
    """显示会话清空确认框并返回明确选择"""

    dialog = _build_session_clear_dialog(parent)
    result = QMessageBox.StandardButton(dialog.exec())
    return result == QMessageBox.StandardButton.Yes


def _build_session_delete_dialog(parent: QWidget) -> QMessageBox:
    """构建单个会话删除确认框"""

    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setWindowTitle("删除会话")
    dialog.setText("这会永久删除此会话中的全部消息，是否继续？")
    dialog.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    delete_button = dialog.button(QMessageBox.StandardButton.Yes)
    cancel_button = dialog.button(QMessageBox.StandardButton.No)
    delete_button.setObjectName("danger_action")
    delete_button.setText("删除会话")
    cancel_button.setObjectName("cancel_action")
    cancel_button.setText("取消")
    dialog.setDefaultButton(QMessageBox.StandardButton.No)
    dialog.setEscapeButton(cancel_button)
    dialog.setStyleSheet(_CONFIRMATION_DIALOG_STYLE)
    return dialog


def _confirm_session_delete(parent: QWidget) -> bool:
    """显示单个会话删除确认框并返回明确选择"""

    dialog = _build_session_delete_dialog(parent)
    result = QMessageBox.StandardButton(dialog.exec())
    return result == QMessageBox.StandardButton.Yes


class RuntimeHostProtocol(Protocol):
    """Qt 控制器使用的 Runtime 工作线程边界"""

    @property
    def llm_configured(self) -> bool: ...

    def start(self) -> None: ...

    def activate(self, source: str) -> Future[Any]: ...

    def speak_notice(self, text: str) -> Future[Any]: ...

    def submit_text(self, text: str) -> Future[Any]: ...

    def set_speech_enabled(self, enabled: bool) -> Future[None]: ...

    def set_manual_input_speech_enabled(self, enabled: bool) -> Future[None]: ...

    def list_tts_voices(self) -> Future[Any]: ...

    def preview_tts_voice(self, voice_name: str) -> Future[None]: ...

    def asr_model_state(self) -> Future[AsrModelState]: ...

    def download_asr_model(self) -> Future[None]: ...

    def load_asr_model(self) -> Future[None]: ...

    def approve(self, mode: ConfirmationMode) -> Future[Any]: ...

    def reject(self) -> Future[Any]: ...

    def list_memories(self) -> Future[Any]: ...

    def delete_memory(self, memory_id: str) -> Future[Any]: ...

    def confirm_memory(self, memory_id: str) -> Future[Any]: ...

    def resolve_memory_conflict(self, memory_id: str) -> Future[Any]: ...

    def export_memories(self, destination: str) -> Future[Any]: ...

    def list_sessions(self) -> Future[Any]: ...

    def current_session_id(self) -> Future[str]: ...

    def list_session_turns(self, session_id: str) -> Future[Any]: ...

    def activate_session(self, session_id: str) -> Future[Any]: ...

    def new_session(self) -> Future[str]: ...

    def delete_session(self, turn_id: str) -> Future[Any]: ...

    def clear_sessions(self) -> Future[Any]: ...

    def install_pet(self, source: str) -> Future[Any]: ...

    def wake_model_state(self) -> Future[WakeModelState]: ...

    def wake_runtime_status(self) -> Future[WakeRuntimeStatus]: ...

    def configure_wake_word(self, config: object) -> Future[None]: ...

    def download_wake_model(
        self,
        progress: Callable[[WakeDownloadProgress], None],
    ) -> Future[None]: ...

    def cancel_wake_model_download(self) -> None: ...

    def run_diagnostics(self) -> Future[Any]: ...

    def export_diagnostics(self, destination: str) -> Future[Any]: ...


class CredentialStoreProtocol(Protocol):
    """Qt 设置页使用的加密凭据存储边界"""

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> bool: ...

    def close(self) -> None: ...


class StartupManagerProtocol(Protocol):
    """设置页使用的当前用户开机启动边界"""

    def set_enabled(self, enabled: bool) -> None: ...


class WindowPositionStoreProtocol(Protocol):
    """桌宠位置持久化边界"""

    def load(self) -> WindowPosition | None: ...

    def save(self, position: WindowPosition) -> None: ...


_RUNTIME_EVENT_TYPES = (
    StateChanged,
    TranscriptReady,
    TextInputSubmitted,
    TextDelta,
    ApprovalRequested,
    ToolResultReady,
    SpeakRequested,
    RuntimeErrorEvent,
    MemoryResultReady,
    WakeCommandPending,
)


class ApplicationController(QObject):
    """集中拥有 Qt 顶层对象并执行显式退出顺序"""

    wake_requested = Signal()
    runtime_failed = Signal(str)
    memory_records_ready = Signal(object)
    memory_status_ready = Signal(str)
    session_records_ready = Signal(object)
    session_detail_ready = Signal(object)
    chat_session_ready = Signal(object)
    session_activated_ready = Signal(object)
    new_session_ready = Signal(str)
    session_status_ready = Signal(str)
    manual_input_succeeded = Signal()
    manual_input_failed = Signal(str)
    pet_installed = Signal(object)
    diagnostic_report_ready = Signal(object)
    notice_finished = Signal()
    asr_state_ready = Signal(object)
    asr_download_ready = Signal()
    asr_load_ready = Signal()
    asr_preparation_failed = Signal()
    wake_model_state_ready = Signal(object)
    wake_runtime_status_ready = Signal(object)
    wake_download_progress = Signal(object)
    wake_download_ready = Signal()
    wake_download_failed = Signal()
    wake_configuration_failed = Signal()
    voice_catalog_ready = Signal(object)
    voice_catalog_failed = Signal()
    voice_preview_ready = Signal()
    voice_preview_failed = Signal()

    def __init__(
        self,
        application: QApplication,
        config_store: ConfigStore,
        config: AppConfig,
        *,
        runtime_host: RuntimeHostProtocol | None = None,
        event_bridge: QtEventBridge | None = None,
        credential_store: CredentialStoreProtocol | None = None,
        pet_directory: str | Path | None = None,
        catalog_loader: Callable[[], tuple[PetChoice, ...]] | None = None,
        global_hotkey: Any | None = None,
        startup_manager: StartupManagerProtocol | None = None,
        position_store: WindowPositionStoreProtocol | None = None,
    ) -> None:
        super().__init__()
        self._application = application
        self._config_store = config_store
        self._config = config
        self._runtime_host = runtime_host
        self._event_bridge = event_bridge
        self._credential_store = credential_store
        self._catalog_loader = catalog_loader
        self._pet_choices: dict[str, PetChoice] = {}
        self._last_preview_pet_id = config.ui.active_skin
        self._global_hotkey = global_hotkey
        self._startup_manager = startup_manager
        self._position_store = position_store
        self._response_text = ""
        self._conversation_phase = ConversationPhase.IDLE
        self._runtime_error_visible = False
        self._notice_pending = False
        self._pending_activation_source: str | None = None
        self._asr_preparation: str | None = None
        self._startup_checks_started = False
        self._voice_catalog_loading = False
        self._voice_catalog_loaded = False
        self._voice_preview_active = False
        self._wake_prompted = False
        self._wake_download_active = False
        self._closed = False
        self.settings = SettingsWindow(config)
        self.manual_input = ManualInputDialog()
        self.pet = PetShellWindow(
            always_on_top=config.ui.always_on_top,
            pet_directory=pet_directory,
        )
        self.confirmation = ConfirmationDialog(parent=self.pet)
        self.tray = TrayController(self)
        self.tray.wake_requested.connect(self._activate_runtime)
        self.tray.manual_input_requested.connect(self.show_manual_input)
        self.pet.activation_requested.connect(self._activate_runtime)
        self.pet.position_changed.connect(self._save_pet_position)
        if self._global_hotkey is not None:
            self._global_hotkey.activated.connect(
                lambda: self._activate_runtime("hotkey")
            )
        self.tray.settings_requested.connect(self.show_settings)
        self.tray.quit_requested.connect(self.close)
        self.settings.save_requested.connect(self._save_config)
        self.settings.memory_refresh_requested.connect(self._refresh_memories)
        self.settings.memory_delete_requested.connect(self._delete_memory)
        self.settings.memory_confirm_requested.connect(self._confirm_memory)
        self.settings.memory_resolve_requested.connect(
            self._resolve_memory_conflict
        )
        self.settings.memory_export_requested.connect(self._export_memories)
        self.settings.session_refresh_requested.connect(self._refresh_sessions)
        self.settings.session_delete_requested.connect(self._delete_session)
        self.settings.session_clear_requested.connect(self._clear_sessions)
        self.settings.session_selection_changed.connect(
            self._load_session_detail
        )
        self.settings.session_activate_requested.connect(
            self._activate_session
        )
        self.settings.session_new_requested.connect(self._new_session)
        self.settings.credential_save_requested.connect(self._save_api_key)
        self.settings.credential_delete_requested.connect(self._delete_api_key)
        self.settings.pet_file_import_requested.connect(self._import_pet_file)
        self.settings.pet_directory_import_requested.connect(
            self._import_pet_directory
        )
        self.settings.pet_selection_changed.connect(self._preview_pet_selection)
        self.settings.wake_model_download_requested.connect(
            self._start_wake_model_download
        )
        self.settings.wake_model_download_cancel_requested.connect(
            self._cancel_wake_model_download
        )
        self.settings.voice_preview_requested.connect(self._preview_tts_voice)
        self.settings.diagnostics_run_requested.connect(self._run_diagnostics)
        self.settings.diagnostics_export_requested.connect(
            self._export_diagnostics
        )
        self.confirmation.accepted.connect(self._approve_tool)
        self.confirmation.rejected.connect(self._reject_tool)
        self.runtime_failed.connect(self._show_runtime_error)
        self.manual_input.submitted.connect(self._submit_manual_text)
        self.manual_input.new_session_requested.connect(self._new_session)
        self.manual_input.session_activate_requested.connect(
            self._activate_session
        )
        self.manual_input.session_delete_requested.connect(
            self._delete_session
        )
        self.manual_input_succeeded.connect(
            self.manual_input.submission_succeeded
        )
        self.manual_input_failed.connect(self.manual_input.show_error)
        self.memory_records_ready.connect(self.settings.set_memory_records)
        self.memory_status_ready.connect(self.settings.validation_message.setText)
        self.session_records_ready.connect(self.settings.set_session_records)
        self.session_records_ready.connect(
            self.manual_input.set_session_records
        )
        self.session_detail_ready.connect(self.settings.set_session_detail)
        self.chat_session_ready.connect(self._apply_chat_session)
        self.session_activated_ready.connect(self._apply_session_activated)
        self.new_session_ready.connect(self._apply_new_session)
        self.session_status_ready.connect(
            self.settings.validation_message.setText
        )
        self.pet_installed.connect(self._apply_installed_pet)
        self.diagnostic_report_ready.connect(
            self.settings.set_diagnostic_report
        )
        self.notice_finished.connect(self._apply_notice_finished)
        self.asr_state_ready.connect(self._apply_asr_model_state)
        self.asr_download_ready.connect(self._apply_asr_download_ready)
        self.asr_load_ready.connect(self._apply_asr_load_ready)
        self.asr_preparation_failed.connect(self._apply_asr_preparation_failed)
        self.wake_model_state_ready.connect(self._apply_wake_model_state)
        self.wake_runtime_status_ready.connect(self._apply_wake_runtime_status)
        self.wake_download_progress.connect(self._apply_wake_download_progress)
        self.wake_download_ready.connect(self._apply_wake_download_ready)
        self.wake_download_failed.connect(self._apply_wake_download_failed)
        self.wake_configuration_failed.connect(
            self._apply_wake_configuration_failed
        )
        self.voice_catalog_ready.connect(self._apply_tts_voice_catalog)
        self.voice_catalog_failed.connect(self._apply_tts_voice_catalog_failed)
        self.voice_preview_ready.connect(self._apply_tts_voice_preview_ready)
        self.voice_preview_failed.connect(self._apply_tts_voice_preview_failed)
        if self._event_bridge is not None:
            self._event_bridge.event_received.connect(self._handle_runtime_event)
        self._refresh_pet_choices(config.ui.active_skin)

    @property
    def is_closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        if self._startup_manager is not None:
            try:
                self._startup_manager.set_enabled(
                    self._config.ui.start_at_login
                )
            except StartupError:
                pass
        if self._runtime_host is not None:
            try:
                self._runtime_host.start()
            except Exception as error:  # noqa: BLE001 Runtime 启动失败时仍保留桌宠界面
                _ = error
                self.runtime_failed.emit("Runtime 启动失败，当前仅保留界面功能")
            else:
                if not self._startup_checks_started:
                    self._startup_checks_started = True
                    self._prepare_cached_asr()
                    self._check_wake_model()
        self.pet.show()
        self._restore_pet_position()
        self.tray.show()

    def show_settings(self) -> None:
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()
        self._load_tts_voice_catalog()
        self._refresh_memories()
        self._refresh_sessions()

    def show_manual_input(self) -> None:
        """从托盘非阻塞打开手动输入窗口"""

        if self._closed:
            return
        self.manual_input.open_for_input()
        self._refresh_chat_session()
        self._refresh_sessions()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._notice_pending = False
        self._pending_activation_source = None
        self._asr_preparation = None
        if self._wake_download_active and self._runtime_host is not None:
            try:
                self._runtime_host.cancel_wake_model_download()
            except Exception as error:  # noqa: BLE001 退出仍需继续关闭资源
                _ = error
        self._wake_download_active = False
        if self._runtime_host is not None:
            try:
                self._runtime_host.close()
            except Exception as error:  # noqa: BLE001 退出时仍需继续关闭 Qt 对象
                _ = error
        if self._event_bridge is not None:
            self._event_bridge.close()
        if self._global_hotkey is not None:
            self._global_hotkey.close()
        self.confirmation.hide()
        self.manual_input.hide()
        self.tray.close()
        self.settings.hide()
        self.pet.hide()
        self._application.quit()

    def _save_config(self, config: AppConfig) -> None:
        application_mode = classify_config_change(self._config, config)
        speech_changed = self._config.tts.enabled != config.tts.enabled
        manual_speech_changed = (
            self._config.tts.manual_input_enabled
            != config.tts.manual_input_enabled
        )
        startup_changed = (
            self._config.ui.start_at_login != config.ui.start_at_login
        )
        wake_changed = self._config.wake_word != config.wake_word
        try:
            self._config_store.save(config)
        except ConfigError as error:
            self.settings.validation_message.setText(f"保存失败：{error}")
            return
        self._config = config
        self.pet.set_always_on_top(config.ui.always_on_top)
        if startup_changed and self._startup_manager is not None:
            try:
                self._startup_manager.set_enabled(config.ui.start_at_login)
            except StartupError:
                self.settings.validation_message.setText(
                    "设置已保存，但开机启动设置失败，请检查系统权限"
                )
                return
        if speech_changed and self._runtime_host is not None:
            try:
                future = self._runtime_host.set_speech_enabled(
                    config.tts.enabled
                )
            except Exception as error:  # noqa: BLE001 配置已保存时仅报告安全错误
                _ = error
                self.settings.validation_message.setText(
                    "设置已保存，但语音播报切换失败，重启后生效"
                )
                return
            future.add_done_callback(self._speech_setting_finished)
        if manual_speech_changed and self._runtime_host is not None:
            try:
                future = self._runtime_host.set_manual_input_speech_enabled(
                    config.tts.manual_input_enabled
                )
            except Exception as error:  # noqa: BLE001 配置已保存时仅报告安全错误
                _ = error
                self.settings.validation_message.setText(
                    "设置已保存，但手动输入播报切换失败，重启后生效"
                )
                return
            future.add_done_callback(self._manual_speech_setting_finished)
        if wake_changed and self._runtime_host is not None:
            try:
                future = self._runtime_host.configure_wake_word(
                    config.wake_word
                )
            except Exception as error:  # noqa: BLE001 配置已保存时仅报告安全错误
                _ = error
                self.settings.validation_message.setText(
                    "设置已保存，语音唤醒应用失败，请重试"
                )
                return
            future.add_done_callback(self._wake_configuration_finished)
        messages = {
            ConfigApplicationMode.NO_CHANGE: "设置已保存",
            ConfigApplicationMode.IMMEDIATE_ONLY: "设置已保存，更改已立即生效",
            ConfigApplicationMode.RESTART_REQUIRED: (
                "设置已保存，即时选项已生效，其余更改将在重新启动 VoicePet 后生效"
            ),
        }
        self.settings.validation_message.setText(messages[application_mode])

    def _speech_setting_finished(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit(
                "设置已保存，但语音播报切换失败，重启后生效"
            )

    def _manual_speech_setting_finished(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit(
                "设置已保存，但手动输入播报切换失败，重启后生效"
            )

    def _restore_pet_position(self) -> None:
        if self._position_store is None:
            return
        position = self._position_store.load()
        if position is None:
            return
        self.pet.move_renderer_to(QPoint(position.x, position.y))

    def _save_pet_position(self, position: QPoint) -> None:
        if self._position_store is None:
            return
        try:
            self._position_store.save(
                WindowPosition(position.x(), position.y())
            )
        except (ValueError, WindowStateError):
            return

    def _load_tts_voice_catalog(self) -> None:
        if self._voice_catalog_loaded or self._voice_catalog_loading:
            return
        if self._runtime_host is None:
            self.settings.set_voice_catalog_status("中文声音目录当前不可用")
            return
        self._voice_catalog_loading = True
        self.settings.set_voice_catalog_status("正在获取全部在线中文声音…")
        try:
            future = self._runtime_host.list_tts_voices()
        except Exception as error:  # noqa: BLE001 Runtime 边界只显示安全状态
            _ = error
            self._voice_catalog_loading = False
            self.settings.set_voice_catalog_status(
                "中文声音获取失败，可继续使用当前声音"
            )
            return
        future.add_done_callback(self._tts_voice_catalog_finished)

    def _tts_voice_catalog_finished(self, future: Future[Any]) -> None:
        try:
            voices = tuple(future.result())
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.voice_catalog_failed.emit()
            return
        self.voice_catalog_ready.emit(voices)

    def _apply_tts_voice_catalog(self, voices: object) -> None:
        self._voice_catalog_loading = False
        self._voice_catalog_loaded = True
        if isinstance(voices, tuple):
            self.settings.set_voice_options(voices)

    def _apply_tts_voice_catalog_failed(self) -> None:
        self._voice_catalog_loading = False
        self.settings.set_voice_catalog_status(
            "中文声音获取失败，可继续使用当前声音"
        )

    def _preview_tts_voice(self, voice_name: str) -> None:
        if self._voice_preview_active:
            return
        if self._runtime_host is None:
            self.settings.set_voice_catalog_status("声音试听当前不可用")
            return
        self._voice_preview_active = True
        self.settings.set_voice_preview_active(True, "正在生成试听语音…")
        try:
            future = self._runtime_host.preview_tts_voice(voice_name)
        except Exception as error:  # noqa: BLE001 Runtime 边界只显示安全状态
            _ = error
            self._voice_preview_active = False
            self.settings.set_voice_preview_active(False, "试听启动失败")
            return
        future.add_done_callback(self._tts_voice_preview_finished)

    def _tts_voice_preview_finished(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.voice_preview_failed.emit()
            return
        self.voice_preview_ready.emit()

    def _apply_tts_voice_preview_ready(self) -> None:
        self._voice_preview_active = False
        self.settings.set_voice_preview_active(False, "试听完成")

    def _apply_tts_voice_preview_failed(self) -> None:
        self._voice_preview_active = False
        self.settings.set_voice_preview_active(False, "试听失败，请检查网络")

    def _wake_configuration_finished(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.wake_configuration_failed.emit()
            return
        self._request_wake_runtime_status()

    def _apply_wake_configuration_failed(self) -> None:
        self.settings.validation_message.setText(
            "设置已保存，语音唤醒应用失败，请重试"
        )

    def _activate_runtime(self, source: str = "click") -> None:
        if self._closed:
            return
        self.wake_requested.emit()
        if self._runtime_host is None:
            return
        if not self._runtime_host.llm_configured:
            self._speak_missing_llm_notice()
            return
        self._pending_activation_source = source
        if self._asr_preparation is not None:
            return
        self._request_asr_model_state()

    def _prepare_cached_asr(self) -> None:
        if (
            self._closed
            or self._runtime_host is None
            or self._asr_preparation is not None
        ):
            return
        self._request_asr_model_state()

    def _request_asr_model_state(self) -> None:
        if self._runtime_host is None:
            return
        self._asr_preparation = "checking"
        try:
            future = self._runtime_host.asr_model_state()
        except Exception as error:  # noqa: BLE001 用户意图边界只显示安全错误
            _ = error
            self._asr_preparation = None
            self.asr_preparation_failed.emit()
            return
        future.add_done_callback(self._asr_model_state_finished)

    def _speak_missing_llm_notice(self) -> None:
        if self._notice_pending or self._runtime_host is None:
            return
        message = "请先在设置中配置 API Key，保存后重启 VoicePet"
        self._show_runtime_error(message)
        self.pet.schedule_message_hide(8000)
        if not self._config.tts.enabled:
            return
        self._notice_pending = True
        try:
            future = self._runtime_host.speak_notice(message)
        except Exception as error:  # noqa: BLE001 气泡提示不依赖语音播放成功
            _ = error
            self._notice_pending = False
            return
        future.add_done_callback(self._notice_playback_finished)

    def _notice_playback_finished(self, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 语音失败时保留已经显示的气泡
            _ = error
        self.notice_finished.emit()

    def _apply_notice_finished(self) -> None:
        self._notice_pending = False
        if not self._closed and not self.pet.bubble.isHidden():
            self.pet.schedule_message_hide(8000)

    def _asr_model_state_finished(self, future: Future[AsrModelState]) -> None:
        try:
            state = future.result()
            if not isinstance(state, AsrModelState):
                raise TypeError("ASR 模型状态无效")
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.asr_preparation_failed.emit()
            return
        self.asr_state_ready.emit(state)

    def _apply_asr_model_state(self, state: AsrModelState) -> None:
        if self._closed:
            self._asr_preparation = None
            self._pending_activation_source = None
            return
        if state is AsrModelState.READY:
            self._asr_preparation = None
            self._resume_pending_activation()
            return
        if state is AsrModelState.CACHED:
            self._start_asr_load()
            return
        self._asr_preparation = None
        if self._pending_activation_source is None:
            return
        model_name = self._config.asr.model
        message = (
            "首次使用需要下载语音模型 small，约 464 MiB。是否现在下载？"
            if model_name == "small"
            else f"首次使用需要下载语音模型 {model_name}。是否现在下载？"
        )
        confirmed = _confirm_model_download(
            self.settings,
            title="准备语音模型",
            message=message,
        )
        if not confirmed:
            self._pending_activation_source = None
            self._show_runtime_error("已取消语音模型下载")
            return
        self._show_runtime_error("正在下载语音模型，请勿退出")
        self._asr_preparation = "downloading"
        try:
            assert self._runtime_host is not None
            future = self._runtime_host.download_asr_model()
        except Exception as error:  # noqa: BLE001 模型准备边界只显示安全错误
            _ = error
            self.asr_preparation_failed.emit()
            return
        future.add_done_callback(self._asr_download_finished)

    def _asr_download_finished(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.asr_preparation_failed.emit()
            return
        self.asr_download_ready.emit()

    def _apply_asr_download_ready(self) -> None:
        if self._closed or self._pending_activation_source is None:
            self._pending_activation_source = None
            return
        self._start_asr_load()

    def _start_asr_load(self) -> None:
        self._show_runtime_error("正在加载语音模型")
        self._asr_preparation = "loading"
        try:
            assert self._runtime_host is not None
            future = self._runtime_host.load_asr_model()
        except Exception as error:  # noqa: BLE001 模型准备边界只显示安全错误
            _ = error
            self.asr_preparation_failed.emit()
            return
        future.add_done_callback(self._asr_load_finished)

    def _asr_load_finished(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.asr_preparation_failed.emit()
            return
        self.asr_load_ready.emit()

    def _apply_asr_load_ready(self) -> None:
        self._asr_preparation = None
        if self._closed:
            self._pending_activation_source = None
            return
        if self._pending_activation_source is not None:
            self._show_runtime_error("语音模型准备完成")
        else:
            self._show_runtime_error("语音模型已就绪")
            self.pet.schedule_message_hide(2000)
        self._resume_pending_activation()

    def _apply_asr_preparation_failed(self) -> None:
        self._asr_preparation = None
        self._pending_activation_source = None
        if not self._closed:
            self._show_runtime_error("语音模型准备失败，请检查网络后重试")

    def _resume_pending_activation(self) -> None:
        source, self._pending_activation_source = (
            self._pending_activation_source,
            None,
        )
        if source is None or self._runtime_host is None or self._closed:
            return
        try:
            future = self._runtime_host.activate(source)
        except Exception as error:  # noqa: BLE001 用户意图边界只显示安全错误
            _ = error
            self.runtime_failed.emit("语音 Runtime 当前不可用")
            return
        future.add_done_callback(self._runtime_operation_finished)

    def _runtime_operation_finished(self, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.runtime_failed.emit("语音操作未能完成")

    def _handle_runtime_event(self, event: object) -> None:
        message: str | None = None
        if isinstance(event, StateChanged):
            self._conversation_phase = event.current
            self.pet.set_phase(event.current)
            self.manual_input.set_processing(
                event.current is not ConversationPhase.IDLE
            )
            self.manual_input.set_assistant_typing(
                event.current is ConversationPhase.THINKING
            )
            if event.current is ConversationPhase.IDLE:
                self.manual_input.finish_assistant_message()
                if self.settings.isVisible() or self.manual_input.isVisible():
                    self._refresh_sessions()
            if event.current in {
                ConversationPhase.IDLE,
                ConversationPhase.LISTENING,
                ConversationPhase.RECOVERING,
            }:
                self.confirmation.hide()
            messages = {
                ConversationPhase.LISTENING: "正在聆听…",
                ConversationPhase.TRANSCRIBING: "正在转写…",
                ConversationPhase.THINKING: "正在思考…",
                ConversationPhase.AWAITING_APPROVAL: "等待确认…",
                ConversationPhase.EXECUTING_TOOL: "正在执行操作…",
                ConversationPhase.SYNTHESIZING: "正在准备语音…",
                ConversationPhase.SPEAKING: "正在播报…",
                ConversationPhase.RECOVERING: "正在恢复…",
            }
            if event.current is ConversationPhase.LISTENING:
                self._response_text = ""
                self._runtime_error_visible = False
            if (
                event.current is ConversationPhase.RECOVERING
                and self._runtime_error_visible
            ):
                message = None
            elif (
                event.current is ConversationPhase.IDLE
                and event.previous
                in {
                    ConversationPhase.LISTENING,
                    ConversationPhase.TRANSCRIBING,
                }
                and not self._response_text.strip()
                and not self._runtime_error_visible
            ):
                message = "没有听清，请再试一次"
            else:
                message = messages.get(event.current)
            if (
                event.current
                in {
                    ConversationPhase.SYNTHESIZING,
                    ConversationPhase.SPEAKING,
                }
                and self._response_text.strip()
            ):
                message = None
        elif isinstance(event, TranscriptReady):
            message = f"你：{event.text}"
            self.manual_input.append_user_message(event.text)
        elif isinstance(event, TextInputSubmitted):
            self._response_text = ""
            self._runtime_error_visible = False
            message = f"你：{event.text}"
            self.manual_input.append_user_message(event.text)
        elif isinstance(event, WakeCommandPending):
            message = "我在听…"
        elif isinstance(event, TextDelta):
            self._response_text += event.text
            message = self._response_text
            self.manual_input.append_assistant_delta(event.text)
        elif isinstance(event, ApprovalRequested):
            message = f"需要确认 {event.risk}：{event.summary}"
            self.confirmation.show_request(event.summary, event.risk)
        elif isinstance(event, ToolResultReady):
            self.confirmation.hide()
            payload_message = event.payload.get("message")
            message = (
                payload_message
                if isinstance(payload_message, str)
                else f"工具执行状态：{event.status}"
            )
        elif isinstance(event, MemoryResultReady):
            self.confirmation.hide()
            if event.status != "pending":
                message = event.message
        elif isinstance(event, SpeakRequested):
            message = self._response_text or event.text
        elif isinstance(event, RuntimeErrorEvent):
            self.confirmation.hide()
            self._runtime_error_visible = True
            if event.component == "tts" and self._response_text.strip():
                message = f"{self._response_text}\n\n（{event.safe_message}）"
            else:
                message = event.safe_message
        if message is not None:
            self.pet.show_message(message)
        if (
            isinstance(event, StateChanged)
            and event.current is ConversationPhase.IDLE
            and (
                self._response_text.strip()
                or self._runtime_error_visible
            )
        ):
            self.pet.schedule_message_hide(8000)
            self._runtime_error_visible = False
        elif (
            isinstance(event, StateChanged)
            and event.current is ConversationPhase.IDLE
            and event.previous
            in {
                ConversationPhase.LISTENING,
                ConversationPhase.TRANSCRIBING,
            }
        ):
            self.pet.schedule_message_hide(2500)

    def _show_runtime_error(self, message: str) -> None:
        self.pet.show_message(message)

    def _approve_tool(self) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.approve(ConfirmationMode.UI)
        except Exception as error:  # noqa: BLE001 确认边界只显示安全错误
            _ = error
            self.runtime_failed.emit("工具确认未能提交")
            return
        future.add_done_callback(self._runtime_operation_finished)

    def _reject_tool(self) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.reject()
        except Exception as error:  # noqa: BLE001 拒绝边界只显示安全错误
            _ = error
            self.runtime_failed.emit("工具取消未能提交")
            return
        future.add_done_callback(self._runtime_operation_finished)

    def _submit_manual_text(self, text: str) -> None:
        if self._runtime_host is None:
            self.manual_input_failed.emit("手动输入当前不可用")
            return
        if not self._runtime_host.llm_configured:
            self.manual_input_failed.emit("请先在设置中配置 API Key")
            return
        if self._conversation_phase is not ConversationPhase.IDLE:
            self.manual_input_failed.emit("上一条消息仍在处理，请等待回复完成")
            return
        self.manual_input.set_submitting(True)
        try:
            future = self._runtime_host.submit_text(text)
        except Exception as error:  # noqa: BLE001 输入边界只显示安全错误
            _ = error
            self.manual_input_failed.emit("手动输入未能提交，请稍后再试")
            return
        future.add_done_callback(self._manual_text_submitted)

    def _manual_text_submitted(self, future: Future[Any]) -> None:
        try:
            future.result()
        except InvalidTransition:
            self.manual_input_failed.emit("当前正在处理其他请求，请稍后再试")
            return
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.manual_input_failed.emit("手动输入发送失败，请稍后再试")
            return
        self.manual_input_succeeded.emit()

    def _refresh_chat_session(self) -> None:
        if self._runtime_host is None:
            self.manual_input_failed.emit("会话记录当前不可用")
            return
        try:
            future = self._runtime_host.current_session_id()
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.manual_input_failed.emit("当前会话读取失败")
            return
        future.add_done_callback(self._current_chat_session_loaded)

    def _current_chat_session_loaded(self, future: Future[str]) -> None:
        try:
            session_id = future.result()
            if not isinstance(session_id, str):
                raise TypeError("当前会话 ID 无效")
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.manual_input_failed.emit("当前会话读取失败")
            return
        self._request_chat_turns(session_id)

    def _request_chat_turns(self, session_id: str) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.list_session_turns(session_id)
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.manual_input_failed.emit("会话记录读取失败")
            return
        future.add_done_callback(
            lambda result: self._chat_turns_loaded(session_id, result)
        )

    def _chat_turns_loaded(
        self,
        session_id: str,
        future: Future[Any],
    ) -> None:
        try:
            turns = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.manual_input_failed.emit("会话记录读取失败")
            return
        self.chat_session_ready.emit((session_id, turns))

    def _apply_chat_session(self, payload: object) -> None:
        session_id, turns = payload
        title = turns[0].user_text if turns else "新会话"
        self.manual_input.set_session(session_id, turns, title)

    def _load_session_detail(self, session_id: str) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.list_session_turns(session_id)
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.session_status_ready.emit("会话详情读取失败")
            return
        future.add_done_callback(self._session_detail_loaded)

    def _session_detail_loaded(self, future: Future[Any]) -> None:
        try:
            turns = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.session_status_ready.emit("会话详情读取失败")
            return
        self.session_detail_ready.emit(turns)

    def _activate_session(self, session_id: str) -> None:
        if not self._session_change_allowed():
            return
        assert self._runtime_host is not None
        try:
            future = self._runtime_host.activate_session(session_id)
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.session_status_ready.emit("会话切换未能提交")
            return
        future.add_done_callback(
            lambda result: self._session_activated(session_id, result)
        )

    def _session_activated(
        self,
        session_id: str,
        future: Future[Any],
    ) -> None:
        try:
            turns = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.session_status_ready.emit("会话切换失败")
            return
        self.session_activated_ready.emit((session_id, turns))

    def _apply_session_activated(self, payload: object) -> None:
        session_id, turns = payload
        title = turns[0].user_text if turns else "新会话"
        self.manual_input.set_session(session_id, turns, title)
        self.settings.set_session_detail(turns)
        self.session_status_ready.emit("已切换当前会话")
        self._refresh_sessions()

    def _new_session(self) -> None:
        if not self._session_change_allowed():
            return
        assert self._runtime_host is not None
        try:
            future = self._runtime_host.new_session()
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.session_status_ready.emit("新建会话未能提交")
            return
        future.add_done_callback(self._new_session_created)

    def _new_session_created(self, future: Future[str]) -> None:
        try:
            session_id = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.session_status_ready.emit("新建会话失败")
            return
        self.new_session_ready.emit(session_id)

    def _apply_new_session(self, session_id: str) -> None:
        self.manual_input.set_session(session_id, (), "新会话")
        self.settings.set_session_detail(())
        self.session_status_ready.emit("已新建会话")
        self._refresh_sessions()

    def _session_change_allowed(self) -> bool:
        if self._runtime_host is None:
            self.session_status_ready.emit("会话管理当前不可用")
            return False
        if self._conversation_phase is not ConversationPhase.IDLE:
            message = "当前请求处理完成后才能切换会话"
            self.session_status_ready.emit(message)
            self.manual_input.show_error(message)
            return False
        return True

    def _refresh_sessions(self) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.list_sessions()
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.session_status_ready.emit("会话列表当前不可用")
            return
        future.add_done_callback(self._sessions_loaded)

    def _sessions_loaded(self, future: Future[Any]) -> None:
        try:
            records = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.session_status_ready.emit("会话列表读取失败")
            return
        self.session_records_ready.emit(records)

    def _delete_session(self, session_id: str) -> None:
        if not self._session_change_allowed():
            return
        parent = self.manual_input if self.manual_input.isVisible() else self.settings
        if not _confirm_session_delete(parent):
            return
        try:
            assert self._runtime_host is not None
            future = self._runtime_host.delete_session(session_id)
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.session_status_ready.emit("会话删除未能提交")
            return
        future.add_done_callback(self._session_deleted)

    def _session_deleted(self, future: Future[Any]) -> None:
        try:
            deleted = bool(future.result())
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.session_status_ready.emit("会话删除失败")
            return
        self.session_status_ready.emit("会话已删除" if deleted else "会话不存在")
        self._refresh_sessions()
        self._refresh_chat_session()

    def _clear_sessions(self) -> None:
        if not self._session_change_allowed():
            return
        if not _confirm_session_clear(self.settings):
            return
        try:
            assert self._runtime_host is not None
            future = self._runtime_host.clear_sessions()
        except Exception as error:  # noqa: BLE001 会话边界只显示安全错误
            _ = error
            self.session_status_ready.emit("会话清空未能提交")
            return
        future.add_done_callback(self._sessions_cleared)

    def _sessions_cleared(self, future: Future[Any]) -> None:
        try:
            affected = int(future.result())
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.session_status_ready.emit("会话清空失败")
            return
        self.session_status_ready.emit(f"已清理 {affected} 条会话数据")
        self._refresh_sessions()
        self._refresh_chat_session()

    def _refresh_memories(self) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.list_memories()
        except Exception as error:  # noqa: BLE001 记忆边界只显示安全错误
            _ = error
            self.memory_status_ready.emit("记忆列表当前不可用")
            return
        future.add_done_callback(self._memories_loaded)

    def _memories_loaded(self, future: Future[Any]) -> None:
        try:
            records = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("记忆列表读取失败")
            return
        self.memory_records_ready.emit(records)

    def _delete_memory(self, memory_id: str) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.delete_memory(memory_id)
        except Exception as error:  # noqa: BLE001 记忆边界只显示安全错误
            _ = error
            self.memory_status_ready.emit("记忆删除未能提交")
            return
        future.add_done_callback(self._memory_deleted)

    def _confirm_memory(self, memory_id: str) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.confirm_memory(memory_id)
        except Exception as error:  # noqa: BLE001 记忆确认边界只显示安全错误
            _ = error
            self.memory_status_ready.emit("记忆候选确认未能提交")
            return
        future.add_done_callback(self._memory_confirmed)

    def _resolve_memory_conflict(self, memory_id: str) -> None:
        if self._runtime_host is None:
            return
        answer = QMessageBox.question(
            self.settings,
            "采用冲突记忆",
            "这会替换同类别的旧记忆，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        try:
            future = self._runtime_host.resolve_memory_conflict(memory_id)
        except Exception as error:  # noqa: BLE001 冲突解决边界只显示安全错误
            _ = error
            self.memory_status_ready.emit("冲突记忆采用未能提交")
            return
        future.add_done_callback(self._memory_conflict_resolved)

    def _memory_deleted(self, future: Future[Any]) -> None:
        try:
            deleted = bool(future.result())
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("记忆删除失败")
            return
        self.memory_status_ready.emit("记忆已删除" if deleted else "记忆不存在")
        self._refresh_memories()

    def _memory_confirmed(self, future: Future[Any]) -> None:
        try:
            record = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("记忆候选确认失败")
            return
        if record.status.value == "conflicted":
            self.memory_status_ready.emit("检测到同类记忆冲突，请选择要采用的内容")
        else:
            self.memory_status_ready.emit("记忆候选已确认")
        self._refresh_memories()

    def _memory_conflict_resolved(self, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("冲突记忆采用失败")
            return
        self.memory_status_ready.emit("已采用选中记忆并替换同类旧内容")
        self._refresh_memories()

    def _export_memories(self) -> None:
        if self._runtime_host is None:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self.settings,
            "导出本地记忆",
            "VoicePet-memory.json",
            "JSON 文件 (*.json)",
        )
        if not destination:
            return
        try:
            future = self._runtime_host.export_memories(destination)
        except Exception as error:  # noqa: BLE001 记忆边界只显示安全错误
            _ = error
            self.memory_status_ready.emit("记忆导出未能提交")
            return
        future.add_done_callback(self._memories_exported)

    def _memories_exported(self, future: Future[Any]) -> None:
        try:
            count = int(future.result())
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("记忆导出失败")
            return
        self.memory_status_ready.emit(f"已导出 {count} 条记忆")

    def _save_api_key(self, value: str) -> None:
        if self._credential_store is None:
            self.settings.validation_message.setText("加密凭据存储当前不可用")
            return
        try:
            self._credential_store.set("openai_api_key", value)
        except CredentialError as error:
            _ = error
            self.settings.validation_message.setText("API Key 加密保存失败")
            return
        self.settings.validation_message.setText(
            "API Key 已加密保存，重启 VoicePet 后生效"
        )

    def _delete_api_key(self) -> None:
        if self._credential_store is None:
            self.settings.validation_message.setText("加密凭据存储当前不可用")
            return
        try:
            deleted = self._credential_store.delete("openai_api_key")
        except CredentialError as error:
            _ = error
            self.settings.validation_message.setText("API Key 移除失败")
            return
        message = "API Key 已移除，重启 VoicePet 后生效" if deleted else "未保存 API Key"
        self.settings.validation_message.setText(message)

    def _import_pet_file(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self.settings,
            "导入桌宠形象包",
            "",
            "Codex Pet (*.codex-pet *.zip);;所有文件 (*)",
        )
        if source:
            self._submit_pet_import(source)

    def _import_pet_directory(self) -> None:
        source = QFileDialog.getExistingDirectory(
            self.settings,
            "导入桌宠形象目录",
        )
        if source:
            self._submit_pet_import(source)

    def _submit_pet_import(self, source: str) -> None:
        if self._runtime_host is None:
            self.settings.validation_message.setText("桌宠导入当前不可用")
            return
        try:
            future = self._runtime_host.install_pet(source)
        except Exception as error:  # noqa: BLE001 桌宠导入边界只显示安全错误
            _ = error
            self.settings.validation_message.setText("桌宠导入未能提交")
            return
        future.add_done_callback(self._pet_import_finished)

    def _pet_import_finished(self, future: Future[Any]) -> None:
        try:
            installed = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("桌宠形象校验或安装失败")
            return
        self.pet_installed.emit(installed)

    def _apply_installed_pet(self, directory: object) -> None:
        path = Path(directory)
        if self._catalog_loader is None:
            if not self.pet.load_pet_directory(path):
                self.settings.validation_message.setText(
                    "桌宠形象加载失败，已保留当前形象"
                )
                return
            self.settings.validation_message.setText(
                "桌宠形象已导入并预览，请保存设置以继续使用"
            )
            return
        self._refresh_pet_choices()
        installed_path = path.expanduser().resolve()
        selected = next(
            (
                choice.pet_id
                for choice in self._pet_choices.values()
                if choice.directory == installed_path
            ),
            None,
        )
        if selected is None:
            self.settings.validation_message.setText("桌宠形象加载失败，已保留当前形象")
            return
        self.settings.select_pet_id(selected)
        self._preview_pet_selection(selected)
        if self._last_preview_pet_id != selected:
            return
        self.settings.validation_message.setText(
            "桌宠形象已导入并预览，请保存设置以继续使用"
        )

    def _refresh_pet_choices(self, selected_id: str | None = None) -> None:
        if self._catalog_loader is None:
            return
        try:
            choices = self._catalog_loader()
        except Exception as error:  # noqa: BLE001 形象扫描失败时保留当前界面
            _ = error
            self.settings.validation_message.setText("桌宠形象列表刷新失败")
            return
        self._pet_choices = {choice.pet_id: choice for choice in choices}
        preferred = selected_id or self._last_preview_pet_id
        if preferred not in self._pet_choices:
            preferred = choices[0].pet_id if choices else ""
        self.settings.set_pet_choices(choices, preferred)
        if preferred:
            self._last_preview_pet_id = preferred

    def _preview_pet_selection(self, pet_id: str) -> None:
        choice = self._pet_choices.get(pet_id)
        if choice is None:
            self.settings.select_pet_id(self._last_preview_pet_id)
            return
        if not self.pet.load_pet_directory(choice.directory):
            self.settings.select_pet_id(self._last_preview_pet_id)
            self.settings.validation_message.setText(
                "桌宠形象加载失败，已保留当前形象"
            )
            return
        self._last_preview_pet_id = pet_id
        self.settings.validation_message.setText("桌宠形象已预览，保存后继续使用")

    def _check_wake_model(self) -> None:
        if self._runtime_host is None or self._closed:
            return
        try:
            future = self._runtime_host.wake_model_state()
        except Exception as error:  # noqa: BLE001 模型状态边界只显示安全错误
            _ = error
            self.wake_download_failed.emit()
            return
        future.add_done_callback(self._wake_model_state_finished)

    def _wake_model_state_finished(self, future: Future[WakeModelState]) -> None:
        try:
            state = future.result()
            if not isinstance(state, WakeModelState):
                raise TypeError("中文唤醒模型状态无效")
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.wake_download_failed.emit()
            return
        self.wake_model_state_ready.emit(state)

    def _apply_wake_model_state(self, state: WakeModelState) -> None:
        if self._closed:
            return
        if not self._config.wake_word.enabled:
            self.settings.set_wake_model_status("disabled", "语音唤醒已关闭")
            return
        if state in {WakeModelState.CACHED, WakeModelState.READY}:
            self._request_wake_runtime_status()
            return
        self.settings.set_wake_model_status("missing", "中文唤醒模型尚未下载")
        if self._wake_prompted:
            return
        self._wake_prompted = True
        confirmed = _confirm_model_download(
            self.settings,
            title="准备中文唤醒模型",
            message="语音唤醒需要下载中文关键词模型，约 31.1 MiB。是否现在下载？",
        )
        if confirmed:
            self._start_wake_model_download()

    def _request_wake_runtime_status(self) -> None:
        if self._runtime_host is None or self._closed:
            return
        try:
            future = self._runtime_host.wake_runtime_status()
        except Exception as error:  # noqa: BLE001 状态边界只显示安全错误
            _ = error
            self.wake_runtime_status_ready.emit(WakeRuntimeStatus.FAILED)
            return
        future.add_done_callback(self._wake_runtime_status_finished)

    def _wake_runtime_status_finished(
        self,
        future: Future[WakeRuntimeStatus],
    ) -> None:
        try:
            status = future.result()
            if not isinstance(status, WakeRuntimeStatus):
                raise TypeError("中文唤醒运行状态无效")
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            status = WakeRuntimeStatus.FAILED
        self.wake_runtime_status_ready.emit(status)

    def _apply_wake_runtime_status(self, status: WakeRuntimeStatus) -> None:
        if self._closed:
            return
        states = {
            WakeRuntimeStatus.DISABLED: ("disabled", "语音唤醒已关闭"),
            WakeRuntimeStatus.MISSING: ("missing", "中文唤醒模型尚未下载"),
            WakeRuntimeStatus.DOWNLOADING: (
                "downloading",
                "正在下载中文唤醒模型",
            ),
            WakeRuntimeStatus.LOADING: ("loading", "正在加载中文唤醒模型"),
            WakeRuntimeStatus.LISTENING: (
                "listening",
                f"正在监听“{self._config.wake_word.keyword}”",
            ),
            WakeRuntimeStatus.FAILED: (
                "failed",
                "中文唤醒启动失败，请检查唤醒词后重试",
            ),
        }
        ui_status, message = states[status]
        self.settings.set_wake_model_status(ui_status, message)

    def _start_wake_model_download(self) -> None:
        if (
            self._runtime_host is None
            or self._closed
            or self._wake_download_active
        ):
            return
        self._wake_download_active = True
        self.settings.set_wake_model_status(
            "downloading",
            "正在下载中文唤醒模型",
        )
        self.settings.set_wake_download_progress(0, 32_654_866)
        try:
            future = self._runtime_host.download_wake_model(
                self._wake_download_progressed
            )
        except Exception as error:  # noqa: BLE001 下载边界只显示安全错误
            _ = error
            self._wake_download_active = False
            self.wake_download_failed.emit()
            return
        future.add_done_callback(self._wake_model_download_finished)

    def _wake_download_progressed(
        self,
        progress: WakeDownloadProgress,
    ) -> None:
        self.wake_download_progress.emit(progress)

    def _apply_wake_download_progress(
        self,
        progress: WakeDownloadProgress,
    ) -> None:
        if self._closed or not self._wake_download_active:
            return
        self.settings.set_wake_download_progress(
            progress.downloaded_bytes,
            progress.total_bytes,
        )

    def _wake_model_download_finished(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            if self._wake_download_active:
                self.wake_download_failed.emit()
            return
        if self._wake_download_active:
            self.wake_download_ready.emit()

    def _apply_wake_download_ready(self) -> None:
        self._wake_download_active = False
        if self._closed or self._runtime_host is None:
            return
        self.settings.set_wake_model_status("loading", "正在加载中文唤醒模型")
        try:
            future = self._runtime_host.configure_wake_word(
                self._config.wake_word
            )
        except Exception as error:  # noqa: BLE001 配置边界只显示安全错误
            _ = error
            self.wake_configuration_failed.emit()
            return
        future.add_done_callback(self._wake_configuration_finished)

    def _apply_wake_download_failed(self) -> None:
        self._wake_download_active = False
        if not self._closed:
            self.settings.set_wake_model_status(
                "failed",
                "中文唤醒模型准备失败，请检查网络后重试",
            )

    def _cancel_wake_model_download(self) -> None:
        if self._runtime_host is None or not self._wake_download_active:
            return
        try:
            self._runtime_host.cancel_wake_model_download()
        except Exception as error:  # noqa: BLE001 取消边界只显示安全错误
            _ = error
            self.settings.set_wake_model_status(
                "failed",
                "中文唤醒模型取消失败，请重试",
            )
            return
        self._wake_download_active = False
        self.settings.set_wake_model_status(
            "missing",
            "已取消下载，可稍后继续",
        )

    def _run_diagnostics(self) -> None:
        if self._runtime_host is None:
            return
        try:
            future = self._runtime_host.run_diagnostics()
        except Exception as error:  # noqa: BLE001 诊断边界只显示安全错误
            _ = error
            self.memory_status_ready.emit("诊断当前不可用")
            return
        future.add_done_callback(self._diagnostics_finished)

    def _diagnostics_finished(self, future: Future[Any]) -> None:
        try:
            report = future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("诊断运行失败")
            return
        self.diagnostic_report_ready.emit(report)

    def _export_diagnostics(self) -> None:
        if self._runtime_host is None:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self.settings,
            "导出脱敏诊断包",
            "VoicePet-diagnostics.zip",
            "ZIP 文件 (*.zip)",
        )
        if not destination:
            return
        try:
            future = self._runtime_host.export_diagnostics(destination)
        except Exception as error:  # noqa: BLE001 诊断边界只显示安全错误
            _ = error
            self.memory_status_ready.emit("诊断包导出未能提交")
            return
        future.add_done_callback(self._diagnostics_exported)

    def _diagnostics_exported(self, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 后台异常不能跨线程操作 QWidget
            _ = error
            self.memory_status_ready.emit("诊断包导出失败")
            return
        self.memory_status_ready.emit("脱敏诊断包已保存到本地")

def run_ui(
    *,
    smoke_test: bool = False,
    runtime_builder: Callable[..., RuntimeServices] = build_default_runtime,
    runtime_host_factory: Callable[[RuntimeServices], RuntimeHostProtocol] = RuntimeHost,
    credential_store_factory: Callable[[object], Any] = DpapiCredentialStore,
    hotkey_factory: Callable[[], Any | None] = create_global_hotkey,
    startup_manager_factory: Callable[[], Any] = WindowsStartupManager,
    position_store_factory: Callable[[object], Any] = WindowPositionStore,
    event_loop: Callable[[], int] | None = None,
) -> int:
    """加载配置并运行 Qt 主进程或无窗口 smoke test"""

    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("VoicePet")
    application.setStyle("Fusion")
    application.setQuitOnLastWindowClosed(False)
    config_path = default_config_path()
    store = ConfigStore(config_path)
    result = store.load()
    if smoke_test:
        controller = ApplicationController(application, store, result.config)
        controller.close()
        return 0
    try:
        credential_store = credential_store_factory(
            config_path.parent / "data" / "credentials.bin"
        )
        api_key = credential_store.get("openai_api_key")
    except CredentialError:
        api_key = None
    event_bus = EventBus()
    active_pet_directory = resolve_active_pet_directory(
        result.config.ui.active_skin,
        config_path.parent,
    )
    services = runtime_builder(
        result.config,
        event_bus,
        config_path.parent.resolve(),
        api_key=api_key,
        pet_directory=active_pet_directory,
    )
    runtime_host = runtime_host_factory(services)
    global_hotkey = hotkey_factory()
    try:
        startup_manager = startup_manager_factory()
    except StartupError:
        startup_manager = None
    position_store = position_store_factory(
        config_path.parent / "window-state.json"
    )
    bridge = QtEventBridge(event_bus, _RUNTIME_EVENT_TYPES)
    controller = ApplicationController(
        application,
        store,
        result.config,
        runtime_host=runtime_host,
        event_bridge=bridge,
        credential_store=credential_store,
        pet_directory=active_pet_directory,
        catalog_loader=lambda: discover_pet_choices(config_path.parent),
        global_hotkey=global_hotkey,
        startup_manager=startup_manager,
        position_store=position_store,
    )
    controller.start()
    try:
        return (event_loop or application.exec)()
    finally:
        controller.close()
