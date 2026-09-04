from dataclasses import FrozenInstanceError

import pytest

import core
from core.events import CorrelationId, ErrorSeverity, RuntimeErrorEvent, TurnId
from core.runtime_errors import runtime_error_event


def test_error_severity_and_factory_are_publicly_exported():
    assert core.ErrorSeverity is ErrorSeverity
    assert core.runtime_error_event is runtime_error_event


def test_runtime_error_event_has_complete_immutable_safe_structure():
    event = RuntimeErrorEvent(
        turn_id=TurnId.new(),
        correlation_id=CorrelationId.new(),
        error_code="llm.network",
        component="llm",
        severity=ErrorSeverity.WARNING,
        retryable=True,
        user_action_required=False,
        safe_message="网络暂时不可用，请稍后重试",
        diagnostic_context={"exception_type": "ServiceUnavailableError"},
    )

    assert event.code == event.error_code
    assert event.message == event.safe_message
    with pytest.raises(TypeError):
        event.diagnostic_context["changed"] = True
    with pytest.raises(FrozenInstanceError):
        event.safe_message = "changed"


@pytest.mark.parametrize(
    "diagnostic_context",
    [
        {"operation": ["mutable"]},
        {"operation": "C:\\Users\\A\\private-prompt.txt"},
        {"backend": "sk-private-credential"},
        {"operation": "private prompt"},
        {"exception_type": "X" * 65},
    ],
)
def test_runtime_error_event_rejects_mutable_or_unsafe_diagnostic_values(
    diagnostic_context,
):
    with pytest.raises(ValueError):
        RuntimeErrorEvent(
            turn_id=TurnId.new(),
            correlation_id=CorrelationId.new(),
            error_code="llm.network",
            component="llm",
            severity=ErrorSeverity.WARNING,
            retryable=True,
            user_action_required=False,
            safe_message="网络暂时不可用，请稍后重试",
            diagnostic_context=diagnostic_context,
        )


def test_runtime_error_event_rejects_unknown_code_and_component_mismatch():
    common = {
        "turn_id": TurnId.new(),
        "correlation_id": CorrelationId.new(),
        "severity": ErrorSeverity.ERROR,
        "retryable": False,
        "user_action_required": False,
        "safe_message": "运行时操作失败，请运行诊断检查",
        "diagnostic_context": {"exception_type": "RuntimeError"},
    }
    with pytest.raises(ValueError):
        RuntimeErrorEvent(
            error_code="llm.private_prompt",
            component="llm",
            **common,
        )
    with pytest.raises(ValueError):
        RuntimeErrorEvent(
            error_code="llm.network",
            component="audio",
            **common,
        )


def test_error_factory_maps_component_without_leaking_exception_text():
    error = type("ServiceUnavailableError", (RuntimeError,), {})(
        "Bearer abcdefghijklmnopqrstuvwxyz private prompt"
    )
    error.code = "llm.network"
    error.retryable = True

    event = runtime_error_event(
        TurnId.new(),
        CorrelationId.new(),
        error,
    )

    assert event.error_code == "llm.network"
    assert event.component == "llm"
    assert event.severity is ErrorSeverity.WARNING
    assert event.retryable is True
    assert event.user_action_required is False
    assert event.safe_message == "LLM 网络暂时不可用，请稍后重试"
    assert event.diagnostic_context == {
        "exception_type": "ServiceUnavailableError"
    }
    assert "Bearer" not in repr(event)
    assert "private prompt" not in repr(event)


def test_error_factory_replaces_untrusted_code_and_exception_type():
    error_type = type(
        "sk_private_prompt_" + "x" * 100,
        (RuntimeError,),
        {},
    )
    error = error_type("C:\\Users\\A\\private.txt Bearer private-token")
    error.code = "llm.private_prompt"
    error.retryable = True

    event = runtime_error_event(TurnId.new(), CorrelationId.new(), error)

    assert event.error_code == "runtime.error"
    assert event.component == "runtime"
    assert event.diagnostic_context == {"exception_type": "Exception"}
    assert "private" not in repr(event)


@pytest.mark.parametrize(
    ("code", "expected_component", "action_required"),
    [
        ("audio.device", "audio", True),
        ("asr.model", "asr", True),
        ("llm.configuration", "llm", True),
        ("tts.synthesis", "tts", False),
        ("tool.execution", "tool", True),
    ],
)
def test_error_factory_maps_known_components(code, expected_component, action_required):
    error = RuntimeError("private")
    error.code = code
    error.retryable = False

    event = runtime_error_event(TurnId.new(), CorrelationId.new(), error)

    assert event.component == expected_component
    assert event.user_action_required is action_required
    assert event.severity is ErrorSeverity.ERROR
