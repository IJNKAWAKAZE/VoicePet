from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, QRect, QSize
from PySide6.QtTest import QSignalSpy

from core.config import AppConfig, UiConfig
from ui.pet_animation_model import PetAnimationModel, PetInteractionController
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


def test_app_shell_navigates_only_to_known_sections():
    shell = AppShellViewModel()
    changed = QSignalSpy(shell.currentSectionChanged)
    errors = QSignalSpy(shell.errorOccurred)

    shell.navigate("memories")
    shell.navigate("invalid")

    assert shell.current_section == "memories"
    assert changed.count() == 1
    assert errors.count() == 1


def test_show_main_defaults_to_chat_and_emits_request():
    shell = AppShellViewModel()
    shell.navigate("settings")
    shown = QSignalSpy(shell.showMainRequested)

    shell.show_main()

    assert shell.current_section == "chat"
    assert shown.count() == 1
    assert shown.at(0)[0] == "chat"


def test_window_rect_is_clamped_to_negative_coordinate_screen():
    coordinator = WindowCoordinator()
    screens = [QRect(-1920, 0, 1920, 1080), QRect(0, 0, 2560, 1440)]

    result = coordinator.clamp_rect(QRect(-2500, 900, 600, 400), screens)

    assert result.width() == 820
    assert result.height() == 560
    assert screens[0].contains(result)


def test_window_rect_falls_back_to_first_screen_when_fully_offscreen():
    coordinator = WindowCoordinator()
    screen = QRect(0, 0, 1280, 720)

    result = coordinator.clamp_rect(QRect(5000, 5000, 1040, 680), [screen])

    assert screen.contains(result)


def test_pet_position_is_clamped_inside_negative_coordinate_screen():
    coordinator = WindowCoordinator()
    screens = [QRect(-1920, 0, 1920, 1080), QRect(0, 0, 2560, 1440)]

    result = coordinator.clamp_pet_position(
        QPoint(-2100, 1000),
        QSize(192, 208),
        screens,
    )

    assert result == QPoint(-1920, 872)


@pytest.mark.parametrize(
    ("width", "overlay", "compact"),
    [(1040, False, False), (900, True, False), (840, True, True)],
)
def test_main_window_responsive_structure(qapp, width, overlay, compact):
    shell = AppShellViewModel()
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    dialogs = DialogCoordinator()
    class Runtime:
        def __getattr__(self, name):
            return lambda *args: _completed(())

    class Store:
        def save(self, config):
            pass

    service = Runtime()
    config_data = AppConfig().to_dict()
    config_data["llm"]["system_prompt"] = "请用中文回答"
    config = AppConfig.from_dict(config_data)
    settings = SettingsViewModel(config, Store())
    chat = ChatViewModel(service)
    memories = MemoryViewModel(service, dialogs)
    pets = PetViewModel(service, settings, lambda: ())
    diagnostics = DiagnosticsViewModel(service)
    animation = PetAnimationModel(Path("assets/pet/kawakaze").resolve())
    interaction = PetInteractionController()
    runtime = QmlRuntime(
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
    runtime.load()
    root = runtime.root_objects[0]
    root.setProperty("width", width)
    qapp.processEvents()

    navigation = root.findChild(QObject, "navigationRail")
    sidebar = root.findChild(QObject, "contextSidebar")
    content = root.findChild(QObject, "mainContent")
    background = root.findChild(QObject, "themeBackground")
    welcome = root.findChild(QObject, "welcomeIllustration")
    pet_preview = root.findChild(QObject, "settingsPetPreview")
    pet_file_button = root.findChild(QObject, "importPetFileButton")
    pet_directory_button = root.findChild(QObject, "importPetDirectoryButton")
    sidebar_button = root.findChild(QObject, "contextSidebarButton")

    assert root.objectName() == "mainWindow"
    assert navigation is not None
    assert sidebar is not None
    assert content is not None
    assert background is not None
    assert welcome is not None
    assert pet_preview is not None
    assert pet_file_button is not None
    assert pet_directory_button is not None
    assert sidebar_button is not None
    assert sidebar_button.property("visible") is overlay
    if overlay:
        sidebar_button.clicked.emit()
        qapp.processEvents()
        assert sidebar.property("drawerOpen") is True
    assert 0.06 <= background.property("opacity") <= 0.12
    assert background.property("enabled") is False
    assert background.property("source") == theme.backgroundImage
    assert welcome.property("source") == theme.welcomeImage
    assert sidebar.property("overlayMode") is overlay
    assert navigation.property("compact") is compact
    assert root.property("minimumWidth") == 820
    assert root.property("minimumHeight") == 560
    runtime.close()


def test_chat_context_sidebar_routes_session_actions(qapp):
    shell = AppShellViewModel()
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    dialogs = DialogCoordinator()

    class Runtime:
        def __init__(self):
            self.activated = []
            self.created = 0

        def list_sessions(self):
            return _completed(())

        def activate_session(self, session_id):
            self.activated.append(session_id)
            return _completed(())

        def new_session(self):
            self.created += 1
            return _completed("new-session")

        def __getattr__(self, name):
            return lambda *args: _completed(())

    class Store:
        def save(self, config):
            pass

    service = Runtime()
    settings = SettingsViewModel(AppConfig(), Store())
    chat = ChatViewModel(service)
    runtime = QmlRuntime(
        qapp,
        {
            "appShell": shell,
            "themeViewModel": theme,
            "dialogCoordinator": dialogs,
            "chatViewModel": chat,
            "memoryViewModel": MemoryViewModel(service, dialogs),
            "settingsViewModel": settings,
            "petViewModel": PetViewModel(service, settings, lambda: ()),
            "diagnosticsViewModel": DiagnosticsViewModel(service),
            "petAnimation": PetAnimationModel(Path("assets/pet/kawakaze").resolve()),
            "petInteraction": PetInteractionController(),
        },
        qml_root() / "Main.qml",
    )
    runtime.load()
    root = runtime.root_objects[0]
    session_list = root.findChild(QObject, "sessionList")
    new_button = root.findChild(QObject, "newSessionButton")

    assert session_list is not None
    assert new_button is not None
    session_list.sessionRequested.emit("s1")
    new_button.clicked.emit()
    qapp.processEvents()

    assert service.activated == ["s1"]
    assert service.created == 1
    runtime.close()


def _completed(value):
    future = Future()
    future.set_result(value)
    return future


def test_title_bar_uses_drawn_window_icons_instead_of_unicode_glyphs():
    source = Path("ui/qml/components/AppTitleBar.qml").read_text(encoding="utf-8")

    assert "Canvas" in source
    assert "WindowGlyph" in source
    for glyph in ("—", "□", "▢", "×"):
        assert glyph not in source


def test_pet_settings_routes_import_and_delete_actions():
    source = Path("ui/qml/screens/settings/PetSettings.qml").read_text(
        encoding="utf-8"
    )

    assert "root.pets.delete_pet(petId)" in source


def test_settings_pages_route_credentials_runtime_and_hotkey_actions():
    ai_source = Path("ui/qml/screens/settings/AiSettings.qml").read_text(
        encoding="utf-8"
    )
    voice_source = Path("ui/qml/screens/settings/VoiceSettings.qml").read_text(
        encoding="utf-8"
    )
    general_source = Path("ui/qml/screens/settings/GeneralSettings.qml").read_text(
        encoding="utf-8"
    )

    assert "root.settings.save_api_key(apiKeyField.text)" in ai_source
    assert "root.settings.delete_api_key()" in ai_source
    assert "root.diagnostics.test_connection()" in ai_source
    assert "root.settings.preview_voice" in voice_source
    assert "root.settings.download_asr_model()" in voice_source
    assert "root.settings.download_wake_model()" in voice_source
    assert '"global_hotkey_enabled"' in general_source


def test_main_window_is_frameless_resizable_and_pet_bubble_uses_screen_geometry():
    main_source = Path("ui/qml/Main.qml").read_text(encoding="utf-8")
    pet_source = Path("ui/qml/windows/PetWindow.qml").read_text(encoding="utf-8")
    bubble_source = Path("ui/qml/components/PetSpeechBubble.qml").read_text(
        encoding="utf-8"
    )

    assert "Qt.FramelessWindowHint" in main_source
    assert "Qt.WindowMinimizeButtonHint" in main_source
    assert "Qt.WindowMaximizeButtonHint" in main_source
    assert "startSystemResize" in main_source
    assert "Screen.desktopAvailableWidth" not in pet_source
    assert "availableArea: petWindow.availableArea" in pet_source
    assert "clip: true" in bubble_source
