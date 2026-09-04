from dataclasses import replace

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
    voice = replace(
        current,
        tts=replace(current.tts, enabled=False),
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
    assert classify_config_change(current, voice) is ConfigApplicationMode.IMMEDIATE_ONLY
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
