"""Tool Worker 使用的严格有界 JSON-RPC 编解码"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .policy import JsonValue

DEFAULT_MAX_LINE_BYTES = 1024 * 1024


class RpcProtocolError(RuntimeError):
    """RPC 消息编码或结构不符合本地协议"""

    code = "rpc.protocol"


def _freeze_json(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RpcProtocolError("RPC JSON 键必须是字符串")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise RpcProtocolError("RPC JSON 数字必须是有限值")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise RpcProtocolError("RPC JSON 包含不支持的值")


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_request_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None
    ):
        raise RpcProtocolError("RPC request ID 格式无效")


@dataclass(frozen=True, slots=True)
class RpcError:
    """不携带原始请求内容的 JSON-RPC 错误"""

    code: int
    message: str

    def __post_init__(self) -> None:
        if isinstance(self.code, bool) or not isinstance(self.code, int):
            raise RpcProtocolError("RPC 错误码必须是整数")
        if not isinstance(self.message, str) or not self.message.strip():
            raise RpcProtocolError("RPC 错误消息不能为空")
        if len(self.message) > 256:
            raise RpcProtocolError("RPC 错误消息过长")


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """字段精确且参数不可变的 JSON-RPC 请求"""

    request_id: str
    method: str
    params: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_request_id(self.request_id)
        if self.method not in {"execute", "cancel", "ping"}:
            raise RpcProtocolError("RPC method 不受支持")
        frozen = _freeze_json(self.params)
        if not isinstance(frozen, Mapping):
            raise RpcProtocolError("RPC params 必须是对象")
        self._validate_params(frozen)
        object.__setattr__(self, "params", frozen)

    def _validate_params(self, params: Mapping[str, JsonValue]) -> None:
        if self.method == "ping":
            if params:
                raise RpcProtocolError("ping params 必须为空")
            return
        if self.method == "cancel":
            if set(params) != {"request_id"}:
                raise RpcProtocolError("cancel params 字段无效")
            request_id = params.get("request_id")
            if not isinstance(request_id, str):
                raise RpcProtocolError("cancel request ID 无效")
            _validate_request_id(request_id)
            return
        if set(params) != {
            "proposal",
            "decision",
            "authorization",
            "context",
        }:
            raise RpcProtocolError("execute params 字段无效")
        if not isinstance(params.get("proposal"), Mapping):
            raise RpcProtocolError("execute proposal 必须是对象")
        if not isinstance(params.get("decision"), Mapping):
            raise RpcProtocolError("execute decision 必须是对象")
        context = params.get("context")
        if not isinstance(context, Mapping) or set(context) != {
            "turn_id",
            "correlation_id",
            "tool_call_id",
        }:
            raise RpcProtocolError("execute context 必须是精确对象")
        authorization = params.get("authorization")
        if authorization is not None and (
            not isinstance(authorization, str) or len(authorization) > 4096
        ):
            raise RpcProtocolError("execute authorization 无效")


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """只包含 result 或 error 其中之一的 JSON-RPC 响应"""

    request_id: str
    result: Mapping[str, JsonValue] | None = None
    error: RpcError | None = None

    def __post_init__(self) -> None:
        _validate_request_id(self.request_id)
        if (self.result is None) == (self.error is None):
            raise RpcProtocolError("RPC 响应必须只包含 result 或 error")
        if self.result is not None:
            frozen = _freeze_json(self.result)
            if not isinstance(frozen, Mapping):
                raise RpcProtocolError("RPC result 必须是对象")
            object.__setattr__(self, "result", frozen)


def encode_request(
    request: RpcRequest,
    *,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> bytes:
    """把请求编码为单行规范 JSON"""

    return _encode_message(
        {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "method": request.method,
            "params": request.params,
        },
        max_line_bytes,
    )


def encode_response(
    response: RpcResponse,
    *,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> bytes:
    """把响应编码为单行规范 JSON"""

    message: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": response.request_id,
    }
    if response.result is not None:
        message["result"] = response.result
    else:
        assert response.error is not None
        message["error"] = {
            "code": response.error.code,
            "message": response.error.message,
        }
    return _encode_message(message, max_line_bytes)


def _encode_message(message: Mapping[str, Any], max_line_bytes: int) -> bytes:
    try:
        encoded = json.dumps(
            _thaw_json(_freeze_json(message)),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as error:
        raise RpcProtocolError("RPC 消息无法编码") from error
    if max_line_bytes <= 0 or len(encoded) > max_line_bytes:
        raise RpcProtocolError("RPC 消息超过大小上限")
    return encoded


def decode_request(
    line: bytes,
    *,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> RpcRequest:
    """严格解码一行 JSON-RPC 请求"""

    message = _decode_message(line, max_line_bytes)
    if set(message) != {"jsonrpc", "id", "method", "params"}:
        raise RpcProtocolError("RPC 请求字段无效")
    if message.get("jsonrpc") != "2.0":
        raise RpcProtocolError("RPC 协议版本无效")
    return RpcRequest(
        message.get("id"),
        message.get("method"),
        message.get("params"),
    )


def decode_response(
    line: bytes,
    *,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> RpcResponse:
    """严格解码一行 JSON-RPC 响应"""

    message = _decode_message(line, max_line_bytes)
    if message.get("jsonrpc") != "2.0":
        raise RpcProtocolError("RPC 协议版本无效")
    keys = set(message)
    if keys == {"jsonrpc", "id", "result"}:
        return RpcResponse(message.get("id"), result=message.get("result"))
    if keys == {"jsonrpc", "id", "error"}:
        error = message.get("error")
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise RpcProtocolError("RPC error 字段无效")
        return RpcResponse(
            message.get("id"),
            error=RpcError(error.get("code"), error.get("message")),
        )
    raise RpcProtocolError("RPC 响应字段无效")


def _decode_message(line: bytes, max_line_bytes: int) -> dict[str, Any]:
    if not isinstance(line, bytes) or max_line_bytes <= 0 or len(line) > max_line_bytes:
        raise RpcProtocolError("RPC 消息超过大小上限")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RpcProtocolError("RPC 消息不是有效 UTF-8") from error

    def reject_constant(value: str) -> None:
        raise ValueError(value)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        message = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise RpcProtocolError("RPC JSON 无效") from error
    if not isinstance(message, dict):
        raise RpcProtocolError("RPC 消息顶层必须是对象")
    return message
