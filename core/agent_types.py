"""Codex Agent 使用的严格且不可变的领域类型"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

MAX_PAYLOAD_BYTES = 900_000
MAX_ATTACHMENTS = 16


class AgentApprovalMode(str, Enum):
    SUGGEST = "suggest"
    AUTO_EDIT = "auto_edit"
    FULL_AUTO = "full_auto"


class AgentEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    COMMAND_STARTED = "command_started"
    COMMAND_OUTPUT_DELTA = "command_output_delta"
    COMMAND_COMPLETED = "command_completed"
    FILE_CHANGE = "file_change"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    PLAN_UPDATED = "plan_updated"
    USAGE_UPDATED = "usage_updated"
    APPROVAL_REQUEST = "approval_request"
    TURN_COMPLETED = "turn_completed"
    CANCELLED = "cancelled"
    ERROR = "error"


TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed", "outcome_unknown"})


def validate_identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise ValueError("Agent 标识无效")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def freeze_json(value: Any, *, limit: int = MAX_PAYLOAD_BYTES) -> Any:
    """复制并递归冻结有界 JSON，避免启动后的调用方修改快照"""
    remaining = limit

    def copy(item: Any, depth: int = 0) -> Any:
        nonlocal remaining
        remaining -= 4
        if remaining < 0 or depth > 24:
            raise ValueError("Agent JSON 大小或深度超限")
        if isinstance(item, Mapping):
            result = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("Agent JSON 字段无效")
                copy(key, depth + 1)
                result[key] = copy(child, depth + 1)
            return MappingProxyType(result)
        if isinstance(item, (list, tuple)):
            return tuple(copy(child, depth + 1) for child in item)
        if type(item) is str:
            try:
                remaining -= len(item.encode("utf-8"))
            except UnicodeError:
                raise ValueError("Agent JSON 文本无效") from None
            if remaining < 0:
                raise ValueError("Agent JSON 大小超限")
            return item
        if item is None or type(item) is bool:
            return item
        if type(item) is int and -(2**63) <= item <= 2**63 - 1:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        raise ValueError("Agent JSON 值无效")

    frozen = copy(value)
    if len(json.dumps(thaw_json(frozen), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > limit:
        raise ValueError("Agent JSON 大小超限")
    return frozen


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    protocol_version: int
    sdk_version: str
    runtime_version: str
    supports_approvals: bool
    supports_full_access: bool

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise ValueError("Agent 协议版本不受支持")
        validate_identifier(self.sdk_version)
        validate_identifier(self.runtime_version)
        if any(type(value) is not bool for value in (self.supports_approvals, self.supports_full_access)):
            raise ValueError("Agent 能力字段无效")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AgentCapabilities:
        if not isinstance(data, Mapping) or set(data) != {"protocol_version", "sdk_version", "runtime_version", "supports_approvals", "supports_full_access"}:
            raise ValueError("Agent 能力字段无效")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class AgentTurnRequest:
    session_id: str
    thread_id: str | None
    turn_id: str
    input: str = field(repr=False)
    approval_mode: AgentApprovalMode
    attachments: tuple[Mapping[str, str], ...] = field(default=(), repr=False)
    context: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        for value in (self.session_id, self.turn_id):
            validate_identifier(value)
        if self.thread_id is not None:
            validate_identifier(self.thread_id)
        if not isinstance(self.input, str) or len(self.input) > 200_000:
            raise ValueError("输入过长或类型无效")
        if not isinstance(self.context, str) or len(self.context) > 8000:
            raise ValueError("Agent 上下文无效")
        if not isinstance(self.approval_mode, AgentApprovalMode):
            raise ValueError("Agent 审批模式无效")  # noqa: TRY004
        if not isinstance(self.attachments, (list, tuple)) or len(self.attachments) > MAX_ATTACHMENTS:
            raise ValueError("Agent 附件数量无效")
        for item in self.attachments:
            if not isinstance(item, Mapping) or set(item) != {"name", "path", "media_type", "kind"}:
                raise ValueError("Agent 附件字段无效")
            if any(not isinstance(value, str) or not value or len(value) > (32767 if key == "path" else 256) for key, value in item.items()):
                raise ValueError("Agent 附件内容无效")
            if item["kind"] not in {"image", "file", "text"}:
                raise ValueError("Agent 附件类型无效")
        object.__setattr__(self, "attachments", freeze_json(self.attachments, limit=64_000))
        freeze_json(self.to_mapping())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AgentTurnRequest:
        required = {"session_id", "thread_id", "turn_id", "input", "approval_mode"}
        if not isinstance(data, Mapping) or not required <= set(data) or set(data) - required - {"attachments", "context"}:
            raise ValueError("Agent 轮次字段无效")
        try:
            mode = AgentApprovalMode(data["approval_mode"])
        except (ValueError, TypeError):
            raise ValueError("Agent 审批模式无效") from None
        return cls(**{**data, "approval_mode": mode})

    def to_mapping(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "thread_id": self.thread_id, "turn_id": self.turn_id, "input": self.input, "approval_mode": self.approval_mode.value, "attachments": thaw_json(self.attachments), "context": self.context}


@dataclass(frozen=True, slots=True)
class AgentEvent:
    session_id: str
    turn_id: str
    seq: int
    type: AgentEventType
    payload: Mapping[str, Any] = field(repr=False)
    thread_id: str | None = None
    codex_turn_id: str | None = None
    item_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        for value in (self.session_id, self.turn_id):
            validate_identifier(value)
        for value in (self.thread_id, self.codex_turn_id, self.item_id):
            if value is not None:
                validate_identifier(value)
        if type(self.seq) is not int or not 0 <= self.seq <= 2**63 - 1:
            raise ValueError("事件序号无效")
        if not isinstance(self.type, AgentEventType) or not isinstance(self.payload, Mapping):
            raise ValueError("Agent 事件无效")  # noqa: TRY004
        try:
            if len(self.timestamp) > 64 or datetime.fromisoformat(self.timestamp).tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Agent 事件时间无效") from None
        object.__setattr__(self, "payload", freeze_json(self.payload))
        if self.type is AgentEventType.TURN_COMPLETED and self.payload.get("status") not in TERMINAL_STATUSES:
            raise ValueError("Agent 终态无效")
        if self.type in {AgentEventType.TEXT_DELTA, AgentEventType.COMMAND_OUTPUT_DELTA} and not isinstance(self.payload.get("text"), str):
            raise ValueError("Agent 文本事件无效")
        if self.type is AgentEventType.FILE_CHANGE and self.payload.get("status") not in {"proposed", "completed", "failed", "declined"}:
            raise ValueError("Agent 文件变更状态无效")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AgentEvent:
        required = {"session_id", "turn_id", "seq", "type", "payload"}
        optional = {"thread_id", "codex_turn_id", "item_id", "timestamp"}
        if not isinstance(data, Mapping) or not required <= set(data) or set(data) - required - optional:
            raise ValueError("Agent 事件字段无效")
        try:
            kind = AgentEventType(data["type"])
        except (ValueError, TypeError):
            raise ValueError("Agent 事件类型无效") from None
        return cls(**{**data, "type": kind})

    def to_mapping(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "turn_id": self.turn_id, "seq": self.seq, "type": self.type.value, "payload": thaw_json(self.payload), "thread_id": self.thread_id, "codex_turn_id": self.codex_turn_id, "item_id": self.item_id, "timestamp": self.timestamp}


@dataclass(frozen=True, slots=True)
class AgentTerminalResult:
    turn_id: str
    status: str
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        validate_identifier(self.turn_id)
        if not isinstance(self.status, str) or self.status not in TERMINAL_STATUSES:
            raise ValueError("Agent 终态无效")
        if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (self.input_tokens, self.output_tokens)):
            raise ValueError("Agent 使用量无效")


