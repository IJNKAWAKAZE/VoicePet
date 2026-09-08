"""把子系统异常映射为完整且不泄漏正文的运行时错误事件"""

from __future__ import annotations

from .events import (
    _RUNTIME_ERROR_COMPONENTS,
    CorrelationId,
    ErrorSeverity,
    RuntimeErrorEvent,
    TurnId,
    _is_safe_diagnostic_identifier,
)


def runtime_error_event(
    turn_id: TurnId,
    correlation_id: CorrelationId,
    error: BaseException,
    *,
    error_code: str | None = None,
    retryable: bool | None = None,
) -> RuntimeErrorEvent:
    """仅保留异常类型并生成适合用户操作的安全消息"""

    resolved_code = (
        error_code if error_code is not None else getattr(error, "code", "runtime.error")
    )
    if resolved_code not in _RUNTIME_ERROR_COMPONENTS:
        resolved_code = "runtime.error"
    resolved_retryable = (
        bool(getattr(error, "retryable", False))
        if retryable is None
        else retryable
    )
    component = _RUNTIME_ERROR_COMPONENTS[resolved_code]
    safe_message = _safe_message(resolved_code, resolved_retryable)
    user_action_required = resolved_code in {
        "audio.device",
        "audio.configuration",
        "asr.configuration",
        "asr.model",
        "llm.configuration",
        "tts.configuration",
        "tool.execution",
        "wake.configuration",
        "runtime.budget",
    }
    severity = (
        ErrorSeverity.WARNING if resolved_retryable else ErrorSeverity.ERROR
    )
    exception_type = type(error).__name__
    if not _is_safe_diagnostic_identifier(exception_type):
        exception_type = "Exception"
    return RuntimeErrorEvent(
        turn_id,
        correlation_id,
        resolved_code,
        component,
        severity,
        resolved_retryable,
        user_action_required,
        safe_message,
        {"exception_type": exception_type},
    )


def _safe_message(error_code: str, retryable: bool) -> str:
    messages = {
        "audio.device": "麦克风设备不可用，请检查系统权限和设备连接",
        "audio.configuration": "音频配置无效，请检查设置",
        "asr.configuration": "语音识别配置无效，请检查设置并重新启动",
        "asr.model": "语音识别模型不可用，请运行诊断或更换模型",
        "asr.transcription": "语音识别暂时失败，请重试",
        "llm.configuration": "LLM 请求配置无效，请检查模型、API 地址和凭据",
        "llm.network": "LLM 网络暂时不可用，请稍后重试",
        "llm.protocol": "LLM 返回格式无效，请稍后重试",
        "tts.configuration": "语音合成配置无效，请检查设置",
        "tts.network": "在线语音服务暂时不可用，正在尝试本地语音",
        "tts.synthesis": "语音合成失败，回复仍可在气泡中查看",
        "tts.playback": "语音播放失败，回复仍可在气泡中查看",
        "tool.execution": "工具执行状态未知，请先查看最近操作记录",
        "runtime.budget": "本轮处理已达到资源上限，请缩短请求后重试",
    }
    if error_code in messages:
        return messages[error_code]
    if retryable:
        return "运行时操作暂时失败，请稍后重试"
    return "运行时操作失败，请运行诊断检查"
