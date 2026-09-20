from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

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
from ui.viewmodels.theme import THEME_PALETTES, ThemeViewModel


class _Runtime:
    def __getattr__(self, _name):
        return lambda *_args: _completed(())


class _Store:
    def save(self, _config):
        pass


def _completed(value):
    future = Future()
    future.set_result(value)
    return future


def _button_with_text(container, text):
    items = [container]
    for item in items:
        items.extend(item.childItems())
    return next(
        item for item in items
        if item.property("text") == text and item.property("kind") is not None
    )


def _pointer_position(item):
    point = item.mapToScene(QPoint(item.width() / 2, item.height() / 2))
    return QPoint(round(point.x()), round(point.y()))


def _contrast_ratio(first, second):
    def luminance(color):
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92 if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first_luminance = luminance(first)
    second_luminance = luminance(second)
    return (max(first_luminance, second_luminance) + 0.05) / (
        min(first_luminance, second_luminance) + 0.05
    )


@pytest.mark.parametrize(
    ("theme_id", "selected_text"),
    [
        ("sunny_sea", "#205b7c"),
        ("deep_night", "#69c7f2"),
        ("sakura_coral", "#9f3652"),
    ],
)
def test_selected_text_meets_contrast_requirement_for_all_button_states(
    theme_id, selected_text
):
    palette = THEME_PALETTES[theme_id]
    text = getattr(palette, "selected_text", palette.interaction_primary)

    assert text.lower() == selected_text
    assert _contrast_ratio(text, palette.user_bubble) >= 4.5
    assert _contrast_ratio(text, palette.surface_alt) >= 4.5
    assert _contrast_ratio(palette.inverse_text, palette.primary_pressed) >= 4.5


@pytest.fixture
def main_window(qapp):
    shell = AppShellViewModel()
    theme = ThemeViewModel(UiConfig(), lambda _config: None)
    dialogs = DialogCoordinator()
    service = _Runtime()
    settings = SettingsViewModel(AppConfig(), _Store())
    runtime = QmlRuntime(
        qapp,
        {
            "appShell": shell,
            "themeViewModel": theme,
            "dialogCoordinator": dialogs,
            "chatViewModel": ChatViewModel(service),
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
    yield runtime.root_objects[0], shell, theme
    runtime.root_objects[0].hide()
    runtime.close()


@pytest.mark.parametrize(
    ("theme_id", "shell_color"),
    [
        ("sunny_sea", "#eaf5fc"),
        ("deep_night", "#172238"),
        ("sakura_coral", "#fff0f3"),
    ],
)
def test_shell_and_selected_navigation_follow_live_theme(
    qapp, main_window, theme_id, shell_color
):
    root, shell, theme = main_window
    frame = root.findChild(QObject, "windowFrame")
    rail = root.findChild(QObject, "navigationRail")
    theme.set_theme(theme_id)
    qapp.processEvents()
    chat_button = _button_with_text(rail, "聊天")
    settings_button = _button_with_text(rail, "设置")

    assert frame.property("color").name() == shell_color
    assert rail.property("color").name() == shell_color
    assert chat_button.property("kind") == "selected"
    assert settings_button.property("kind") == "ghost"

    shell.navigate("settings")
    qapp.processEvents()
    selected_category = _button_with_text(
        root.findChild(QQuickItem, "settingsPage"), "常规"
    )

    assert settings_button.property("kind") == "selected"
    assert selected_category.property("kind") == "selected"
    assert selected_category.property("backgroundColor").name() == theme.userBubble.name()
    assert selected_category.property("foregroundColor").name() == theme.selectedText.name()


@pytest.mark.parametrize("theme_id", ["sunny_sea", "deep_night", "sakura_coral"])
def test_selected_button_has_hover_pressed_and_focus_feedback(qapp, main_window, theme_id):
    root, _shell, theme = main_window
    root.show()
    root.requestActivate()
    theme.set_theme(theme_id)
    qapp.processEvents()
    rail = root.findChild(QQuickItem, "navigationRail")
    button = _button_with_text(rail, "聊天")
    point = _pointer_position(button)

    QTest.mouseMove(root, QPoint(0, 0))
    QTest.mouseMove(root, point)
    qapp.processEvents()
    assert button.property("backgroundColor").name() == theme.surfaceAlt.name()
    assert button.property("foregroundColor").name() == theme.selectedText.name()

    QTest.mousePress(root, Qt.LeftButton, Qt.NoModifier, point)
    qapp.processEvents()
    assert button.property("backgroundColor").name() == theme.primaryPressed.name()
    assert button.property("foregroundColor").name() == theme.inverseText.name()

    QTest.mouseRelease(root, Qt.LeftButton, Qt.NoModifier, point)
    button.setFocus(True, Qt.TabFocusReason)
    qapp.processEvents()
    assert button.property("activeFocus") is True
    assert button.property("focusBorderWidth") == 2
