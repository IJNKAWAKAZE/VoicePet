"""VoicePet 运行时共享的不可变标识和事件"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from uuid import UUID, uuid4


class ConversationPhase(str, Enum):
    """跨运行时边界使用的稳定对话阶段值"""

    IDLE = "IDLE"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING_TOOL = "EXECUTING_TOOL"
    SYNTHESIZING = "SYNTHESIZING"
    SPEAKING = "SPEAKING"
    RECOVERING = "RECOVERING"


class ErrorSeverity(str, Enum):
    """跨运行时与 UI 边界使用的稳定错误严重程度"""

    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


_RUNTIME_ERROR_COMPONENTS = MappingProxyType(
    {
        "runtime.error": "runtime",
        "runtime.budget": "runtime",
        "audio.error": "audio",
        "audio.configuration": "audio",
        "audio.device": "audio",
        "audio.overflow": "audio",
        "audio.frame": "audio",
        "asr.error": "asr",
        "asr.configuration": "asr",
        "asr.model": "asr",
        "asr.transcription": "asr",
        "llm.error": "llm",
        "llm.configuration": "llm",
        "llm.network": "llm",
        "llm.protocol": "llm",
        "tts.error": "tts",
        "tts.configuration": "tts",
        "tts.network": "tts",
        "tts.synthesis": "tts",
        "tts.playback": "tts",
        "tool.execution": "tool",
        "wake.error": "wake",
        "wake.configuration": "wake",
        "wake.detection": "wake",
    }
)
_DIAGNOSTIC_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_LLM_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SENSITIVE_IDENTIFIER_TERMS = (
    "access_key",
    "apikey",
    "api_key",
    "bearer",
    "password",
    "private",
    "prompt",
    "secret",
    "sk-",
    "sk_",
    "token",
)


def _is_safe_diagnostic_identifier(value: object) -> bool:
    if not isinstance(value, str) or _DIAGNOSTIC_IDENTIFIER.fullmatch(value) is None:
        return False
    normalized = value.casefold()
    return not any(term in normalized for term in _SENSITIVE_IDENTIFIER_TERMS)


def validate_llm_model_name(value: object) -> str:
    """验证可安全写入结构化日志的模型标识"""

    if not isinstance(value, str):
        raise TypeError("LLM 模型名称类型无效")
    if _LLM_MODEL_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("LLM 模型名称无效")
    normalized = value.casefold()
    if any(term in normalized for term in _SENSITIVE_IDENTIFIER_TERMS):
        raise ValueError("LLM 模型名称无效")
    return value


@dataclass(frozen=True, slots=True)
class TurnId:
    """同一对话轮次中所有操作共享的标识"""

    value: UUID

    @classmethod
    def new(cls) -> "TurnId":
        return cls(uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class CorrelationId:
    """对话轮次中单个异步操作的标识"""

    value: UUID

    @classmethod
    def new(cls) -> "CorrelationId":
        return cls(uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class StateChanged:
    turn_id: TurnId
    correlation_id: CorrelationId
    previous: ConversationPhase
    current: ConversationPhase


@dataclass(frozen=True, slots=True)
class TranscriptReady:
    turn_id: TurnId
    correlation_id: CorrelationId
    text: str


@dataclass(frozen=True, slots=True)
class WakeCommandPending:
    """只说唤醒词后等待下一段命令"""

    turn_id: TurnId
    correlation_id: CorrelationId


@dataclass(frozen=True, slots=True)
class LlmUsageRecorded:
    """不包含业务正文的单次 LLM 用量事件"""

    turn_id: TurnId
    correlation_id: CorrelationId
    model: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    turn_total_tokens: int

    def __post_init__(self) -> None:
        validate_llm_model_name(self.model)
        values = (
            self.duration_ms,
            self.input_tokens,
            self.output_tokens,
            self.turn_total_tokens,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("LLM 用量字段类型无效")
        if any(value < 0 for value in values):
            raise ValueError("LLM 用量字段不能小于零")
        if self.turn_total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("单轮累计 token 不能小于本次用量")


@dataclass(frozen=True, slots=True)
class TextDelta:
    turn_id: TurnId
    correlation_id: CorrelationId
    text: str


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    turn_id: TurnId
    correlation_id: CorrelationId
    tool_call_id: str
    summary: str
    risk: str


@dataclass(frozen=True, slots=True)
class ToolResultReady:
    turn_id: TurnId
    correlation_id: CorrelationId
    tool_call_id: str
    status: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class MemoryResultReady:
    turn_id: TurnId
    correlation_id: CorrelationId
    status: str
    message: str
    affected: int


@dataclass(frozen=True, slots=True)
class SpeakRequested:
    turn_id: TurnId
    correlation_id: CorrelationId
    text: str


@dataclass(frozen=True, slots=True)
class RuntimeErrorEvent:
    turn_id: TurnId
    correlation_id: CorrelationId
    error_code: str
    component: str
    severity: ErrorSeverity
    retryable: bool
    user_action_required: bool
    safe_message: str
    diagnostic_context: Mapping[str, object]

    def __post_init__(self) -> None:
        expected_component = _RUNTIME_ERROR_COMPONENTS.get(self.error_code)
        if expected_component is None:
            raise ValueError("运行时错误代码不在稳定集合中")
        if self.component != expected_component:
            raise ValueError("运行时错误组件与错误代码不匹配")
        if not isinstance(self.severity, ErrorSeverity):
            raise TypeError("运行时错误严重程度类型无效")
        if not isinstance(self.retryable, bool) or not isinstance(
            self.user_action_required,
            bool,
        ):
            raise TypeError("运行时错误布尔字段类型无效")
        if not isinstance(self.safe_message, str):
            raise TypeError("运行时错误安全消息类型无效")
        if (
            not self.safe_message.strip()
            or len(self.safe_message) > 512
            or any(character in self.safe_message for character in "\r\n\x00")
        ):
            raise ValueError("运行时错误安全消息不能为空")
        if not isinstance(self.diagnostic_context, Mapping):
            raise TypeError("运行时错误诊断上下文必须是映射")
        allowed_context = frozenset(
            {"exception_type", "backend", "device", "compute_type", "operation"}
        )
        if set(self.diagnostic_context) - allowed_context:
            raise ValueError("运行时错误诊断上下文字段无效")
        if not all(
            _is_safe_diagnostic_identifier(value)
            for value in self.diagnostic_context.values()
        ):
            raise ValueError("运行时错误诊断上下文值无效")
        object.__setattr__(
            self,
            "diagnostic_context",
            MappingProxyType(dict(self.diagnostic_context)),
        )

    @property
    def code(self) -> str:
        """兼容旧消费者读取错误代码"""

        return self.error_code

    @property
    def message(self) -> str:
        """兼容旧消费者且始终只返回安全消息"""

        return self.safe_message
