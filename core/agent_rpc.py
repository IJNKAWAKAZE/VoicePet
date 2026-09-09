"""Agent Worker 使用的严格有界 JSON-RPC 编解码"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .agent_types import (
    AgentEvent,
    AgentTurnRequest,
    freeze_json,
    thaw_json,
    validate_identifier,
)

DEFAULT_MAX_LINE_BYTES = 1024 * 1024


class AgentProtocolError(ValueError):
    """协议失败只提供安全错误，不附带原始载荷"""

    code = "agent.protocol"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AgentProtocolError("Agent JSON 字段重复")
        result[key] = value
    return result


def _constant(_value):
    raise AgentProtocolError("Agent JSON 数值无效")


def _params(method, params):
    if not isinstance(params, Mapping):
        raise AgentProtocolError("Agent 参数必须为对象")
    if method == "agent.turn.start":
        AgentTurnRequest.from_mapping(params)
    elif method == "agent.event":
        AgentEvent.from_mapping(params)
    else:
        fields = {
            "agent.initialize": {"protocol_version"},
            "agent.turn.cancel": {"turn_id", "reason"},
            "agent.approval.resolve": {"approval_id", "decision"},
            "agent.shutdown": set(),
        }
        if method not in fields or set(params) != fields[method]:
            raise AgentProtocolError("Agent 方法或参数字段无效")
        if method == "agent.initialize" and (type(params["protocol_version"]) is not int or params["protocol_version"] != 1):
            raise AgentProtocolError("Agent 协议版本不受支持")
        for name in ("approval_id", "session_id", "turn_id"):
            if name in params:
                validate_identifier(params[name])
        if method == "agent.turn.cancel" and params["reason"] not in ("user_cancelled", "shutdown", "timeout"):
            raise AgentProtocolError("Agent 取消原因无效")
        if method == "agent.approval.resolve" and params["decision"] not in ("accept", "decline", "cancel"):
            raise AgentProtocolError("Agent 审批决定无效")
    return freeze_json(params)


@dataclass(frozen=True, slots=True)
class AgentRpcRequest:
    request_id: str
    method: str
    params: Mapping[str, Any] = field(repr=False)

    def __post_init__(self):
        validate_identifier(self.request_id)
        if not isinstance(self.method, str) or self.method == "agent.event":
            raise AgentProtocolError("Agent 请求方法无效")
        object.__setattr__(self, "params", _params(self.method, self.params))


@dataclass(frozen=True, slots=True)
class AgentRpcResponse:
    request_id: str
    result: Any = field(default=None, repr=False)
    error: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self):
        validate_identifier(self.request_id)
        if self.error is not None:
            if self.result is not None or not isinstance(self.error, Mapping) or set(self.error) != {"code", "message"}:
                raise AgentProtocolError("Agent 响应错误字段无效")
            if type(self.error["code"]) is not int or not isinstance(self.error["message"], str) or len(self.error["message"]) > 256:
                raise AgentProtocolError("Agent 响应错误内容无效")
            object.__setattr__(self, "error", freeze_json(self.error))
        object.__setattr__(self, "result", freeze_json(self.result))


@dataclass(frozen=True, slots=True)
class AgentRpcNotification:
    method: str
    params: Mapping[str, Any] = field(repr=False)

    def __post_init__(self):
        if self.method != "agent.event":
            raise AgentProtocolError("Agent 通知方法无效")
        object.__setattr__(self, "params", _params(self.method, self.params))


def encode_message(message, *, max_line_bytes=DEFAULT_MAX_LINE_BYTES) -> bytes:
    data = {"jsonrpc": "2.0"}
    if isinstance(message, AgentRpcRequest):
        data.update(id=message.request_id, method=message.method, params=thaw_json(message.params))
    elif isinstance(message, AgentRpcResponse):
        data["id"] = message.request_id
        data.update({"error": thaw_json(message.error)} if message.error is not None else {"result": thaw_json(message.result)})
    elif isinstance(message, AgentRpcNotification):
        data.update(method=message.method, params=thaw_json(message.params))
    else:
        raise AgentProtocolError("Agent 消息类型无效")
    encoded = (json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > max_line_bytes:
        raise AgentProtocolError("Agent 消息过大")
    return encoded


def decode_message(line, *, max_line_bytes=DEFAULT_MAX_LINE_BYTES):
    try:
        raw = line.encode("utf-8") if isinstance(line, str) else line
        if not isinstance(raw, bytes) or len(raw) > max_line_bytes:
            raise AgentProtocolError("Agent 消息过大或类型无效")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
            raise AgentProtocolError("Agent 信封无效")
        fields = set(data)
        if fields == {"jsonrpc", "id", "method", "params"}:
            return AgentRpcRequest(data["id"], data["method"], data["params"])
        if fields == {"jsonrpc", "method", "params"}:
            return AgentRpcNotification(data["method"], data["params"])
        if fields == {"jsonrpc", "id", "result"}:
            return AgentRpcResponse(data["id"], result=data["result"])
        if fields == {"jsonrpc", "id", "error"}:
            return AgentRpcResponse(data["id"], error=data["error"])
        raise AgentProtocolError("Agent 信封字段无效")
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
        raise AgentProtocolError("Agent 协议数据无效") from None
