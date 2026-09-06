from concurrent.futures import Future

import pytest
from PySide6.QtCore import QObject, QPoint
from test_qml_application import build_controller, completed


@pytest.fixture
def mute_app(qapp):
    controller, runtime, tray, qml, _, _, dialogs, _, settings, *_ = build_controller(qapp)
    controller.start()
    yield controller, runtime, tray, qml.root_objects[0], dialogs, settings
    controller.close()


def entries(context):
    _, _, tray, root, _, _ = context
    tray.context_menu_requested.emit(QPoint(100, 100))
    return {item["id"]: item for item in root.findChild(QObject, "trayMenuWindow").property("entries")}


def test_tray_has_one_mouse_interaction_toggle(mute_app):
    actions = entries(mute_app)
    assert "clickThrough" in actions
    assert "restore" not in actions


def test_temporary_mute_preserves_saved_speech_preference(mute_app):
    controller, runtime, _, _, _, settings = mute_app
    settings.set_field("tts", "enabled", False)
    settings.save_draft()
    runtime.speech_enabled.clear()
    muted = []
    runtime.set_audio_muted = lambda value: muted.append(value) or completed()
    controller._handle_pet_menu_action("mute")
    controller._handle_pet_menu_action("mute")
    assert muted == [True, False]
    assert runtime.speech_enabled == []
    assert settings.config.tts.enabled is False


def test_mute_waits_for_runtime_success_and_ignores_repeat_click(mute_app):
    controller, runtime, *_ = mute_app
    pending = Future()
    calls = []
    runtime.set_audio_muted = lambda value: calls.append(value) or pending
    controller._handle_pet_menu_action("mute")
    controller._handle_pet_menu_action("mute")
    assert calls == [True]
    assert entries(mute_app)["mute"]["enabled"] is False
    assert not controller._muted
    pending.set_result(None)
    assert controller._muted
    assert entries(mute_app)["mute"]["label"] == "取消静音"


def test_failed_mute_keeps_menu_state_and_shows_safe_feedback(mute_app):
    controller, runtime, _, _, dialogs, _ = mute_app
    pending = Future()
    runtime.set_audio_muted = lambda value: pending
    controller._handle_pet_menu_action("mute")
    pending.set_exception(RuntimeError("private backend detail"))
    assert not controller._muted
    assert entries(mute_app)["mute"]["enabled"] is True
    assert entries(mute_app)["mute"]["label"] == "静音"
    model = dialogs.toastModel
    assert model.rowCount() == 1
    text = model.data(model.index(0), next(k for k, v in model.roleNames().items() if v == b"message"))
    assert "失败" in text and "private" not in text


def test_saving_speech_preference_does_not_clear_temporary_mute(mute_app):
    controller, runtime, _, _, _, settings = mute_app
    calls = []
    runtime.set_audio_muted = lambda value: calls.append(value) or completed()
    controller._handle_pet_menu_action("mute")
    settings.set_field("tts", "enabled", False)
    settings.save_draft()
    settings.set_field("tts", "enabled", True)
    settings.save_draft()
    assert calls == [True]
    assert entries(mute_app)["mute"]["label"] == "取消静音"
    assert settings.config.tts.enabled


def test_failed_unmute_keeps_muted_label(mute_app):
    controller, runtime, *_ = mute_app
    runtime.set_audio_muted = lambda value: completed()
    controller._handle_pet_menu_action("mute")
    pending = Future()
    runtime.set_audio_muted = lambda value: pending
    controller._handle_pet_menu_action("mute")
    pending.set_exception(RuntimeError("backend failure"))
    assert controller._muted
    assert entries(mute_app)["mute"]["label"] == "取消静音"
