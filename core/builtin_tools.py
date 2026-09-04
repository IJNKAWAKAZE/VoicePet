"""经过本地白名单和资源范围约束的专用工具"""

from __future__ import annotations

import asyncio
import platform
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from uuid import uuid4

from .audit import AuditError, AuditStore, AuditUndoState
from .cancellation import CancellationToken, CancelledError
from .path_security import PathSecurityError, ScopedPathResolver
from .policy import (
    ConcurrencyPolicy,
    JsonValue,
    RiskLevel,
    ToolManifest,
)
from .tool_types import ToolExecutionResult, ToolExecutionStatus


class ApplicationLauncher(Protocol):
    """白名单应用启动的最小系统边界"""

    async def launch(
        self,
        argv: tuple[str, ...],
        token: CancellationToken,
    ) -> int: ...


class SubprocessApplicationLauncher:
    """使用 exec 参数列表启动应用且不经过 shell"""

    async def launch(
        self,
        argv: tuple[str, ...],
        token: CancellationToken,
    ) -> int:
        token.throw_if_cancelled()
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.pid is not None
        return process.pid


class SystemInfoTool:
    """返回不含主机名和设备序列号的基础系统信息"""

    def __init__(self, *, info_provider: Callable[[], Mapping[str, str]] | None = None) -> None:
        self._info_provider = info_provider or self._read_system_info
        self.manifest = ToolManifest(
            name="system_info",
            description="读取基础系统版本和运行时信息",
            input_schema={
                "type": "object",
                "maxProperties": 1,
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            base_risk=RiskLevel.R0,
            timeout=2.0,
            concurrency_policy=ConcurrencyPolicy.PARALLEL,
        )

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        token: CancellationToken,
    ) -> ToolExecutionResult:
        token.throw_if_cancelled()
        if arguments:
            return ToolExecutionResult(
                ToolExecutionStatus.DENIED,
                {},
                "系统信息参数无效",
            )
        try:
            payload = dict(self._info_provider())
        except Exception as error:  # noqa: BLE001 系统信息提供器属于外部边界
            return ToolExecutionResult(
                ToolExecutionStatus.FAILED,
                {"error_type": type(error).__name__},
                "无法读取系统信息",
            )
        token.throw_if_cancelled()
        return ToolExecutionResult(
            ToolExecutionStatus.SUCCESS,
            payload,
            "已读取基础系统信息",
        )

    @staticmethod
    def _read_system_info() -> dict[str, str]:
        return {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        }


class OpenAppTool:
    """只启动本地配置中精确匹配的应用 argv"""

    def __init__(
        self,
        applications: Mapping[str, Sequence[str]],
        *,
        launcher: ApplicationLauncher | None = None,
    ) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for name, argv in applications.items():
            values = tuple(argv)
            if not name or not values or any(not isinstance(item, str) for item in values):
                raise ValueError("应用白名单配置无效")
            program = Path(values[0])
            if not program.is_absolute() or not program.is_file():
                raise ValueError("应用白名单程序必须是存在的绝对文件")
            normalized[name] = values
        self._applications = MappingProxyType(normalized)
        self._launcher = launcher or SubprocessApplicationLauncher()
        self.manifest = ToolManifest(
            name="open_app",
            description="打开本地白名单中的应用",
            input_schema={
                "type": "object",
                "maxProperties": 1,
                "properties": {
                    "name": {"type": "string", "maxLength": 64},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            required_permissions=frozenset({"process.launch"}),
            base_risk=RiskLevel.R1,
            timeout=5.0,
            concurrency_policy=ConcurrencyPolicy.PER_TOOL,
            supports_cancel=True,
            risk_evaluator=self._risk,
            impact_summarizer=self._summary,
        )

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        token: CancellationToken,
    ) -> ToolExecutionResult:
        token.throw_if_cancelled()
        name = arguments.get("name")
        if not isinstance(name, str) or name not in self._applications:
            return ToolExecutionResult(
                ToolExecutionStatus.DENIED,
                {},
                "应用不在本地白名单中",
            )
        try:
            pid = await self._launcher.launch(self._applications[name], token)
        except CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 应用启动器属于操作系统边界
            return ToolExecutionResult(
                ToolExecutionStatus.FAILED,
                {"error_type": type(error).__name__},
                "应用启动失败",
            )
        status = (
            ToolExecutionStatus.PARTIAL
            if token.is_cancelled
            else ToolExecutionStatus.SUCCESS
        )
        return ToolExecutionResult(
            status,
            {"application": name, "pid": pid},
            "应用启动请求已提交",
        )

    def _risk(self, arguments: Mapping[str, JsonValue]) -> RiskLevel:
        if arguments.get("name") not in self._applications:
            raise ValueError("application is not allowed")
        return RiskLevel.R1

    def _summary(self, arguments: Mapping[str, JsonValue]) -> str:
        name = arguments.get("name")
        if not isinstance(name, str) or name not in self._applications:
            raise ValueError("application is not allowed")
        return f"打开应用 {name}"


class MoveFileTool:
    """在允许根目录内移动普通文件且默认禁止覆盖"""

    def __init__(
        self,
        resolver: ScopedPathResolver,
        *,
        move_function: Callable[[str, str], Any] = shutil.move,
    ) -> None:
        self._resolver = resolver
        self._move_function = move_function
        path_schema = {"type": "string", "maxLength": 32767}
        self.manifest = ToolManifest(
            name="move_file",
            description="在允许目录内移动一个文件",
            input_schema={
                "type": "object",
                "maxProperties": 2,
                "properties": {
                    "source": path_schema,
                    "destination": path_schema,
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
            required_permissions=frozenset({"filesystem.move"}),
            base_risk=RiskLevel.R2,
            timeout=30.0,
            concurrency_policy=ConcurrencyPolicy.SERIAL,
            supports_cancel=True,
            supports_undo=True,
            sensitive_fields=("source", "destination"),
            risk_evaluator=self._risk,
            impact_summarizer=self._summary,
        )

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        token: CancellationToken,
    ) -> ToolExecutionResult:
        token.throw_if_cancelled()
        try:
            source, destination = self._resolve_arguments(arguments)
        except PathSecurityError:
            return ToolExecutionResult(
                ToolExecutionStatus.DENIED,
                {},
                "文件路径超出允许范围或目标已存在",
            )
        try:
            await asyncio.to_thread(
                self._move_function,
                str(source),
                str(destination),
            )
        except OSError as error:
            return ToolExecutionResult(
                ToolExecutionStatus.FAILED,
                {"error_type": type(error).__name__},
                "文件移动失败",
            )
        status = (
            ToolExecutionStatus.PARTIAL
            if token.is_cancelled
            else ToolExecutionStatus.SUCCESS
        )
        return ToolExecutionResult(
            status,
            {"moved": True},
            "文件移动已完成",
            {
                "source": str(source),
                "destination": str(destination),
            },
        )

    def _resolve_arguments(
        self,
        arguments: Mapping[str, JsonValue],
    ) -> tuple[Path, Path]:
        source = arguments.get("source")
        destination = arguments.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise PathSecurityError("文件移动参数无效")
        return (
            self._resolver.existing_file(source),
            self._resolver.new_file(destination),
        )

    def _risk(self, arguments: Mapping[str, JsonValue]) -> RiskLevel:
        self._resolve_arguments(arguments)
        return RiskLevel.R2

    def _summary(self, arguments: Mapping[str, JsonValue]) -> str:
        source, destination = self._resolve_arguments(arguments)
        return f"将文件 {source.name} 移动为 {destination.name}"


class MoveFileUndoTool:
    """按原审计撤销一次文件移动并防止重复消费"""

    def __init__(
        self,
        audit_store: AuditStore,
        resolver: ScopedPathResolver,
        *,
        move_function: Callable[[str, str], Any] = shutil.move,
    ) -> None:
        self._audit_store = audit_store
        self._resolver = resolver
        self._move_function = move_function
        self.manifest = ToolManifest(
            name="undo_move_file",
            description="撤销一条已审计的文件移动",
            input_schema={
                "type": "object",
                "maxProperties": 1,
                "properties": {
                    "audit_id": {"type": "string", "maxLength": 36},
                },
                "required": ["audit_id"],
                "additionalProperties": False,
            },
            required_permissions=frozenset({"filesystem.move"}),
            base_risk=RiskLevel.R2,
            timeout=30.0,
            concurrency_policy=ConcurrencyPolicy.SERIAL,
            supports_cancel=True,
            supports_undo=True,
            risk_evaluator=self._risk,
            impact_summarizer=self._summary,
        )

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        token: CancellationToken,
    ) -> ToolExecutionResult:
        token.throw_if_cancelled()
        audit_id = arguments.get("audit_id")
        if not isinstance(audit_id, str):
            return self._denied()
        try:
            _, current, restored = self._prepare(audit_id)
        except (AuditError, PathSecurityError, ValueError):
            return self._denied()
        operation_id = str(uuid4())
        try:
            reserved = self._audit_store.reserve_undo(audit_id, operation_id)
        except AuditError:
            return self._denied()
        if not reserved:
            return self._denied()
        try:
            await asyncio.to_thread(
                self._move_function,
                str(current),
                str(restored),
            )
        except OSError as error:
            _ = error
            self._audit_store.release_undo(audit_id, operation_id)
            return ToolExecutionResult(
                ToolExecutionStatus.FAILED,
                {},
                "文件移动撤销失败",
            )
        try:
            completed = self._audit_store.complete_undo(audit_id, operation_id)
        except AuditError:
            completed = False
        status = (
            ToolExecutionStatus.SUCCESS
            if completed and not token.is_cancelled
            else ToolExecutionStatus.PARTIAL
        )
        return ToolExecutionResult(
            status,
            {
                "undone": completed,
                "original_audit_id": audit_id,
                "operation_id": operation_id,
            },
            "文件移动撤销已执行",
            {
                "source": str(current),
                "destination": str(restored),
            },
        )

    def _prepare(self, audit_id: str):
        record = self._audit_store.get(audit_id)
        if (
            record is None
            or record.tool_name != "move_file"
            or record.undo_state is not AuditUndoState.AVAILABLE
            or record.undo_data is None
        ):
            raise ValueError("audit record is not undoable")
        source = record.undo_data.get("source")
        destination = record.undo_data.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise TypeError("undo data is invalid")
        return (
            record,
            self._resolver.existing_file(destination),
            self._resolver.new_file(source),
        )

    def _risk(self, arguments: Mapping[str, JsonValue]) -> RiskLevel:
        audit_id = arguments.get("audit_id")
        if not isinstance(audit_id, str):
            raise TypeError("audit ID is invalid")
        self._prepare(audit_id)
        return RiskLevel.R2

    def _summary(self, arguments: Mapping[str, JsonValue]) -> str:
        audit_id = arguments.get("audit_id")
        if not isinstance(audit_id, str):
            raise TypeError("audit ID is invalid")
        _, current, restored = self._prepare(audit_id)
        return f"将文件 {current.name} 撤销移动为 {restored.name}"

    @staticmethod
    def _denied() -> ToolExecutionResult:
        return ToolExecutionResult(
            ToolExecutionStatus.DENIED,
            {},
            "该文件移动当前不可撤销",
        )
