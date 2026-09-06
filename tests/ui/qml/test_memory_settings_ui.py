from concurrent.futures import Future

import pytest

from core.config import AppConfig
from ui.viewmodels.settings import SettingsViewModel


def completed(value=None):
    future = Future()
    future.set_result(value)
    return future


def test_saved_privacy_and_model_changes_reach_runtime_in_order(qapp):
    from test_qml_application import build_controller

    controller, runtime, _tray, _qml, _shell, _chat, _dialogs, _interaction, settings, _positions = build_controller(qapp)
    calls = []
    runtime.invalidate_memory = lambda: calls.append("invalidate") or completed()
    runtime.configure_memory = lambda privacy: calls.append(privacy) or completed()
    try:
        settings.set_field("llm", "model", "synthetic-model")
        settings.set_field("privacy", "auto_memory_enabled", False)
        settings.set_field("privacy", "summary_retention_days", 14)
        settings.save_draft()
        assert calls == ["invalidate", settings.config.privacy]
        assert calls[1].auto_memory_enabled is False
        assert calls[1].summary_retention_days == 14
        settings.set_field("ui", "reduce_motion", True)
        assert len(calls) == 2
    finally:
        controller.close()


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_credential_change_pauses_old_memory_provider(qapp, operation):
    from test_settings_viewmodel import Credentials, Store

    class Runtime:
        def __init__(self):
            self.pending = Future()
            self.calls = 0

        def invalidate_memory(self):
            self.calls += 1
            return self.pending

    runtime = Runtime()
    credentials = Credentials()
    credentials.set("openai_api_key", "synthetic")
    settings = SettingsViewModel(AppConfig(), Store(), credential_store=credentials, runtime=runtime)
    if operation == "save":
        settings.save_api_key("synthetic-new")
    else:
        settings.delete_api_key()
    assert runtime.calls == 1
    runtime.pending.set_exception(RuntimeError("private provider details"))
    qapp.processEvents()
    assert "重启" in settings.statusMessage and "暂停" in settings.statusMessage
    assert "private provider" not in settings.statusMessage


@pytest.mark.parametrize("succeeded", [True, False])
def test_privacy_reconfiguration_waits_for_model_invalidation(qapp, succeeded):
    from test_qml_application import build_controller

    controller, runtime, _tray, _qml, _shell, _chat, _dialogs, _interaction, settings, _positions = build_controller(qapp)
    pending = Future()
    applied = []
    runtime.invalidate_memory = lambda: pending
    runtime.configure_memory = lambda privacy: applied.append(privacy) or completed()
    try:
        settings.set_field("llm", "model", "synthetic-model")
        settings.set_field("privacy", "chat_retention_days", 14)
        settings.save_draft()
        assert applied == []
        # 等待期间的隐私保存也要等待同一个旧服务暂停结果
        settings.set_field("privacy", "chat_retention_days", 21)
        settings.save_draft()
        assert applied == []
        if succeeded:
            pending.set_result(None)
        else:
            pending.set_exception(RuntimeError("synthetic failure"))
        qapp.processEvents()
        assert applied == ([settings.config.privacy] if succeeded else [])
    finally:
        controller.close()
