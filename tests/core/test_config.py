import json
from dataclasses import FrozenInstanceError, replace

import pytest

import core.config as config_module
from core.config import (
    AppConfig,
    ConfigError,
    ConfigLoadStatus,
    ConfigStore,
    default_config_path,
)


@pytest.mark.parametrize("value", ["auto", "minimal", "low", "medium", "high", "xhigh"])
def test_llm_reasoning_effort_accepts_all_ui_values(value):
    data = AppConfig().to_dict()
    data["llm"]["reasoning_effort"] = value
    assert AppConfig.from_dict(data).llm.reasoning_effort == value


def test_llm_reasoning_effort_rejects_unknown_value():
    data = AppConfig().to_dict()
    data["llm"]["reasoning_effort"] = "turbo"
    with pytest.raises(ConfigError, match="思考强度"):
        AppConfig.from_dict(data)


def legacy_config_dict():
    data = AppConfig().to_dict()
    data["config_version"] = 1
    for field in (
        "theme_id",
        "reduce_motion",
        "pet_scale",
        "pet_click_through",
        "preferred_screen",
        "global_hotkey_enabled",
        "global_hotkey",
    ):
        data["ui"].pop(field, None)
    return data


def test_memory_settings_have_independent_defaults_and_round_trip():
    privacy = AppConfig().privacy
    assert privacy.chat_history_enabled is True
    assert privacy.auto_memory_enabled is True
    assert privacy.chat_retention_days == 7
    assert privacy.summary_retention_days == 7
    updated = replace(AppConfig(), privacy=replace(
        privacy, chat_history_enabled=False, auto_memory_enabled=False,
        chat_retention_days=2, summary_retention_days=30))
    assert AppConfig.from_dict(updated.to_dict()) == updated
    assert "short_term_retention_days" not in updated.to_dict()["privacy"]


def test_old_memory_settings_preserve_disabled_history_and_unrelated_config():
    data = AppConfig().to_dict()
    data["privacy"] = {"memory_enabled": False, "diagnostic_recording": True,
                       "short_term_retention_days": 100}
    data["llm"]["model"] = "synthetic-local-model"
    loaded = AppConfig.from_dict(data)
    assert loaded.privacy.memory_enabled is False
    assert loaded.privacy.chat_history_enabled is False
    assert loaded.privacy.chat_retention_days == 7
    assert loaded.privacy.summary_retention_days == 7
    assert loaded.privacy.diagnostic_recording is True
    assert loaded.llm.model == "synthetic-local-model"
    assert data["privacy"]["short_term_retention_days"] == 100


@pytest.mark.parametrize("field", ["chat_retention_days", "summary_retention_days"])
@pytest.mark.parametrize("value", [True, 1.5, "7", 0, 366])
def test_memory_retention_requires_bounded_integer(field, value):
    data = AppConfig().to_dict()
    data["privacy"][field] = value
    with pytest.raises(ConfigError):
        AppConfig.from_dict(data)


@pytest.mark.parametrize("field", ["chat_history_enabled", "auto_memory_enabled"])
def test_new_memory_flags_require_real_booleans(field):
    data = AppConfig().to_dict()
    data["privacy"][field] = 1
    with pytest.raises(ConfigError):
        AppConfig.from_dict(data)


def test_default_config_matches_product_defaults_and_is_immutable():
    config = AppConfig()

    assert config.config_version == 2
    assert config.audio.sample_rate == 16000
    assert config.audio.channels == 1
    assert config.wake_word.enabled is True
    assert config.wake_word.keyword == "你好，小蓝"
    assert config.wake_word.sensitivity == 0.5
    assert config.asr.model == "small"
    assert config.llm.model == "gpt-5.6-terra"
    assert config.llm.api == "responses"
    assert config.llm.base_url == ""
    assert config.llm.system_prompt == ""
    assert config.llm.store is False
    assert config.tts.enabled is True
    assert config.tts.manual_input_enabled is False
    assert config.tts.voice == "zh-CN-XiaoxiaoNeural"
    assert config.ui.active_skin == "dpsk-girl"
    assert config.ui.start_at_login is False
    assert config.ui.theme_id == "sunny_sea"
    assert config.ui.reduce_motion is False
    assert config.ui.pet_scale == 1.0
    assert config.ui.pet_click_through is False
    assert config.ui.preferred_screen == ""
    assert config.ui.global_hotkey_enabled is True
    assert config.ui.global_hotkey == "Ctrl+Alt+Space"
    assert config.privacy.memory_enabled is True
    assert config.privacy.chat_retention_days == 7
    assert config.privacy.summary_retention_days == 7
    with pytest.raises(FrozenInstanceError):
        config.config_version = 2


def test_llm_config_accepts_chat_protocol_and_loopback_endpoint():
    config = replace(
        AppConfig(),
        llm=replace(
            AppConfig().llm,
            api="chat_completions",
            base_url="http://localhost:11434/v1",
        ),
    )

    assert config.llm.api == "chat_completions"
    assert config.llm.base_url == "http://localhost:11434/v1"


def test_v1_config_without_llm_base_url_uses_official_default():
    data = legacy_config_dict()
    del data["llm"]["base_url"]

    assert AppConfig.from_dict(data).llm.base_url == ""


def test_v1_config_without_system_prompt_uses_blank_persona():
    data = legacy_config_dict()
    del data["llm"]["system_prompt"]

    assert AppConfig.from_dict(data).llm.system_prompt == ""


def test_v1_config_without_tts_enabled_keeps_voice_on():
    data = legacy_config_dict()
    del data["tts"]["enabled"]

    assert AppConfig.from_dict(data).tts.enabled is True


def test_v1_config_without_manual_input_tts_uses_quiet_default():
    data = legacy_config_dict()
    del data["tts"]["manual_input_enabled"]

    assert AppConfig.from_dict(data).tts.manual_input_enabled is False


def test_v1_config_without_wake_fields_uses_chinese_defaults():
    data = legacy_config_dict()
    del data["wake_word"]["enabled"]
    del data["wake_word"]["keyword"]

    loaded = AppConfig.from_dict(data)

    assert loaded.wake_word.enabled is True
    assert loaded.wake_word.keyword == "你好，小蓝"


def test_v1_config_without_startup_setting_uses_disabled_default():
    data = legacy_config_dict()
    del data["ui"]["start_at_login"]

    assert AppConfig.from_dict(data).ui.start_at_login is False


def test_version_one_config_migrates_ui_defaults_without_rewriting_file(tmp_path):
    path = tmp_path / "config.json"
    legacy = legacy_config_dict()
    path.write_text(json.dumps(legacy), encoding="utf-8")

    result = ConfigStore(path).load()

    assert result.status is ConfigLoadStatus.LOADED
    assert result.config.config_version == 2
    assert result.config.ui.theme_id == "sunny_sea"
    assert result.config.ui.pet_scale == 1.0
    assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == 1


def test_wake_word_fields_round_trip_in_config(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = replace(
        AppConfig(),
        wake_word=replace(
            AppConfig().wake_word,
            enabled=False,
            keyword="你好海蓝",
        ),
    )

    store.save(config)

    assert store.load().config.wake_word == config.wake_word


def test_wake_keyword_normalization_removes_common_separators():
    assert config_module.normalize_wake_keyword(" 你 好，小蓝！ ") == "你好小蓝"


@pytest.mark.parametrize(
    "keyword",
    ["", "小", "hello", "1234", "中文唤醒词超过十二个汉字的长度"],
)
def test_wake_word_rejects_invalid_keyword(keyword):
    with pytest.raises(ConfigError, match="唤醒词"):
        replace(
            AppConfig(),
            wake_word=replace(AppConfig().wake_word, keyword=keyword),
        )


def test_wake_word_enabled_rejects_non_boolean_value():
    with pytest.raises(ConfigError, match="布尔"):
        replace(
            AppConfig(),
            wake_word=replace(AppConfig().wake_word, enabled=1),
        )


def test_tts_enabled_round_trips_in_config(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = replace(
        AppConfig(),
        tts=replace(AppConfig().tts, enabled=False),
    )

    store.save(config)

    assert store.load().config.tts.enabled is False


def test_tts_enabled_rejects_non_boolean_value():
    with pytest.raises(ConfigError):
        replace(
            AppConfig(),
            tts=replace(AppConfig().tts, enabled=1),
        )


def test_start_at_login_rejects_non_boolean_value():
    with pytest.raises(ConfigError):
        replace(
            AppConfig(),
            ui=replace(AppConfig().ui, start_at_login=1),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("theme_id", "unknown"),
        ("reduce_motion", 1),
        ("pet_scale", 0.49),
        ("pet_scale", 2.01),
        ("pet_scale", True),
        ("pet_click_through", 1),
        ("preferred_screen", 123),
        ("global_hotkey_enabled", 1),
        ("global_hotkey", ""),
    ],
)
def test_ui_config_rejects_invalid_theme_behavior_and_hotkey(field, value):
    with pytest.raises(ConfigError):
        replace(
            AppConfig(),
            ui=replace(AppConfig().ui, **{field: value}),
        )


def test_manual_input_tts_round_trips_and_rejects_non_boolean_value(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = replace(
        AppConfig(),
        tts=replace(AppConfig().tts, manual_input_enabled=True),
    )

    store.save(config)

    assert store.load().config.tts.manual_input_enabled is True
    with pytest.raises(ConfigError):
        replace(
            AppConfig(),
            tts=replace(AppConfig().tts, manual_input_enabled=1),
        )


def test_system_prompt_round_trips_in_non_secret_config(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = replace(
        AppConfig(),
        llm=replace(AppConfig().llm, system_prompt="你是一只蓝色猫娘"),
    )

    store.save(config)

    assert store.load().config.llm.system_prompt == "你是一只蓝色猫娘"


@pytest.mark.parametrize("system_prompt", ["角色" * 2001, 123])
def test_system_prompt_rejects_oversized_or_non_text_values(system_prompt):
    with pytest.raises(ConfigError, match="角色设定"):
        replace(
            AppConfig(),
            llm=replace(AppConfig().llm, system_prompt=system_prompt),
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com/v1",
        "https://user:secret@example.com/v1",
        "https://example.com/v1?token=secret",
        "https://example.com/v1#fragment",
        " https://example.com/v1",
    ],
)
def test_llm_config_rejects_unsafe_base_url(base_url):
    with pytest.raises(ConfigError):
        replace(AppConfig(), llm=replace(AppConfig().llm, base_url=base_url))


@pytest.mark.parametrize(
    "base_url",
    [
        "https://gateway.example/v1",
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_llm_config_accepts_secure_and_loopback_base_urls(base_url):
    config = replace(AppConfig(), llm=replace(AppConfig().llm, base_url=base_url))

    assert config.llm.base_url == base_url


def test_llm_config_rejects_unknown_api_protocol():
    with pytest.raises(ConfigError, match="协议"):
        replace(AppConfig(), llm=replace(AppConfig().llm, api="automatic"))


def test_config_round_trip_uses_exact_schema_and_contains_no_secret_fields(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig()

    store.save(config)
    loaded = store.load()
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))

    assert loaded.status is ConfigLoadStatus.LOADED
    assert loaded.config == config
    assert set(data) == {
        "config_version",
        "audio",
        "wake_word",
        "asr",
        "llm",
        "tts",
        "ui",
        "privacy",
    }
    assert "api_key" not in json.dumps(data)
    assert "access_key" not in json.dumps(data)


def test_missing_config_returns_defaults_without_creating_file(tmp_path):
    path = tmp_path / "config.json"
    result = ConfigStore(path).load()

    assert result.status is ConfigLoadStatus.MISSING
    assert result.config == AppConfig()
    assert not path.exists()


def test_corrupt_or_invalid_config_returns_defaults_and_preserves_source(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{private invalid json", encoding="utf-8")
    corrupt = ConfigStore(path).load()
    original = path.read_text(encoding="utf-8")

    assert corrupt.status is ConfigLoadStatus.INVALID
    assert corrupt.config == AppConfig()
    assert "private" not in corrupt.safe_message
    assert original == "{private invalid json"

    path.write_text(
        json.dumps({"config_version": 1, "unexpected": True}),
        encoding="utf-8",
    )
    invalid = ConfigStore(path).load()
    assert invalid.status is ConfigLoadStatus.INVALID


@pytest.mark.parametrize(
    "data",
    [
        {"config_version": 3},
        {"config_version": True},
        {"audio": {"sample_rate": 0, "channels": 1}},
        {"wake_word": {"sensitivity": 2, "debounce_sec": 1.5}},
        {"llm": {"provider": "openai", "api": "responses", "model": "", "reasoning_effort": "low", "store": False}},
        {"privacy": {"memory_enabled": 1, "diagnostic_recording": False}},
        {
            "privacy": {
                "memory_enabled": True,
                "diagnostic_recording": False,
                "summary_retention_days": 0,
            }
        },
    ],
)
def test_config_rejects_invalid_types_ranges_and_versions(data):
    with pytest.raises(ConfigError):
        AppConfig.from_dict(data)


def test_save_is_atomic_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    calls = []
    import os

    original_replace = os.replace

    def replace(source, destination):
        calls.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr("core.config.os.replace", replace)
    ConfigStore(path).save(AppConfig())

    assert len(calls) == 1
    assert calls[0][1] == path
    assert list(tmp_path.glob("*.tmp")) == []


def test_default_config_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_config_path() == tmp_path / "VoicePet" / "config.json"


def test_default_config_path_requires_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(ConfigError):
        default_config_path()
