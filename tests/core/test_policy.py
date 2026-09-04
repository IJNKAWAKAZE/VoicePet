import base64
from dataclasses import FrozenInstanceError

import pytest

from core.policy import (
    AuthorizationError,
    AuthorizationIssuer,
    AuthorizationVerifier,
    ConcurrencyPolicy,
    ConfirmationMode,
    PolicyConfigurationError,
    PolicyDecision,
    PolicyEngine,
    PolicySettings,
    ProposalValidationError,
    RiskLevel,
    ToolManifest,
    ToolProposal,
    ToolRegistry,
    canonical_json,
)


def strict_schema():
    return {
        "type": "object",
        "maxProperties": 1,
        "properties": {
            "name": {"type": "string", "maxLength": 64},
        },
        "required": ["name"],
        "additionalProperties": False,
    }


def manifest(**overrides):
    values = {
        "name": "open_app",
        "description": "打开允许的应用",
        "input_schema": strict_schema(),
        "required_permissions": frozenset({"process.launch"}),
        "base_risk": RiskLevel.R1,
        "timeout": 10.0,
        "concurrency_policy": ConcurrencyPolicy.SERIAL,
        "supports_cancel": True,
        "supports_undo": False,
        "sensitive_fields": ("secret",),
    }
    values.update(overrides)
    return ToolManifest(**values)


def test_risk_levels_are_ordered_and_policy_enums_are_stable():
    assert RiskLevel.R0 < RiskLevel.R1 < RiskLevel.R2 < RiskLevel.R3
    assert [risk.label for risk in RiskLevel] == ["R0", "R1", "R2", "R3"]
    assert ConfirmationMode.NONE.value == "none"
    assert ConfirmationMode.VOICE.value == "voice"
    assert ConfirmationMode.UI.value == "ui"
    assert ConcurrencyPolicy.SERIAL.value == "serial"


def test_manifest_and_proposal_freeze_nested_json_values():
    tool = manifest()
    proposal = ToolProposal("call-1", "open_app", {"name": "calc"})

    with pytest.raises(FrozenInstanceError):
        tool.timeout = 20
    with pytest.raises(TypeError):
        tool.input_schema["type"] = "string"
    with pytest.raises(TypeError):
        tool.input_schema["properties"]["name"]["maxLength"] = 1
    with pytest.raises(TypeError):
        proposal.arguments["name"] = "other"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "Open App"},
        {"description": " "},
        {"timeout": 0},
        {"required_permissions": frozenset({""})},
    ],
)
def test_manifest_rejects_invalid_metadata(kwargs):
    with pytest.raises(PolicyConfigurationError):
        manifest(**kwargs)


@pytest.mark.parametrize(
    "arguments",
    [
        {1: "non-string-key"},
        {"value": float("nan")},
        {"value": object()},
    ],
)
def test_proposal_rejects_non_canonical_json(arguments):
    with pytest.raises(ProposalValidationError):
        ToolProposal("call-1", "open_app", arguments)


def test_canonical_json_is_deterministic_and_rejects_non_finite_numbers():
    assert canonical_json({"b": 2, "a": [True, None]}) == (
        '{"a":[true,null],"b":2}'
    )
    with pytest.raises(ProposalValidationError):
        canonical_json({"value": float("inf")})


def test_policy_settings_are_immutable_and_validate_confirmation_floor():
    settings = PolicySettings(
        version=3,
        disabled_tools=frozenset({"delete_file"}),
        r1_requires_confirmation=True,
        r2_confirmation=ConfirmationMode.UI,
        tool_confirmation_overrides={"open_app": ConfirmationMode.VOICE},
    )

    assert settings.version == 3
    with pytest.raises(TypeError):
        settings.tool_confirmation_overrides["open_app"] = ConfirmationMode.NONE
    with pytest.raises(PolicyConfigurationError):
        PolicySettings(version=0)
    with pytest.raises(PolicyConfigurationError):
        PolicySettings(r2_confirmation=ConfirmationMode.NONE)


def test_policy_decision_is_immutable_and_validated():
    decision = PolicyDecision(
        call_id="call-1",
        tool_name="open_app",
        allowed=True,
        risk=RiskLevel.R1,
        confirmation=ConfirmationMode.NONE,
        summary="打开计算器",
        call_fingerprint="a" * 64,
        policy_version=1,
        reason="允许自动执行",
    )

    with pytest.raises(FrozenInstanceError):
        decision.allowed = False
    with pytest.raises(PolicyConfigurationError):
        PolicyDecision(
            "call-1",
            "open_app",
            True,
            RiskLevel.R1,
            ConfirmationMode.NONE,
            "summary",
            "invalid",
            1,
            "reason",
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "maxLength": 10},
        {
            "type": "object",
            "maxProperties": 1,
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "maxProperties": 1,
            "properties": {
                "items": {"type": "array", "items": {"type": "string", "maxLength": 8}}
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    ],
)
def test_manifest_rejects_non_strict_or_unbounded_schema(schema):
    with pytest.raises(PolicyConfigurationError):
        manifest(input_schema=schema)


def test_registry_rejects_duplicates_and_engine_rejects_unknown_or_disabled_tools():
    with pytest.raises(PolicyConfigurationError, match="重复"):
        ToolRegistry([manifest(), manifest()])

    engine = PolicyEngine(
        ToolRegistry([manifest()]),
        PolicySettings(version=7, disabled_tools=frozenset({"open_app"})),
    )
    disabled = engine.evaluate(
        ToolProposal("call-1", "open_app", {"name": "calc"})
    )
    unknown = engine.evaluate(
        ToolProposal("call-2", "unknown_tool", {})
    )

    assert disabled.allowed is False
    assert disabled.policy_version == 7
    assert disabled.confirmation is ConfirmationMode.NONE
    assert unknown.allowed is False
    assert unknown.risk is RiskLevel.R3


def test_engine_validates_arguments_before_local_risk_and_summary():
    calls = []

    def risk_evaluator(arguments):
        calls.append(arguments)
        return RiskLevel.R1

    engine = PolicyEngine(
        ToolRegistry([manifest(risk_evaluator=risk_evaluator)]),
        PolicySettings(),
    )

    with pytest.raises(ProposalValidationError) as captured:
        engine.evaluate(ToolProposal("call-1", "open_app", {"name": "x" * 65}))

    assert calls == []
    assert "xxxxxxxx" not in str(captured.value)


def test_engine_uses_local_summary_and_only_allows_dynamic_risk_escalation():
    def summary(arguments):
        return f"打开 {arguments['name']}"

    high = PolicyEngine(
        ToolRegistry(
            [
                manifest(
                    risk_evaluator=lambda arguments: RiskLevel.R2,
                    impact_summarizer=summary,
                )
            ]
        ),
        PolicySettings(version=2),
    ).evaluate(ToolProposal("call-1", "open_app", {"name": "calc"}))
    low = PolicyEngine(
        ToolRegistry(
            [
                manifest(
                    base_risk=RiskLevel.R2,
                    risk_evaluator=lambda arguments: RiskLevel.R0,
                    impact_summarizer=summary,
                )
            ]
        ),
        PolicySettings(version=2),
    ).evaluate(ToolProposal("call-2", "open_app", {"name": "calc"}))

    assert high.risk is RiskLevel.R2
    assert high.summary == "打开 calc"
    assert high.confirmation is ConfirmationMode.VOICE
    assert low.risk is RiskLevel.R2


def test_engine_fails_closed_when_local_evaluator_or_summary_fails():
    def fail(arguments):
        raise RuntimeError(f"secret={arguments['name']}")

    proposal = ToolProposal("call-1", "open_app", {"name": "private-app"})
    risk_failure = PolicyEngine(
        ToolRegistry([manifest(risk_evaluator=fail)]),
        PolicySettings(),
    ).evaluate(proposal)
    summary_failure = PolicyEngine(
        ToolRegistry([manifest(impact_summarizer=fail)]),
        PolicySettings(),
    ).evaluate(proposal)

    assert risk_failure.allowed is False
    assert summary_failure.allowed is False
    assert "private-app" not in risk_failure.reason
    assert "private-app" not in summary_failure.reason


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (RiskLevel.R0, ConfirmationMode.NONE),
        (RiskLevel.R1, ConfirmationMode.NONE),
        (RiskLevel.R2, ConfirmationMode.VOICE),
        (RiskLevel.R3, ConfirmationMode.UI),
    ],
)
def test_default_confirmation_floors(risk, expected):
    decision = PolicyEngine(
        ToolRegistry([manifest(base_risk=risk)]),
        PolicySettings(),
    ).evaluate(ToolProposal("call-1", "open_app", {"name": "calc"}))

    assert decision.allowed is True
    assert decision.confirmation is expected
    assert decision.requires_authorization is (expected is not ConfirmationMode.NONE)


def test_confirmation_settings_can_only_make_policy_stricter():
    r1 = PolicyEngine(
        ToolRegistry([manifest(base_risk=RiskLevel.R1)]),
        PolicySettings(r1_requires_confirmation=True),
    ).evaluate(ToolProposal("call-1", "open_app", {"name": "calc"}))
    r2 = PolicyEngine(
        ToolRegistry([manifest(base_risk=RiskLevel.R2)]),
        PolicySettings(
            r2_confirmation=ConfirmationMode.UI,
            tool_confirmation_overrides={"open_app": ConfirmationMode.NONE},
        ),
    ).evaluate(ToolProposal("call-2", "open_app", {"name": "calc"}))
    r3 = PolicyEngine(
        ToolRegistry([manifest(base_risk=RiskLevel.R3)]),
        PolicySettings(
            tool_confirmation_overrides={"open_app": ConfirmationMode.NONE}
        ),
    ).evaluate(ToolProposal("call-3", "open_app", {"name": "calc"}))

    assert r1.confirmation is ConfirmationMode.VOICE
    assert r2.confirmation is ConfirmationMode.UI
    assert r3.confirmation is ConfirmationMode.UI


def test_settings_update_creates_new_snapshot_without_mutating_old_decision():
    engine = PolicyEngine(ToolRegistry([manifest()]), PolicySettings(version=1))
    proposal = ToolProposal("call-1", "open_app", {"name": "calc"})

    before = engine.evaluate(proposal)
    engine.update_settings(
        PolicySettings(version=2, r1_requires_confirmation=True)
    )
    after = engine.evaluate(proposal)

    assert before.policy_version == 1
    assert before.confirmation is ConfirmationMode.NONE
    assert after.policy_version == 2
    assert after.confirmation is ConfirmationMode.VOICE


def authorization_case(risk=RiskLevel.R2):
    proposal = ToolProposal("call-secret", "open_app", {"name": "private-app"})
    decision = PolicyEngine(
        ToolRegistry([manifest(base_risk=risk)]),
        PolicySettings(version=9),
    ).evaluate(proposal)
    return proposal, decision


def test_authorization_key_must_have_at_least_256_bits():
    with pytest.raises(PolicyConfigurationError, match="256"):
        AuthorizationIssuer(b"short")
    with pytest.raises(PolicyConfigurationError, match="256"):
        AuthorizationVerifier(b"short")


def test_authorization_token_is_private_valid_and_single_use():
    proposal, decision = authorization_case()
    key = b"k" * 32
    issuer = AuthorizationIssuer(key, clock=lambda: 100.0, ttl_seconds=60.0)
    verifier = AuthorizationVerifier(key, clock=lambda: 120.0)

    token = issuer.issue(decision, ConfirmationMode.VOICE)
    payload_text = base64.urlsafe_b64decode(
        token.split(".", 1)[0] + "=="
    ).decode("utf-8")
    grant = verifier.consume(token, proposal, decision)

    assert "private-app" not in token
    assert "private-app" not in payload_text
    assert grant.call_fingerprint == decision.call_fingerprint
    assert grant.policy_version == 9
    assert grant.risk is RiskLevel.R2
    assert grant.confirmation is ConfirmationMode.VOICE
    with pytest.raises(AuthorizationError, match="消费"):
        verifier.consume(token, proposal, decision)


def test_authorization_confirmation_must_meet_decision_floor():
    proposal, r2 = authorization_case(RiskLevel.R2)
    _, r3 = authorization_case(RiskLevel.R3)
    issuer = AuthorizationIssuer(b"k" * 32)

    with pytest.raises(AuthorizationError):
        issuer.issue(r2, ConfirmationMode.NONE)
    with pytest.raises(AuthorizationError):
        issuer.issue(r3, ConfirmationMode.VOICE)

    token = issuer.issue(r2, ConfirmationMode.UI)
    assert AuthorizationVerifier(b"k" * 32).consume(
        token,
        proposal,
        r2,
    ).confirmation is ConfirmationMode.UI


def test_authorization_rejects_expired_and_tampered_tokens():
    proposal, decision = authorization_case()
    key = b"k" * 32
    issuer = AuthorizationIssuer(key, clock=lambda: 10.0, ttl_seconds=5.0)
    token = issuer.issue(decision, ConfirmationMode.VOICE)

    with pytest.raises(AuthorizationError, match="过期"):
        AuthorizationVerifier(key, clock=lambda: 15.1).consume(
            token,
            proposal,
            decision,
        )

    payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = f"{payload}.{replacement}{signature[1:]}"
    with pytest.raises(AuthorizationError, match="签名"):
        AuthorizationVerifier(key, clock=lambda: 12.0).consume(
            tampered,
            proposal,
            decision,
        )


def test_authorization_is_bound_to_proposal_and_policy_snapshot():
    proposal, decision = authorization_case()
    key = b"k" * 32
    issuer = AuthorizationIssuer(key, clock=lambda: 10.0)
    token = issuer.issue(decision, ConfirmationMode.VOICE)
    wrong_proposal = ToolProposal(
        proposal.call_id,
        proposal.tool_name,
        {"name": "other-app"},
    )
    verifier = AuthorizationVerifier(key, clock=lambda: 11.0)

    with pytest.raises(AuthorizationError, match="绑定"):
        verifier.consume(token, wrong_proposal, decision)
    with pytest.raises(AuthorizationError, match="消费"):
        verifier.consume(token, proposal, decision)

    fresh_token = issuer.issue(decision, ConfirmationMode.VOICE)
    wrong_decision = PolicyDecision(
        decision.call_id,
        decision.tool_name,
        decision.allowed,
        decision.risk,
        decision.confirmation,
        decision.summary,
        decision.call_fingerprint,
        decision.policy_version + 1,
        decision.reason,
    )
    with pytest.raises(AuthorizationError, match="策略"):
        AuthorizationVerifier(key, clock=lambda: 11.0).consume(
            fresh_token,
            proposal,
            wrong_decision,
        )
