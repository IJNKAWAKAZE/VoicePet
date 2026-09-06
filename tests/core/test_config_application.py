from dataclasses import replace

import pytest

import core
from core.config import AppConfig
from core.config_application import ConfigApplicationMode, classify_config_change


def test_config_application_types_are_publicly_exported():
    assert core.ConfigApplicationMode is ConfigApplicationMode
    assert core.classify_config_change is classify_config_change


def test_classifier_distinguishes_unchanged_and_topmost_only_changes():
    current = AppConfig()
    topmost = replace(
        current,
        ui=replace(current.ui, always_on_top=False),
    )
    startup = replace(
        current,
        ui=replace(current.ui, start_at_login=True),
    )
    voice = replace(
        current,
        tts=replace(current.tts, enabled=False),
    )
    manual_voice = replace(
        current,
        tts=replace(current.tts, manual_input_enabled=True),
    )
    skin = replace(
        current,
        ui=replace(current.ui, active_skin="another-pet"),
    )
    immediate_changes = replace(
        voice,
        wake_word=replace(
            current.wake_word,
            keyword="你好海蓝",
            sensitivity=0.7,
        ),
        ui=replace(
            current.ui,
            active_skin="another-pet",
            always_on_top=False,
        ),
    )

    assert classify_config_change(current, current) is ConfigApplicationMode.NO_CHANGE
    assert (
        classify_config_change(current, topmost)
        is ConfigApplicationMode.IMMEDIATE_ONLY
    )
    assert (
        classify_config_change(current, startup)
        is ConfigApplicationMode.IMMEDIATE_ONLY
    )
    assert classify_config_change(current, voice) is ConfigApplicationMode.IMMEDIATE_ONLY
    assert (
        classify_config_change(current, manual_voice)
        is ConfigApplicationMode.IMMEDIATE_ONLY
    )
    assert classify_config_change(current, skin) is ConfigApplicationMode.IMMEDIATE_ONLY
    assert (
        classify_config_change(current, immediate_changes)
        is ConfigApplicationMode.IMMEDIATE_ONLY
    )


def test_classifier_requires_restart_for_runtime_or_mixed_changes():
    current = AppConfig()
    runtime_change = replace(
        current,
        asr=replace(current.asr, model="medium"),
    )
    mixed = replace(
        runtime_change,
        ui=replace(current.ui, always_on_top=False),
    )

    assert (
        classify_config_change(current, runtime_change)
        is ConfigApplicationMode.RESTART_REQUIRED
    )
    assert (
        classify_config_change(current, mixed)
        is ConfigApplicationMode.RESTART_REQUIRED
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("theme_id", "deep_night"),
        ("reduce_motion", True),
        ("pet_scale", 1.5),
        ("pet_click_through", True),
        ("preferred_screen", "screen-2"),
        ("global_hotkey_enabled", False),
        ("global_hotkey", "Ctrl+Shift+Space"),
    ],
)
def test_classifier_treats_new_ui_fields_as_immediate(field, value):
    current = AppConfig()
    changed = replace(
        current,
        ui=replace(current.ui, **{field: value}),
    )

    assert (
        classify_config_change(current, changed)
        is ConfigApplicationMode.IMMEDIATE_ONLY
    )
