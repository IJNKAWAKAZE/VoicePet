"""QML 主进程装配、托盘与配置保存生命周期。"""

from __future__ import annotations

import sys
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from typing import Any

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.config import ConfigError, ConfigStore, default_config_path
from core.credentials import CredentialError, DpapiCredentialStore
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    MemoryChanged,
    MemoryMaintenanceChanged,
    MemoryResultReady,
    RuntimeErrorEvent,
    RecordingStarted,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TextInputSubmitted,
    ToolResultReady,
    TranscriptReady,
    WakeCommandPending,
)
from core.runtime import RuntimeHost, RuntimeServices
from core.runtime_factory import build_default_runtime
from core.startup import StartupError, WindowsStartupManager
from core.window_state import WindowPositionStore

from .event_bridge import QtEventBridge
from .global_hotkey import create_global_hotkey
from .pet_animation_model import PetAnimationModel, PetInteractionController
from .pet_catalog import discover_pet_choices, resolve_active_pet_directory
from .qml_application import QmlApplicationController, QmlRuntimeHostProtocol
from .qml_resources import qml_root, ui_assets_root
from .qml_runtime import QmlRuntime
from .tray import TrayController
from .viewmodels import (
    AppShellViewModel,
    ChatViewModel,
    DiagnosticsViewModel,
    DialogCoordinator,
    MemoryViewModel,
    PetViewModel,
    SettingsViewModel,
    ThemeViewModel,
)
from .window_coordinator import WindowCoordinator

_RUNTIME_EVENT_TYPES = (
    StateChanged,
    RecordingStarted,
    TranscriptReady,
    TextInputSubmitted,
    TextDelta,
    ApprovalRequested,
    ToolResultReady,
    SpeakRequested,
    RuntimeErrorEvent,
    MemoryResultReady,
    MemoryChanged,
    MemoryMaintenanceChanged,
    WakeCommandPending,
)


def run_ui(
    *,
    smoke_test: bool = False,
    runtime_builder: Callable[..., RuntimeServices] = build_default_runtime,
    runtime_host_factory: Callable[[RuntimeServices], QmlRuntimeHostProtocol] = RuntimeHost,
    credential_store_factory: Callable[[object], Any] = DpapiCredentialStore,
    hotkey_factory: Callable[[], Any | None] = create_global_hotkey,
    startup_manager_factory: Callable[[], Any] = WindowsStartupManager,
    position_store_factory: Callable[[object], Any] = WindowPositionStore,
    event_loop: Callable[[], int] | None = None,
) -> int:
    """加载配置并运行 QML 主进程或无窗口 smoke test"""

    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("VoicePet")
    application.setWindowIcon(
        QIcon(str(ui_assets_root() / "brand" / "voicepet.ico"))
    )
    application.setStyle("Fusion")
    application.setQuitOnLastWindowClosed(False)
    config_path = default_config_path()
    store = ConfigStore(config_path)
    result = store.load()
    config = result.config

    class UnavailableRuntime:
        """仅为 QML smoke 装配提供不会执行的接口"""

        def __getattr__(self, name: str) -> Callable[..., Future[Any]]:
            del name

            def unavailable(*args: object, **kwargs: object) -> Future[Any]:
                del args, kwargs
                future: Future[Any] = Future()
                future.set_exception(RuntimeError("Runtime 未启动"))
                return future

            return unavailable

    runtime_for_models: Any = UnavailableRuntime()
    settings_viewmodel = SettingsViewModel(config, store, data_directory=config_path.parent)

    def persist_theme(ui_config: object) -> None:
        if not isinstance(ui_config, type(config.ui)):
            raise ConfigError("主题设置类型无效")
        candidate = replace(settings_viewmodel.config, ui=ui_config)
        store.save(candidate)
        settings_viewmodel.begin_edit(candidate)

    theme_viewmodel = ThemeViewModel(config.ui, persist_theme)
    app_shell = AppShellViewModel()
    dialogs = DialogCoordinator()

    def build_qml_runtime(runtime: Any) -> tuple[
        QmlRuntime,
        ChatViewModel,
        MemoryViewModel,
        PetViewModel,
        PetInteractionController,
        PetAnimationModel,
    ]:
        chat = ChatViewModel(runtime, dialogs=dialogs)
        memories = MemoryViewModel(runtime, dialogs)
        pets = PetViewModel(
            runtime,
            settings_viewmodel,
            lambda: discover_pet_choices(config_path.parent),
        )
        diagnostics = DiagnosticsViewModel(runtime)
        pet_animation = PetAnimationModel(
            resolve_active_pet_directory(
                settings_viewmodel.config.ui.active_skin,
                config_path.parent,
            )
        )
        pet_interaction = PetInteractionController()
        qml_runtime = QmlRuntime(
            application,
            {
                "appShell": app_shell,
                "themeViewModel": theme_viewmodel,
                "dialogCoordinator": dialogs,
                "chatViewModel": chat,
                "memoryViewModel": memories,
                "settingsViewModel": settings_viewmodel,
                "petViewModel": pets,
                "diagnosticsViewModel": diagnostics,
                "petAnimation": pet_animation,
                "petInteraction": pet_interaction,
            },
            qml_root() / "Main.qml",
        )
        return qml_runtime, chat, memories, pets, pet_interaction, pet_animation

    if smoke_test:
        qml_runtime, _, _, _, _, _ = build_qml_runtime(runtime_for_models)
        qml_runtime.load()
        qml_runtime.close()
        return 0

    credential_store = None
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
        config,
        event_bus,
        config_path.parent.resolve(),
        api_key=api_key,
        pet_directory=active_pet_directory,
    )
    runtime_host = runtime_host_factory(services)
    runtime_for_models = runtime_host
    global_hotkey = hotkey_factory()
    if global_hotkey is not None:
        replace_shortcut = getattr(global_hotkey, "try_replace", None)
        if callable(replace_shortcut):
            replace_shortcut(config.ui.global_hotkey)
        set_hotkey_enabled = getattr(global_hotkey, "set_enabled", None)
        if callable(set_hotkey_enabled):
            set_hotkey_enabled(config.ui.global_hotkey_enabled)
    try:
        startup_manager = startup_manager_factory()
    except StartupError:
        startup_manager = None
    position_store = position_store_factory(
        config_path.parent / "window-state.json"
    )
    settings_viewmodel.bind_services(
        credential_store=credential_store,
        runtime=runtime_host,
    )
    bridge = QtEventBridge(event_bus, _RUNTIME_EVENT_TYPES)
    qml_runtime, chat, memories, pets, pet_interaction, pet_animation = build_qml_runtime(
        runtime_for_models
    )
    tray = TrayController()
    window_coordinator = WindowCoordinator(
        can_recover_click_through=lambda: tray.is_visible
    )
    controller = QmlApplicationController(
        application,
        runtime_host,
        qml_runtime,
        app_shell,
        chat,
        dialogs,
        pet_animation,
        pet_interaction,
        tray,
        memories=memories,
        event_bridge=bridge,
        global_hotkey=global_hotkey,
        settings=settings_viewmodel,
        pets=pets,
        startup_manager=startup_manager,
        window_coordinator=window_coordinator,
        position_store=position_store,
    )
    if startup_manager is not None:
        try:
            startup_manager.set_enabled(config.ui.start_at_login)
        except StartupError:
            pass
    try:
        controller.start()
        return (event_loop or application.exec)()
    finally:
        controller.close()
        if credential_store is not None:
            close_credentials = getattr(credential_store, "close", None)
            if callable(close_credentials):
                close_credentials()
