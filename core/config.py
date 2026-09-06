"""VoicePet 版本化配置的严格验证与原子持久化"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

SUPPORTED_LLM_APIS = frozenset({"responses", "chat_completions"})
CURRENT_CONFIG_VERSION = 2
SUPPORTED_THEME_IDS = frozenset({"sunny_sea", "deep_night", "sakura_coral"})
MAX_SYSTEM_PROMPT_CHARS = 4000
_LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_WAKE_PUNCTUATION = frozenset(
    " ，。！？、,.!?：:；;‘’\"“”()（）【】[]-_"
)


class ConfigError(RuntimeError):
    """配置路径、结构或持久化失败"""

    code = "config.error"


class ConfigLoadStatus(str, Enum):
    """配置加载使用的稳定结果状态"""

    LOADED = "loaded"
    MISSING = "missing"
    INVALID = "invalid"


def normalize_wake_keyword(value: str) -> str:
    """移除唤醒词中的空白与常见标点并验证中文长度"""

    if not isinstance(value, str):
        raise ConfigError("唤醒词必须是文本")
    normalized = "".join(
        character
        for character in value.strip()
        if not character.isspace() and character not in _WAKE_PUNCTUATION
    )
    if not 2 <= len(normalized) <= 12 or any(
        not "\u4e00" <= character <= "\u9fff" for character in normalized
    ):
        raise ConfigError("唤醒词必须包含二到十二个中文字符")
    return normalized


@dataclass(frozen=True, slots=True)
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1


@dataclass(frozen=True, slots=True)
class WakeWordConfig:
    enabled: bool = True
    keyword: str = "你好，小蓝"
    sensitivity: float = 0.5
    debounce_sec: float = 1.5


@dataclass(frozen=True, slots=True)
class AsrConfig:
    backend: str = "faster-whisper"
    model: str = "small"
    language: str = "zh"
    use_vad: bool = True


@dataclass(frozen=True, slots=True)
class LlmConfig:
    provider: str = "openai"
    api: str = "responses"
    base_url: str = ""
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "low"
    system_prompt: str = ""
    store: bool = False

    def __post_init__(self) -> None:
        if self.api not in SUPPORTED_LLM_APIS:
            raise ConfigError("LLM API 协议不受支持")
        validate_llm_base_url(self.base_url)
        if not isinstance(self.system_prompt, str):
            raise ConfigError("角色设定必须是文本")
        if len(self.system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
            raise ConfigError("角色设定不能超过 4000 个字符")


@dataclass(frozen=True, slots=True)
class TtsConfig:
    enabled: bool = True
    manual_input_enabled: bool = False
    online: str = "edge-tts"
    offline: str = "windows-sapi"
    voice: str = "zh-CN-XiaoxiaoNeural"


@dataclass(frozen=True, slots=True)
class UiConfig:
    active_skin: str = "dpsk-girl"
    always_on_top: bool = True
    hot_reload_skin: bool = True
    start_at_login: bool = False
    theme_id: str = "sunny_sea"
    reduce_motion: bool = False
    pet_scale: float = 1.0
    pet_click_through: bool = False
    preferred_screen: str = ""
    global_hotkey_enabled: bool = True
    global_hotkey: str = "Ctrl+Alt+Space"


@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    memory_enabled: bool = True
    diagnostic_recording: bool = False
    chat_history_enabled: bool = True
    auto_memory_enabled: bool = True
    chat_retention_days: int = 7
    summary_retention_days: int = 7


@dataclass(frozen=True, slots=True)
class AppConfig:
    """不包含凭据的完整不可变应用配置"""

    config_version: int = CURRENT_CONFIG_VERSION
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "config_version",
            "audio",
            "wake_word",
            "asr",
            "llm",
            "tts",
            "ui",
            "privacy",
        }
    )

    def __post_init__(self) -> None:
        if (
            type(self.config_version) is not int
            or self.config_version != CURRENT_CONFIG_VERSION
        ):
            raise ConfigError(f"只支持 config_version {CURRENT_CONFIG_VERSION}")
        if type(self.audio.sample_rate) is not int or self.audio.sample_rate <= 0:
            raise ConfigError("音频采样率必须大于零")
        if type(self.audio.channels) is not int or self.audio.channels != 1:
            raise ConfigError("当前只支持单声道音频")
        if not 0 <= self.wake_word.sensitivity <= 1:
            raise ConfigError("唤醒灵敏度必须在零到一之间")
        if self.wake_word.debounce_sec <= 0:
            raise ConfigError("唤醒防抖时间必须大于零")
        normalize_wake_keyword(self.wake_word.keyword)
        for retention in (
            self.privacy.chat_retention_days,
            self.privacy.summary_retention_days,
        ):
            if type(retention) is not int or not 1 <= retention <= 365:
                raise ConfigError("聊天和摘要保留天数必须在一到三百六十五之间")
        if self.ui.theme_id not in SUPPORTED_THEME_IDS:
            raise ConfigError("界面主题不受支持")
        if (
            type(self.ui.pet_scale) not in (int, float)
            or not 0.5 <= self.ui.pet_scale <= 2.0
        ):
            raise ConfigError("桌宠缩放必须在 0.5 到 2.0 之间")
        if not isinstance(self.ui.preferred_screen, str):
            raise ConfigError("首选显示器必须是文本")
        if (
            not isinstance(self.ui.global_hotkey, str)
            or not self.ui.global_hotkey.strip()
        ):
            raise ConfigError("全局快捷键不能为空")
        self._validate_strings()
        for value in (
            self.wake_word.enabled,
            self.asr.use_vad,
            self.llm.store,
            self.tts.enabled,
            self.tts.manual_input_enabled,
            self.ui.always_on_top,
            self.ui.hot_reload_skin,
            self.ui.start_at_login,
            self.ui.reduce_motion,
            self.ui.pet_click_through,
            self.ui.global_hotkey_enabled,
            self.privacy.memory_enabled,
            self.privacy.diagnostic_recording,
            self.privacy.chat_history_enabled,
            self.privacy.auto_memory_enabled,
        ):
            if type(value) is not bool:
                raise ConfigError("配置布尔字段类型无效")

    def _validate_strings(self) -> None:
        values = (
            self.asr.backend,
            self.asr.model,
            self.asr.language,
            self.wake_word.keyword,
            self.llm.provider,
            self.llm.api,
            self.llm.model,
            self.llm.reasoning_effort,
            self.tts.online,
            self.tts.offline,
            self.tts.voice,
            self.ui.active_skin,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ConfigError("配置文本字段不能为空")

    @classmethod
    def from_dict(cls, data: Any) -> AppConfig:
        if not isinstance(data, dict):
            raise ConfigError("配置顶层必须是对象")
        data = _migrate_config_data(data)
        # 只丢弃废弃配置键，其他设置和既有历史保存关闭意图保持不变
        raw_privacy = data.get("privacy")
        if isinstance(raw_privacy, dict):
            privacy = dict(raw_privacy)
            privacy.pop("short_term_retention_days", None)
            privacy.setdefault("chat_history_enabled", privacy.get("memory_enabled", True))
            data = {**data, "privacy": privacy}
        if unknown := set(data) - cls._FIELDS:
            raise ConfigError(f"配置包含未知字段: {len(unknown)}")
        defaults = cls()
        return cls(
            config_version=data.get("config_version", defaults.config_version),
            audio=_section(data, "audio", AudioConfig, defaults.audio),
            wake_word=_section(
                data,
                "wake_word",
                WakeWordConfig,
                defaults.wake_word,
                allowed_missing={
                    "enabled": defaults.wake_word.enabled,
                    "keyword": defaults.wake_word.keyword,
                },
            ),
            asr=_section(data, "asr", AsrConfig, defaults.asr),
            llm=_section(
                data,
                "llm",
                LlmConfig,
                defaults.llm,
                allowed_missing={
                    "base_url": defaults.llm.base_url,
                    "system_prompt": defaults.llm.system_prompt,
                },
            ),
            tts=_section(
                data,
                "tts",
                TtsConfig,
                defaults.tts,
                allowed_missing={
                    "enabled": defaults.tts.enabled,
                    "manual_input_enabled": defaults.tts.manual_input_enabled,
                },
            ),
            ui=_section(
                data,
                "ui",
                UiConfig,
                defaults.ui,
                allowed_missing={
                    "start_at_login": defaults.ui.start_at_login,
                },
            ),
            privacy=_section(
                data,
                "privacy",
                PrivacyConfig,
                defaults.privacy,
                allowed_missing={
                    "chat_history_enabled": defaults.privacy.chat_history_enabled,
                    "auto_memory_enabled": defaults.privacy.auto_memory_enabled,
                    "chat_retention_days": defaults.privacy.chat_retention_days,
                    "summary_retention_days": defaults.privacy.summary_retention_days,
                },
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _migrate_config_data(data: dict[str, Any]) -> dict[str, Any]:
    """把受支持的旧配置迁移为当前内存结构"""

    version = data.get("config_version", CURRENT_CONFIG_VERSION)
    if type(version) is not int:
        raise ConfigError("配置版本类型无效")
    if version == CURRENT_CONFIG_VERSION:
        return data
    if version != 1:
        raise ConfigError(f"只支持 config_version {CURRENT_CONFIG_VERSION}")

    migrated = dict(data)
    migrated["config_version"] = CURRENT_CONFIG_VERSION
    raw_ui = migrated.get("ui")
    if raw_ui is not None and not isinstance(raw_ui, dict):
        raise ConfigError("配置分区 ui 必须是对象")
    ui = dict(raw_ui or {})
    defaults = UiConfig()
    for field_name in (
        "theme_id",
        "reduce_motion",
        "pet_scale",
        "pet_click_through",
        "preferred_screen",
        "global_hotkey_enabled",
        "global_hotkey",
    ):
        ui.setdefault(field_name, getattr(defaults, field_name))
    if raw_ui is not None:
        migrated["ui"] = ui
    return migrated


def _section[ConfigSection](
    data: dict[str, Any],
    name: str,
    section_type: type[ConfigSection],
    default: ConfigSection,
    *,
    allowed_missing: dict[str, Any] | None = None,
) -> ConfigSection:
    raw = data.get(name)
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"配置分区 {name} 必须是对象")
    allowed = set(asdict(default))
    missing_defaults = allowed_missing or {}
    if set(raw) - allowed or allowed - set(raw) - set(missing_defaults):
        raise ConfigError(f"配置分区 {name} 字段不完整或包含未知项")
    try:
        return section_type(**{**missing_defaults, **raw})
    except (TypeError, ValueError) as error:
        raise ConfigError(f"配置分区 {name} 类型无效") from error


def is_local_llm_base_url(value: str) -> bool:
    """判断端点是否指向允许无凭据运行的本机主机"""

    if not isinstance(value, str) or not value:
        return False
    try:
        hostname = urlparse(value).hostname
    except ValueError:
        return False
    return hostname is not None and hostname.lower() in _LOCAL_LLM_HOSTS


def validate_llm_base_url(value: str) -> None:
    """验证自定义 LLM 端点不会降级公网传输安全"""

    if not isinstance(value, str):
        raise ConfigError("LLM API Base URL 类型无效")
    if not value:
        return
    if value != value.strip():
        raise ConfigError("LLM API Base URL 不能包含首尾空白")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError as error:
        raise ConfigError("LLM API Base URL 格式无效") from error
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("LLM API Base URL 不能包含凭据")
    if parsed.query or parsed.fragment:
        raise ConfigError("LLM API Base URL 不能包含查询参数或片段")
    if parsed.scheme == "https" and hostname:
        return
    if parsed.scheme == "http" and is_local_llm_base_url(value):
        return
    raise ConfigError("LLM API Base URL 必须使用 HTTPS 或本机 HTTP")


@dataclass(frozen=True, slots=True)
class ConfigLoadResult:
    """配置加载结果及可安全展示的说明"""

    config: AppConfig
    status: ConfigLoadStatus
    safe_message: str


class ConfigStore:
    """从指定路径加载并原子保存应用配置"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> ConfigLoadResult:
        if not self._path.exists():
            return ConfigLoadResult(
                AppConfig(),
                ConfigLoadStatus.MISSING,
                "配置文件不存在，已使用默认设置",
            )
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            config = AppConfig.from_dict(data)
        except (OSError, UnicodeError, json.JSONDecodeError, ConfigError) as error:
            return ConfigLoadResult(
                AppConfig(),
                ConfigLoadStatus.INVALID,
                f"配置文件无效，已使用默认设置: {type(error).__name__}",
            )
        return ConfigLoadResult(config, ConfigLoadStatus.LOADED, "配置加载成功")

    def save(self, config: AppConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            config.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self._path)
            temporary_path = None
        except OSError as error:
            raise ConfigError("配置文件保存失败") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def default_config_path() -> Path:
    """返回当前 Windows 用户的默认配置文件路径"""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ConfigError("LOCALAPPDATA 环境变量不可用")
    return Path(local_app_data) / "VoicePet" / "config.json"
