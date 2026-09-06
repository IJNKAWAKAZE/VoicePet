from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy

from core.config import UiConfig
from ui.viewmodels.theme import ThemeViewModel


@pytest.mark.parametrize(
    ("theme_id", "canvas"),
    [
        ("sunny_sea", "#f6fbff"),
        ("deep_night", "#111827"),
        ("sakura_coral", "#fff8f8"),
    ],
)
def test_theme_switch_publishes_semantic_palette(theme_id, canvas):
    viewmodel = ThemeViewModel(UiConfig(), lambda config: None)

    viewmodel.set_theme(theme_id)

    assert viewmodel.current_theme_id == theme_id
    assert viewmodel.canvas.name() == canvas


def test_valid_theme_and_motion_changes_are_persisted_and_signalled():
    saved = []
    viewmodel = ThemeViewModel(UiConfig(), saved.append)
    theme_spy = QSignalSpy(viewmodel.themeChanged)
    motion_spy = QSignalSpy(viewmodel.reduceMotionChanged)

    viewmodel.set_theme("sakura_coral")
    viewmodel.set_reduce_motion(True)

    assert saved == [
        replace(UiConfig(), theme_id="sakura_coral"),
        replace(UiConfig(), theme_id="sakura_coral", reduce_motion=True),
    ]
    assert theme_spy.count() == 1
    assert motion_spy.count() == 1
    assert viewmodel.reduce_motion is True
    assert viewmodel.reduceMotion is True


def test_invalid_theme_keeps_current_palette_and_reports_error():
    saved = []
    viewmodel = ThemeViewModel(UiConfig(), saved.append)
    error_spy = QSignalSpy(viewmodel.errorOccurred)

    viewmodel.set_theme("unknown")

    assert viewmodel.current_theme_id == "sunny_sea"
    assert saved == []
    assert error_spy.count() == 1


def test_failed_persistence_does_not_publish_partial_change():
    def fail(_config):
        raise OSError("disk details")

    viewmodel = ThemeViewModel(UiConfig(), fail)
    error_spy = QSignalSpy(viewmodel.errorOccurred)

    viewmodel.set_theme("deep_night")

    assert viewmodel.current_theme_id == "sunny_sea"
    assert error_spy.count() == 1
    assert "disk details" not in error_spy.at(0)[0]


def test_palette_exposes_accessible_interaction_tokens():
    viewmodel = ThemeViewModel(UiConfig(theme_id="deep_night"), lambda config: None)

    assert viewmodel.primary.name() == "#69c7f2"
    assert viewmodel.interaction_primary.name() == "#69c7f2"
    assert viewmodel.text.name() == "#f3f7fb"
    assert viewmodel.danger.isValid()
    assert viewmodel.focus.isValid()
    assert viewmodel.motionStandard == 180


@pytest.mark.parametrize(
    "theme_id",
    ["sunny_sea", "deep_night", "sakura_coral"],
)
def test_theme_publishes_existing_welcome_and_background_assets(theme_id):
    viewmodel = ThemeViewModel(UiConfig(theme_id=theme_id), lambda config: None)

    welcome = Path(viewmodel.welcomeImage.toLocalFile())
    background = Path(viewmodel.backgroundImage.toLocalFile())

    assert welcome == Path(f"assets/ui/themes/{theme_id}/welcome.webp").resolve()
    assert background == Path(
        f"assets/ui/themes/{theme_id}/background.webp"
    ).resolve()
    assert welcome.is_file()
    assert background.is_file()
    assert Path(viewmodel.brandMark.toLocalFile()) == Path(
        "assets/ui/brand/voicepet-mark.svg"
    ).resolve()
