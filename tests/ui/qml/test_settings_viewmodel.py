from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtTest import QSignalSpy, QTest

from core.asr import AsrModelState
from core.config import AppConfig
from core.tts import TtsVoiceOption
from core.wake_models import WakeModelState
from ui.viewmodels.settings import SettingsViewModel


class Store:
    def __init__(self):
        self.saved = []

    def save(self, config):
        self.saved.append(config)


@pytest.mark.parametrize(
    ("section", "field", "expression", "expected"),
    [
        ("llm", "system_prompt", '"请用中文回答"', "请用中文回答"),
        ("tts", "enabled", "false", False),
        ("wake_word", "sensitivity", "0.7", 0.7),
        ("privacy", "summary_retention_days", "14", 14),
        ("privacy", "chat_retention_days", "21", 21),
        ("privacy", "auto_memory_enabled", "false", False),
        ("privacy", "chat_history_enabled", "false", False),
    ],
)
def test_qml_can_pass_setting_values(qapp, section, field, expression, expected):
    settings = SettingsViewModel(AppConfig(), Store())
    engine = QQmlEngine()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(items))
    engine.rootContext().setContextProperty("settingsModel", settings)
    component = QQmlComponent(engine)
    component.setData(
        (
            "import QtQuick\nQtObject {\n"
            "Component.onCompleted: {\n"
            f'settingsModel.set_field("{section}", "{field}", {expression})\n'
            "}\n}"
        ).encode(),
        QUrl(),
    )
    root = component.create()
    try:
        assert root is not None, component.errors()
        assert not warnings, [warning.toString() for warning in warnings]
        assert settings.draft_value(section, field) == expected
        settings.save_draft()
        assert getattr(getattr(settings.config, section), field) == expected
    finally:
        if root is not None:
            root.deleteLater()
        engine.deleteLater()
        qapp.processEvents()


def completed(value=None):
    future = Future()
    future.set_result(value)
    return future


class Credentials:
    def __init__(self):
        self.values = {}

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        return self.values.pop(name, None) is not None


class Runtime:
    def __init__(self):
        self.previews = []
        self.asr_downloads = 0
        self.wake_downloads = 0
        self.asr_state = AsrModelState.MISSING
        self.wake_state = WakeModelState.MISSING
        self.voices = (TtsVoiceOption("zh-CN-YunxiNeural", "zh-CN", "Male"),)

    def list_tts_voices(self):
        return completed(self.voices)

    def asr_model_state(self):
        return completed(self.asr_state)

    def wake_model_state(self):
        return completed(self.wake_state)

    def preview_tts_voice(self, voice_name):
        self.previews.append(voice_name)
        return completed()

    def download_asr_model(self):
        self.asr_downloads += 1
        return completed()

    def download_wake_model(self, progress):
        self.wake_downloads += 1
        progress(None)
        return completed()


def test_theme_and_pet_behavior_are_saved_immediately():
    store = Store()
    settings = SettingsViewModel(AppConfig(), store)

    settings.set_field("ui", "theme_id", "sakura_coral")
    settings.set_field("ui", "pet_scale", 1.35)

    assert store.saved == [
        replace(AppConfig(), ui=replace(AppConfig().ui, theme_id="sakura_coral")),
        replace(
            AppConfig(),
            ui=replace(AppConfig().ui, theme_id="sakura_coral", pet_scale=1.35),
        ),
    ]
    assert settings.config.ui.pet_scale == 1.35


def test_ai_base_url_stays_in_draft_until_save():
    store = Store()
    settings = SettingsViewModel(AppConfig(), store)

    settings.set_field("llm", "base_url", "https://gateway.example/v1")

    assert store.saved == []
    assert settings.draft_value("llm", "base_url") == "https://gateway.example/v1"
    assert settings.config.llm.base_url == ""

    settings.save_draft()

    assert store.saved[-1].llm.base_url == "https://gateway.example/v1"
    assert settings.config.llm.base_url == "https://gateway.example/v1"


def test_invalid_url_is_not_saved_and_has_stable_field_error():
    store = Store()
    settings = SettingsViewModel(AppConfig(), store)
    settings.set_field("llm", "base_url", "http://example.com/v1")

    settings.save_draft()

    assert store.saved == []
    assert "llm.base_url" in settings.field_errors
    assert "无效" in settings.statusMessage


def test_discard_draft_restores_committed_values():
    settings = SettingsViewModel(AppConfig(), Store())
    settings.set_field("llm", "model", "draft-model")

    settings.discard_draft()

    assert settings.draft_value("llm", "model") == AppConfig().llm.model


def test_immediate_validation_failure_does_not_change_committed_config():
    store = Store()
    settings = SettingsViewModel(AppConfig(), store)

    settings.set_field("ui", "theme_id", "unknown")

    assert settings.config.ui.theme_id == "sunny_sea"
    assert store.saved == []
    assert "ui.theme_id" in settings.field_errors


def test_startup_and_hotkey_fields_are_saved_immediately():
    store = Store()
    settings = SettingsViewModel(AppConfig(), store)

    settings.set_field("ui", "start_at_login", True)
    settings.set_field("ui", "global_hotkey", "Ctrl+Shift+Space")

    assert settings.config.ui.start_at_login is True
    assert settings.config.ui.global_hotkey == "Ctrl+Shift+Space"
    assert len(store.saved) == 2


def test_credentials_are_encrypted_without_entering_config_draft():
    credentials = Credentials()
    settings = SettingsViewModel(
        AppConfig(),
        Store(),
        credential_store=credentials,
    )

    settings.save_api_key("sk-private")

    assert credentials.values == {"openai_api_key": "sk-private"}
    assert "sk-private" not in str(settings.draft_value("llm", "model"))
    assert settings.status_message == "API Key 已加密保存，重启 VoicePet 后生效"

    settings.delete_api_key()
    assert credentials.values == {}


def test_voice_preview_and_model_downloads_delegate_to_runtime():
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    changed = QSignalSpy(settings.operationBusyChanged)

    settings.preview_voice("zh-CN-XiaoxiaoNeural")
    settings.download_asr_model()
    settings.download_wake_model()

    assert runtime.previews == ["zh-CN-XiaoxiaoNeural"]
    assert runtime.asr_downloads == 1
    assert runtime.wake_downloads == 1
    assert settings.operation_busy is False
    assert changed.count() == 6


@pytest.mark.parametrize("model", ["asr", "wake"])
@pytest.mark.parametrize("state", ["cached", "ready"])
def test_download_skips_existing_models(model, state):
    runtime = Runtime()
    setattr(runtime, f"{model}_state", state)
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)

    getattr(settings, f"download_{model}_model")()

    assert getattr(runtime, f"{model}_downloads") == 0
    assert "已下载" in settings.statusMessage
    assert not settings.operationBusy


@pytest.mark.parametrize("model", ["asr", "wake"])
def test_model_state_check_and_download_are_one_busy_operation(model):
    runtime = Runtime()
    state_future = Future()
    download_future = Future()
    calls = []
    setattr(runtime, f"{model}_model_state", lambda: state_future)
    setattr(runtime, f"download_{model}_model", lambda *args: calls.append(model) or download_future)
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    changed = QSignalSpy(settings.operationBusyChanged)

    getattr(settings, f"download_{model}_model")()
    assert settings.operationBusy
    assert "检查" in settings.statusMessage
    getattr(settings, f"download_{model}_model")()
    assert "进行" in settings.statusMessage
    assert calls == []

    state_future.set_result("missing")
    assert calls == [model]
    assert settings.operationBusy
    assert "下载" in settings.statusMessage
    download_future.set_result(None)
    assert not settings.operationBusy
    assert changed.count() == 2


@pytest.mark.parametrize("model", ["asr", "wake"])
def test_failed_model_state_check_does_not_start_download(model):
    runtime = Runtime()
    failure = Future()
    failure.set_exception(RuntimeError("offline"))
    setattr(runtime, f"{model}_model_state", lambda: failure)
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)

    getattr(settings, f"download_{model}_model")()

    assert getattr(runtime, f"{model}_downloads") == 0
    assert "失败" in settings.statusMessage
    assert not settings.operationBusy


def test_voice_catalog_preserves_saved_and_draft_selections():
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    saved_voice = settings.draft_value("tts", "voice")

    settings.refresh_voices()

    assert settings.draft_value("tts", "voice") == saved_voice
    assert {"label": runtime.voices[0].label, "short_name": "zh-CN-YunxiNeural"} in settings.voiceOptions
    assert saved_voice in [item["short_name"] for item in settings.voiceOptions]
    settings.set_field("tts", "voice", "custom-draft")
    settings.refresh_voices()
    assert settings.draft_value("tts", "voice") == "custom-draft"
    assert "custom-draft" in [item["short_name"] for item in settings.voiceOptions]
    assert settings.config.tts.voice == saved_voice


def test_voice_refresh_failure_retains_catalog_and_allows_retry():
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.refresh_voices()
    previous = settings.voiceOptions
    future = Future()
    runtime.list_tts_voices = lambda: future

    settings.refresh_voices()
    assert settings.voicesLoading
    future.set_exception(RuntimeError("offline"))
    assert not settings.voicesLoading
    assert "失败" in settings.voicesError
    assert settings.voiceOptions == previous

    runtime.list_tts_voices = lambda: completed(runtime.voices)
    settings.refresh_voices()
    assert settings.voicesError == ""


def test_binding_runtime_defers_catalog_request_until_page_requests_it():
    settings = SettingsViewModel(AppConfig(), Store())
    runtime = Runtime()
    requests = []
    runtime.list_tts_voices = lambda: requests.append(True) or completed(runtime.voices)

    settings.bind_services(runtime=runtime)

    assert requests == []
    settings.refresh_voices()
    assert runtime.voices[0].label in [item["label"] for item in settings.voiceOptions]


@pytest.mark.parametrize("opened", [True, False])
def test_open_data_directory_uses_native_local_url(tmp_path, monkeypatch, opened):
    urls = []
    monkeypatch.setattr(
        "PySide6.QtGui.QDesktopServices.openUrl",
        lambda url: urls.append(url) or opened,
    )
    settings = SettingsViewModel(AppConfig(), Store(), data_directory=tmp_path)

    settings.open_data_directory()

    assert urls == [QUrl.fromLocalFile(str(tmp_path))]
    assert settings.dataDirectory == str(tmp_path)
    assert ("失败" in settings.statusMessage) is (not opened)


@pytest.mark.parametrize("model", ["asr", "wake"])
def test_failed_download_restores_buttons_for_retry(model):
    runtime = Runtime()
    pending = Future()
    setattr(runtime, f"download_{model}_model", lambda *args: pending)
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)

    getattr(settings, f"download_{model}_model")()
    pending.set_exception(OSError("network unavailable"))

    assert not settings.operationBusy
    assert "失败" in settings.statusMessage


def test_runtime_worker_completions_are_applied_on_ui_thread(qapp):
    runtime = Runtime()
    model_state = Future()
    voices = Future()
    runtime.asr_model_state = lambda: model_state
    runtime.list_tts_voices = lambda: voices
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.download_asr_model()
    settings.refresh_voices()

    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(model_state.set_result, AsrModelState.CACHED).result()
        worker.submit(voices.set_result, runtime.voices).result()

    assert settings.operationBusy
    assert settings.voicesLoading
    qapp.processEvents()
    assert not settings.operationBusy
    assert not settings.voicesLoading
    assert runtime.asr_downloads == 0
    assert runtime.voices[0].label in [option["label"] for option in settings.voiceOptions]


def test_voice_refresh_reports_count_and_late_completion_stays_off_other_category(qapp):
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.set_status_context("voice")
    settings.refresh_voices()
    assert "1" in settings.statusMessage and "完成" in settings.statusMessage
    pending = Future()
    runtime.list_tts_voices = lambda: pending
    settings.refresh_voices()
    settings.set_status_context("ai")
    assert settings.statusMessage == ""
    pending.set_result(runtime.voices)
    assert settings.statusMessage == ""


def test_late_download_does_not_restore_status_after_navigation(qapp):
    runtime = Runtime()
    pending = Future()
    runtime.asr_model_state = lambda: pending
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.set_status_context("voice")
    settings.download_asr_model()
    settings.set_status_context("privacy")
    pending.set_result("missing")
    assert runtime.asr_downloads == 1
    assert settings.statusMessage == ""


def test_asr_download_requires_selected_model_to_match_running_model(qapp):
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.set_field("asr", "model", "large-v3")
    settings.save_draft()
    settings.download_asr_model()
    assert runtime.asr_downloads == 0
    assert "重启" in settings.statusMessage
    assert "small" in settings.asrDownloadNote


def test_immediate_settings_are_not_reported_as_discardable(qapp):
    settings = SettingsViewModel(AppConfig(), Store())
    settings.set_field("ui", "pet_scale", 1.2)
    assert not settings.hasDraftChanges
    settings.set_field("llm", "model", "draft")
    assert settings.hasDraftChanges
    settings.discard_draft()
    assert not settings.hasDraftChanges
    assert settings.config.ui.pet_scale == 1.2


def test_terminal_status_expires_but_pending_operation_does_not(qapp):
    runtime = Runtime()
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings._status_timer.setInterval(20)
    settings.save_draft()
    QTest.qWait(40)
    assert settings.statusMessage == ""
    pending = Future()
    runtime.list_tts_voices = lambda: pending
    settings.refresh_voices()
    QTest.qWait(40)
    assert "正在" in settings.statusMessage
    pending.set_result(runtime.voices)
    assert "完成" in settings.statusMessage
    QTest.qWait(40)
    assert settings.statusMessage == ""


def test_asr_completion_names_running_model_when_selection_changes_mid_download(qapp):
    runtime = Runtime()
    pending = Future()
    runtime.download_asr_model = lambda: pending
    settings = SettingsViewModel(AppConfig(), Store(), runtime=runtime)
    settings.download_asr_model()
    settings.set_field("asr", "model", "large-v3")
    pending.set_result(None)
    assert "small" in settings.statusMessage
    assert "large-v3" not in settings.statusMessage
