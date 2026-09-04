"""工具执行器共享的结果契约与本地目录"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from .cancellation import CancellationToken
from .policy import JsonValue, ToolManifest, ToolRegistry


class ToolError(RuntimeError):
    """工具执行层可报告的基础错误"""

    code = "tool.error"


class ToolConfigurationError(ToolError):
    """工具目录或执行结果配置无效"""

    code = "tool.configuration"


class ToolExecutionError(ToolError):
    """工具执行期间发生安全失败"""

    code = "tool.execution"


class ToolExecutionStatus(str, Enum):
    """跨进程使用的稳定工具执行状态"""

    SUCCESS = "success"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    FAILED = "failed"
    PARTIAL = "partial"


def _freeze_json(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ToolConfigurationError("工具结果 JSON 键必须是字符串")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolConfigurationError("工具结果 JSON 数字必须是有限值")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ToolConfigurationError(
        f"工具结果包含不支持的类型: {type(value).__name__}"
    )


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """不包含异常对象的不可变工具执行结果"""

    status: ToolExecutionStatus
    payload: Mapping[str, JsonValue]
    safe_message: str
    undo_data: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if not self.safe_message.strip():
            raise ToolConfigurationError("工具结果安全消息不能为空")
        payload = _freeze_json(self.payload)
        if not isinstance(payload, Mapping):
            raise ToolConfigurationError("工具结果 payload 必须是对象")
        undo_data = (
            None if self.undo_data is None else _freeze_json(self.undo_data)
        )
        if undo_data is not None and not isinstance(undo_data, Mapping):
            raise ToolConfigurationError("工具撤销数据必须是对象")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "undo_data", undo_data)


class ToolHandler(Protocol):
    """Tool Worker 可调用的异步工具边界"""

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        token: CancellationToken,
    ) -> ToolExecutionResult: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """把安全声明和固定 Python handler 绑定为一项工具"""

    manifest: ToolManifest
    handler: ToolHandler

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "execute", None)):
            raise ToolConfigurationError("工具 handler 必须实现 execute")


class ToolCatalog:
    """按精确名称保存可执行工具且拒绝重复注册"""

    def __init__(self, tools: Sequence[RegisteredTool]) -> None:
        registered: dict[str, RegisteredTool] = {}
        for tool in tools:
            if tool.manifest.name in registered:
                raise ToolConfigurationError(
                    f"工具名称重复: {tool.manifest.name}"
                )
            registered[tool.manifest.name] = tool
        self._tools = MappingProxyType(registered)
        self._policy_registry = ToolRegistry(
            tuple(tool.manifest for tool in registered.values())
        )

    @property
    def policy_registry(self) -> ToolRegistry:
        return self._policy_registry

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)
