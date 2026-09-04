"""工具提议的风险决策与一次性授权"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any, ClassVar


class PolicyError(RuntimeError):
    """策略子系统可报告的基础错误"""

    code = "policy.error"


class PolicyConfigurationError(PolicyError):
    """本地工具声明或策略设置无效"""

    code = "policy.configuration"


class ProposalValidationError(PolicyError):
    """模型工具提议不符合本地契约"""

    code = "policy.proposal_invalid"


class PolicyDeniedError(PolicyError):
    """策略明确拒绝工具调用"""

    code = "policy.denied"


class AuthorizationError(PolicyError):
    """授权令牌无效、过期或已消费"""

    code = "policy.authorization"


class RiskLevel(IntEnum):
    """工具操作的稳定风险等级"""

    R0 = 0
    R1 = 1
    R2 = 2
    R3 = 3

    @property
    def label(self) -> str:
        return self.name


class ConfirmationMode(str, Enum):
    """策略允许使用的确认方式"""

    NONE = "none"
    VOICE = "voice"
    UI = "ui"


class ConcurrencyPolicy(str, Enum):
    """工具 Worker 的并发约束"""

    SERIAL = "serial"
    PER_TOOL = "per_tool"
    PARALLEL = "parallel"


JsonValue = None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
def _freeze_json[ErrorType: PolicyError](
    value: Any,
    error_type: type[ErrorType],
) -> JsonValue:
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise error_type("JSON object 的键必须是字符串")
            frozen[key] = _freeze_json(item, error_type)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, error_type) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise error_type("JSON 数字必须是有限值")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise error_type(f"JSON 值包含不支持的类型: {type(value).__name__}")


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """生成拒绝非标准数值的确定性 JSON"""

    frozen = _freeze_json(value, ProposalValidationError)
    return json.dumps(
        _thaw_json(frozen),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_bounded_schema(schema: Mapping[str, JsonValue]) -> None:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except (ImportError, ModuleNotFoundError) as error:
        raise PolicyConfigurationError(
            "缺少 jsonschema 依赖，请安装 voicepet[tools]"
        ) from error
    thawed = _thaw_json(schema)
    try:
        Draft202012Validator.check_schema(thawed)
    except SchemaError as error:
        raise PolicyConfigurationError("工具 JSON Schema 无效") from error
    _validate_schema_node(thawed, top_level=True)


def _validate_schema_node(schema: Any, *, top_level: bool = False) -> None:
    if not isinstance(schema, dict):
        raise PolicyConfigurationError("工具 Schema 节点必须是对象")
    schema_type = schema.get("type")
    if top_level and schema_type != "object":
        raise PolicyConfigurationError("工具 Schema 顶层类型必须为 object")
    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        max_properties = schema.get("maxProperties")
        if not isinstance(properties, dict):
            raise PolicyConfigurationError("对象 Schema 必须声明 properties")
        if schema.get("additionalProperties") is not False:
            raise PolicyConfigurationError("对象 Schema 必须禁止额外字段")
        if not isinstance(required, list) or set(required) != set(properties):
            raise PolicyConfigurationError("对象 Schema 必须要求全部属性")
        if not isinstance(max_properties, int) or max_properties <= 0:
            raise PolicyConfigurationError("对象 Schema 必须声明 maxProperties")
        for property_schema in properties.values():
            _validate_schema_node(property_schema)
    elif schema_type == "array":
        max_items = schema.get("maxItems")
        if not isinstance(max_items, int) or max_items <= 0:
            raise PolicyConfigurationError("数组 Schema 必须声明 maxItems")
        _validate_schema_node(schema.get("items"))
    elif schema_type == "string":
        max_length = schema.get("maxLength")
        if not isinstance(max_length, int) or max_length <= 0:
            raise PolicyConfigurationError("字符串 Schema 必须声明 maxLength")

    for keyword in ("anyOf", "allOf", "oneOf"):
        alternatives = schema.get(keyword, [])
        if isinstance(alternatives, list):
            for alternative in alternatives:
                _validate_schema_node(alternative)


RiskEvaluator = Callable[[Mapping[str, JsonValue]], RiskLevel]
ImpactSummarizer = Callable[[Mapping[str, JsonValue]], str]


@dataclass(frozen=True, slots=True)
class ToolManifest:
    """本地注册工具的完整安全声明"""

    name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    required_permissions: frozenset[str] = frozenset()
    base_risk: RiskLevel = RiskLevel.R0
    timeout: float = 10.0
    concurrency_policy: ConcurrencyPolicy = ConcurrencyPolicy.SERIAL
    supports_cancel: bool = False
    supports_undo: bool = False
    sensitive_fields: tuple[str, ...] = ()
    risk_evaluator: RiskEvaluator | None = None
    impact_summarizer: ImpactSummarizer | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name) is None:
            raise PolicyConfigurationError("工具名称格式无效")
        if not self.description.strip():
            raise PolicyConfigurationError("工具描述不能为空")
        if self.timeout <= 0:
            raise PolicyConfigurationError("工具超时必须大于零")
        permissions = frozenset(self.required_permissions)
        if any(not permission.strip() for permission in permissions):
            raise PolicyConfigurationError("工具权限名称不能为空")
        sensitive_fields = tuple(self.sensitive_fields)
        if any(not field.strip() for field in sensitive_fields):
            raise PolicyConfigurationError("敏感字段名称不能为空")
        frozen_schema = _freeze_json(
            self.input_schema,
            PolicyConfigurationError,
        )
        if not isinstance(frozen_schema, Mapping):
            raise PolicyConfigurationError("工具 Schema 必须是对象")
        _validate_bounded_schema(frozen_schema)
        object.__setattr__(self, "input_schema", frozen_schema)
        object.__setattr__(self, "required_permissions", permissions)
        object.__setattr__(self, "sensitive_fields", sensitive_fields)


@dataclass(frozen=True, slots=True)
class ToolProposal:
    """经过 JSON 基础约束的模型工具提议"""

    call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ProposalValidationError("工具调用 ID 不能为空")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.tool_name) is None:
            raise ProposalValidationError("工具名称格式无效")
        frozen = _freeze_json(self.arguments, ProposalValidationError)
        if not isinstance(frozen, Mapping):
            raise ProposalValidationError("工具参数必须是 JSON object")
        object.__setattr__(self, "arguments", frozen)


@dataclass(frozen=True, slots=True)
class PolicySettings:
    """单次策略评估使用的不可变设置快照"""

    version: int = 1
    disabled_tools: frozenset[str] = frozenset()
    r1_requires_confirmation: bool = False
    r2_confirmation: ConfirmationMode = ConfirmationMode.VOICE
    tool_confirmation_overrides: Mapping[str, ConfirmationMode] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise PolicyConfigurationError("策略版本必须大于零")
        if self.r2_confirmation is ConfirmationMode.NONE:
            raise PolicyConfigurationError("R2 工具不能关闭确认")
        disabled = frozenset(self.disabled_tools)
        if any(not name.strip() for name in disabled):
            raise PolicyConfigurationError("禁用工具名称不能为空")
        overrides = dict(self.tool_confirmation_overrides)
        if any(not name.strip() for name in overrides):
            raise PolicyConfigurationError("工具确认覆盖名称不能为空")
        if any(not isinstance(mode, ConfirmationMode) for mode in overrides.values()):
            raise PolicyConfigurationError("工具确认覆盖值无效")
        object.__setattr__(self, "disabled_tools", disabled)
        object.__setattr__(
            self,
            "tool_confirmation_overrides",
            MappingProxyType(overrides),
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """绑定提议和策略快照的最终权限决策"""

    call_id: str
    tool_name: str
    allowed: bool
    risk: RiskLevel
    confirmation: ConfirmationMode
    summary: str
    call_fingerprint: str
    policy_version: int
    reason: str

    def __post_init__(self) -> None:
        if not self.call_id.strip() or not self.tool_name.strip():
            raise PolicyConfigurationError("策略决策缺少调用标识")
        if not self.summary.strip() or not self.reason.strip():
            raise PolicyConfigurationError("策略决策说明不能为空")
        if re.fullmatch(r"[0-9a-f]{64}", self.call_fingerprint) is None:
            raise PolicyConfigurationError("调用指纹必须是 SHA-256 十六进制值")
        if self.policy_version <= 0:
            raise PolicyConfigurationError("策略决策版本必须大于零")

    @property
    def requires_authorization(self) -> bool:
        return self.allowed and self.confirmation is not ConfirmationMode.NONE


class ToolRegistry:
    """保存名称唯一的本地工具安全声明"""

    def __init__(self, manifests: list[ToolManifest] | tuple[ToolManifest, ...]) -> None:
        tools: dict[str, ToolManifest] = {}
        for manifest in manifests:
            if manifest.name in tools:
                raise PolicyConfigurationError(f"工具名称重复: {manifest.name}")
            tools[manifest.name] = manifest
        self._tools = MappingProxyType(tools)

    def get(self, name: str) -> ToolManifest | None:
        return self._tools.get(name)


class PolicyEngine:
    """按不可变设置快照评估工具提议"""

    _CONFIRMATION_RANK: ClassVar[dict[ConfirmationMode, int]] = {
        ConfirmationMode.NONE: 0,
        ConfirmationMode.VOICE: 1,
        ConfirmationMode.UI: 2,
    }

    def __init__(
        self,
        registry: ToolRegistry,
        settings: PolicySettings,
    ) -> None:
        self._registry = registry
        self._settings = settings

    def update_settings(self, settings: PolicySettings) -> None:
        if settings.version <= self._settings.version:
            raise PolicyConfigurationError("新策略版本必须递增")
        self._settings = settings

    def evaluate(self, proposal: ToolProposal) -> PolicyDecision:
        fingerprint = self._fingerprint(proposal)
        manifest = self._registry.get(proposal.tool_name)
        if manifest is None:
            return self._denied(
                proposal,
                fingerprint,
                RiskLevel.R3,
                "未知工具已拒绝",
            )
        if manifest.name in self._settings.disabled_tools:
            return self._denied(
                proposal,
                fingerprint,
                manifest.base_risk,
                "工具已被本地策略禁用",
            )

        self._validate_arguments(manifest, proposal)
        try:
            dynamic_risk = (
                manifest.risk_evaluator(proposal.arguments)
                if manifest.risk_evaluator is not None
                else manifest.base_risk
            )
            if not isinstance(dynamic_risk, RiskLevel):
                raise TypeError("risk evaluator returned invalid type")
            risk = max(manifest.base_risk, dynamic_risk)
        except Exception as error:  # noqa: BLE001 本地扩展评估器属于不可信边界
            _ = error
            return self._denied(
                proposal,
                fingerprint,
                RiskLevel.R3,
                "本地风险评估失败",
            )

        try:
            summary = (
                manifest.impact_summarizer(proposal.arguments)
                if manifest.impact_summarizer is not None
                else manifest.description
            )
            if not isinstance(summary, str) or not summary.strip():
                raise TypeError("summary is invalid")
        except Exception as error:  # noqa: BLE001 本地摘要器属于不可信边界
            _ = error
            return self._denied(
                proposal,
                fingerprint,
                RiskLevel.R3,
                "本地影响摘要生成失败",
            )

        confirmation = self._confirmation_for(manifest.name, risk)
        return PolicyDecision(
            proposal.call_id,
            proposal.tool_name,
            True,
            risk,
            confirmation,
            summary.strip(),
            fingerprint,
            self._settings.version,
            "工具提议通过本地策略评估",
        )

    def _confirmation_for(
        self,
        tool_name: str,
        risk: RiskLevel,
    ) -> ConfirmationMode:
        if risk is RiskLevel.R3:
            floor = ConfirmationMode.UI
        elif risk is RiskLevel.R2:
            floor = self._settings.r2_confirmation
        elif risk is RiskLevel.R1 and self._settings.r1_requires_confirmation:
            floor = ConfirmationMode.VOICE
        else:
            floor = ConfirmationMode.NONE
        override = self._settings.tool_confirmation_overrides.get(
            tool_name,
            ConfirmationMode.NONE,
        )
        if self._CONFIRMATION_RANK[override] > self._CONFIRMATION_RANK[floor]:
            return override
        return floor

    def _denied(
        self,
        proposal: ToolProposal,
        fingerprint: str,
        risk: RiskLevel,
        reason: str,
    ) -> PolicyDecision:
        return PolicyDecision(
            proposal.call_id,
            proposal.tool_name,
            False,
            risk,
            ConfirmationMode.NONE,
            "工具调用已拒绝",
            fingerprint,
            self._settings.version,
            reason,
        )

    @staticmethod
    def _validate_arguments(
        manifest: ToolManifest,
        proposal: ToolProposal,
    ) -> None:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError

        try:
            Draft202012Validator(_thaw_json(manifest.input_schema)).validate(
                _thaw_json(proposal.arguments)
            )
        except ValidationError as error:
            raise ProposalValidationError(
                "工具参数不符合本地 JSON Schema"
            ) from error

    @staticmethod
    def _fingerprint(proposal: ToolProposal) -> str:
        return call_fingerprint(proposal)


def call_fingerprint(proposal: ToolProposal) -> str:
    """计算绑定调用标识、工具和全部参数的指纹"""

    payload = canonical_json(
        {
            "call_id": proposal.call_id,
            "tool_name": proposal.tool_name,
            "arguments": proposal.arguments,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    """成功消费授权令牌后得到的不可变授权"""

    nonce: str
    call_fingerprint: str
    policy_version: int
    risk: RiskLevel
    confirmation: ConfirmationMode
    expires_at: float


def _validate_authorization_key(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise PolicyConfigurationError("授权密钥至少需要 256 位")


def _encode_base64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_base64(value: str) -> bytes:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as error:
        raise AuthorizationError("授权令牌编码无效") from error
    if _encode_base64(decoded) != value:
        raise AuthorizationError("授权令牌编码不是规范格式")
    return decoded


class AuthorizationIssuer:
    """签发不包含原始工具参数的短时授权令牌"""

    _CONFIRMATION_RANK: ClassVar[dict[ConfirmationMode, int]] = {
        ConfirmationMode.NONE: 0,
        ConfirmationMode.VOICE: 1,
        ConfirmationMode.UI: 2,
    }

    def __init__(
        self,
        secret: bytes,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = 60.0,
    ) -> None:
        _validate_authorization_key(secret)
        if ttl_seconds <= 0:
            raise PolicyConfigurationError("授权有效期必须大于零")
        self._secret = secret
        self._clock = clock
        self._ttl_seconds = ttl_seconds

    def issue(
        self,
        decision: PolicyDecision,
        confirmation: ConfirmationMode,
    ) -> str:
        if not decision.requires_authorization:
            raise AuthorizationError("当前策略决策不需要授权令牌")
        if self._CONFIRMATION_RANK[confirmation] < self._CONFIRMATION_RANK[
            decision.confirmation
        ]:
            raise AuthorizationError("确认方式未达到策略要求")
        payload = canonical_json(
            {
                "v": 1,
                "nonce": secrets.token_urlsafe(24),
                "fp": decision.call_fingerprint,
                "pv": decision.policy_version,
                "risk": decision.risk.label,
                "confirmation": confirmation.value,
                "exp": self._clock() + self._ttl_seconds,
            }
        ).encode("utf-8")
        encoded_payload = _encode_base64(payload)
        signature = hmac.digest(
            self._secret,
            encoded_payload.encode("ascii"),
            "sha256",
        )
        return f"{encoded_payload}.{_encode_base64(signature)}"


class AuthorizationVerifier:
    """验签并一次性消费与工具调用绑定的授权令牌"""

    def __init__(
        self,
        secret: bytes,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_authorization_key(secret)
        self._secret = secret
        self._clock = clock
        self._consumed_nonces: set[str] = set()

    def consume(
        self,
        token: str,
        proposal: ToolProposal,
        decision: PolicyDecision,
    ) -> AuthorizationGrant:
        payload = self._verified_payload(token)
        nonce = payload.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise AuthorizationError("授权令牌缺少 nonce")
        if nonce in self._consumed_nonces:
            raise AuthorizationError("授权令牌已消费")
        try:
            return self._validate_payload(payload, proposal, decision)
        finally:
            self._consumed_nonces.add(nonce)

    def _verified_payload(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or token.count(".") != 1:
            raise AuthorizationError("授权令牌格式无效")
        encoded_payload, encoded_signature = token.split(".")
        signature = _decode_base64(encoded_signature)
        expected = hmac.digest(
            self._secret,
            encoded_payload.encode("ascii"),
            "sha256",
        )
        if not hmac.compare_digest(signature, expected):
            raise AuthorizationError("授权令牌签名无效")
        try:
            payload = json.loads(_decode_base64(encoded_payload))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AuthorizationError("授权令牌内容无效") from error
        if not isinstance(payload, dict):
            raise AuthorizationError("授权令牌内容必须是对象")
        return payload

    def _validate_payload(
        self,
        payload: dict[str, Any],
        proposal: ToolProposal,
        decision: PolicyDecision,
    ) -> AuthorizationGrant:
        if payload.get("v") != 1:
            raise AuthorizationError("授权令牌版本无效")
        expires_at = payload.get("exp")
        if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
            raise AuthorizationError("授权令牌到期时间无效")
        if self._clock() > expires_at:
            raise AuthorizationError("授权令牌已过期")
        fingerprint = payload.get("fp")
        expected_fingerprint = call_fingerprint(proposal)
        if (
            fingerprint != expected_fingerprint
            or fingerprint != decision.call_fingerprint
        ):
            raise AuthorizationError("授权令牌与工具调用绑定不匹配")
        if payload.get("pv") != decision.policy_version:
            raise AuthorizationError("授权令牌策略版本不匹配")
        if payload.get("risk") != decision.risk.label:
            raise AuthorizationError("授权令牌风险等级不匹配")
        try:
            confirmation = ConfirmationMode(payload.get("confirmation"))
        except (TypeError, ValueError) as error:
            raise AuthorizationError("授权令牌确认方式无效") from error
        if AuthorizationIssuer._CONFIRMATION_RANK[confirmation] < (
            AuthorizationIssuer._CONFIRMATION_RANK[decision.confirmation]
        ):
            raise AuthorizationError("授权令牌确认方式不足")
        nonce = payload["nonce"]
        assert isinstance(nonce, str)
        return AuthorizationGrant(
            nonce,
            fingerprint,
            decision.policy_version,
            decision.risk,
            confirmation,
            float(expires_at),
        )
