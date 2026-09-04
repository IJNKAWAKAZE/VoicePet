"""工具操作的脱敏 SQLite 审计与撤销状态管理"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from .policy import (
    ConfirmationMode,
    JsonValue,
    PolicyDecision,
    RiskLevel,
    ToolManifest,
    canonical_json,
)
from .tool_types import ToolExecutionResult, ToolExecutionStatus


class AuditError(RuntimeError):
    """审计存储可报告的基础错误"""

    code = "audit.error"


class AuditConfigurationError(AuditError):
    """审计记录、数据库路径或生命周期无效"""

    code = "audit.configuration"


class AuditStorageError(AuditError):
    """SQLite 审计读写失败"""

    code = "audit.storage"


class AuditUndoState(str, Enum):
    """审计记录关联撤销数据的生命周期"""

    NONE = "none"
    AVAILABLE = "available"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


def _freeze_json(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise AuditConfigurationError("审计 JSON 包含不支持的值")


@dataclass(frozen=True, slots=True)
class AuditContext:
    """一次工具调用关联的 Runtime 标识"""

    turn_id: str
    correlation_id: str
    tool_call_id: str

    def __post_init__(self) -> None:
        try:
            UUID(self.turn_id)
            UUID(self.correlation_id)
        except (ValueError, AttributeError) as error:
            raise AuditConfigurationError("审计 Runtime 标识必须是 UUID") from error
        if not self.tool_call_id.strip() or len(self.tool_call_id) > 128:
            raise AuditConfigurationError("审计工具调用 ID 无效")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """从 SQLite 读取的不可变脱敏审计记录"""

    audit_id: str
    occurred_at_utc: datetime
    turn_id: str
    correlation_id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue]
    risk: RiskLevel
    confirmation: ConfirmationMode
    policy_version: int
    status: ToolExecutionStatus
    safe_message: str
    duration_ms: int
    undo_data: Mapping[str, JsonValue] | None
    undo_state: AuditUndoState
    undone_by: str | None

    def __post_init__(self) -> None:
        frozen_arguments = _freeze_json(self.arguments)
        if not isinstance(frozen_arguments, Mapping):
            raise AuditConfigurationError("审计参数必须是对象")
        frozen_undo = (
            None if self.undo_data is None else _freeze_json(self.undo_data)
        )
        if frozen_undo is not None and not isinstance(frozen_undo, Mapping):
            raise AuditConfigurationError("审计撤销数据必须是对象")
        object.__setattr__(self, "arguments", frozen_arguments)
        object.__setattr__(self, "undo_data", frozen_undo)


class AuditStore:
    """串行化 SQLite 审计写入并提供原子撤销预留"""

    _SCHEMA_VERSION = 1

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path)
        if not path.parent.exists():
            raise AuditConfigurationError("审计数据库父目录不存在")
        try:
            connection = sqlite3.connect(
                path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as error:
            raise AuditStorageError("无法打开审计数据库") from error
        connection.row_factory = sqlite3.Row
        self._connection = connection
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._configure()
            self._migrate()
        except sqlite3.Error as error:
            connection.close()
            self._closed = True
            raise AuditStorageError("审计数据库初始化失败") from error

    def diagnostics(self) -> dict[str, JsonValue]:
        with self._lock:
            self._ensure_open()
            journal_mode = self._connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            foreign_keys = self._connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()[0]
            busy_timeout = self._connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0]
            schema_version = self._connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            return {
                "schema_version": schema_version,
                "journal_mode": str(journal_mode).lower(),
                "foreign_keys": bool(foreign_keys),
                "busy_timeout_ms": busy_timeout,
            }

    def write(
        self,
        *,
        context: AuditContext,
        manifest: ToolManifest,
        arguments: Mapping[str, JsonValue],
        decision: PolicyDecision,
        result: ToolExecutionResult,
        duration_ms: int,
        occurred_at_utc: datetime | None = None,
        audit_id: str | None = None,
    ) -> AuditRecord:
        self._validate_write(context, manifest, decision, duration_ms)
        audit_id = audit_id or str(uuid4())
        self._validate_uuid(audit_id, "审计 ID")
        occurred = occurred_at_utc or datetime.now(UTC)
        if occurred.tzinfo is None:
            raise AuditConfigurationError("审计时间必须包含时区")
        occurred = occurred.astimezone(UTC)
        redacted = self._redact_arguments(arguments, manifest.sensitive_fields)
        arguments_json = canonical_json(redacted)
        undo_data_json = None
        undo_state = AuditUndoState.NONE
        if (
            result.undo_data is not None
            and result.status
            in {ToolExecutionStatus.SUCCESS, ToolExecutionStatus.PARTIAL}
        ):
            undo_data_json = canonical_json(result.undo_data)
            undo_state = AuditUndoState.AVAILABLE
        safe_message = result.safe_message[:512]
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute(
                    """
                    INSERT INTO audit_records (
                        audit_id, occurred_at_utc, turn_id, correlation_id,
                        tool_call_id, tool_name, arguments_json, risk,
                        confirmation, policy_version, status, safe_message,
                        duration_ms, undo_data_json, undo_state, undone_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        audit_id,
                        occurred.isoformat(),
                        context.turn_id,
                        context.correlation_id,
                        context.tool_call_id,
                        manifest.name,
                        arguments_json,
                        decision.risk.label,
                        decision.confirmation.value,
                        decision.policy_version,
                        result.status.value,
                        safe_message,
                        duration_ms,
                        undo_data_json,
                        undo_state.value,
                    ),
                )
            except sqlite3.Error as error:
                raise AuditStorageError("审计记录写入失败") from error
            record = self.get(audit_id)
            assert record is not None
            return record

    def get(self, audit_id: str) -> AuditRecord | None:
        self._validate_uuid(audit_id, "审计 ID")
        with self._lock:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    "SELECT * FROM audit_records WHERE audit_id = ?",
                    (audit_id,),
                ).fetchone()
            except sqlite3.Error as error:
                raise AuditStorageError("审计记录读取失败") from error
            return None if row is None else self._record_from_row(row)

    def list_recent(self, *, limit: int = 100) -> tuple[AuditRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise AuditConfigurationError("审计查询数量必须在 1 到 1000 之间")
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    """
                    SELECT * FROM audit_records
                    ORDER BY occurred_at_utc DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            except sqlite3.Error as error:
                raise AuditStorageError("审计记录查询失败") from error
            return tuple(self._record_from_row(row) for row in rows)

    def reserve_undo(self, audit_id: str, operation_id: str) -> bool:
        self._validate_uuid(audit_id, "审计 ID")
        self._validate_uuid(operation_id, "撤销操作 ID")
        return self._update_undo_state(
            audit_id,
            AuditUndoState.AVAILABLE,
            AuditUndoState.IN_PROGRESS,
            operation_id,
            None,
        )

    def release_undo(self, audit_id: str, operation_id: str) -> bool:
        self._validate_uuid(audit_id, "审计 ID")
        self._validate_uuid(operation_id, "撤销操作 ID")
        return self._update_undo_state(
            audit_id,
            AuditUndoState.IN_PROGRESS,
            AuditUndoState.AVAILABLE,
            None,
            operation_id,
        )

    def complete_undo(self, audit_id: str, operation_id: str) -> bool:
        self._validate_uuid(audit_id, "审计 ID")
        self._validate_uuid(operation_id, "撤销操作 ID")
        return self._update_undo_state(
            audit_id,
            AuditUndoState.IN_PROGRESS,
            AuditUndoState.COMPLETED,
            operation_id,
            operation_id,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _configure(self) -> None:
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA synchronous=NORMAL")

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > self._SCHEMA_VERSION:
            raise sqlite3.DatabaseError("database schema is newer than runtime")
        if version == 0:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_records (
                        audit_id TEXT PRIMARY KEY,
                        occurred_at_utc TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        arguments_json TEXT NOT NULL,
                        risk TEXT NOT NULL,
                        confirmation TEXT NOT NULL,
                        policy_version INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        safe_message TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        undo_data_json TEXT,
                        undo_state TEXT NOT NULL,
                        undone_by TEXT
                    )
                    """
                )
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS audit_recent_idx ON audit_records(occurred_at_utc DESC)"
                )
                self._connection.execute(f"PRAGMA user_version={self._SCHEMA_VERSION}")
                self._connection.execute("COMMIT")
            except sqlite3.Error:
                self._connection.execute("ROLLBACK")
                raise

    def _update_undo_state(
        self,
        audit_id: str,
        expected: AuditUndoState,
        target: AuditUndoState,
        new_operation: str | None,
        expected_operation: str | None,
    ) -> bool:
        with self._lock:
            self._ensure_open()
            query = (
                "UPDATE audit_records SET undo_state = ?, undone_by = ? "
                "WHERE audit_id = ? AND undo_state = ?"
            )
            parameters: list[Any] = [
                target.value,
                new_operation,
                audit_id,
                expected.value,
            ]
            if expected_operation is not None:
                query += " AND undone_by = ?"
                parameters.append(expected_operation)
            try:
                cursor = self._connection.execute(query, parameters)
            except sqlite3.Error as error:
                raise AuditStorageError("审计撤销状态更新失败") from error
            return cursor.rowcount == 1

    @staticmethod
    def _validate_write(
        context: AuditContext,
        manifest: ToolManifest,
        decision: PolicyDecision,
        duration_ms: int,
    ) -> None:
        if decision.call_id != context.tool_call_id:
            raise AuditConfigurationError("审计调用 ID 与策略决策不一致")
        if decision.tool_name != manifest.name:
            raise AuditConfigurationError("审计工具与策略决策不一致")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise AuditConfigurationError("审计耗时必须是非负整数")

    @staticmethod
    def _validate_uuid(value: str, label: str) -> None:
        try:
            UUID(value)
        except (ValueError, AttributeError) as error:
            raise AuditConfigurationError(f"{label} 必须是 UUID") from error

    @staticmethod
    def _redact_arguments(
        arguments: Mapping[str, JsonValue],
        sensitive_fields: tuple[str, ...],
    ) -> dict[str, Any]:
        try:
            copied = json.loads(canonical_json(arguments))
        except Exception as error:
            raise AuditConfigurationError("审计参数不是规范 JSON") from error
        for field in sensitive_fields:
            current: Any = copied
            parts = field.split(".")
            for part in parts[:-1]:
                if not isinstance(current, dict) or part not in current:
                    current = None
                    break
                current = current[part]
            if isinstance(current, dict) and parts[-1] in current:
                current[parts[-1]] = "[REDACTED]"
        assert isinstance(copied, dict)
        return copied

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> AuditRecord:
        arguments = json.loads(row["arguments_json"])
        undo_data = (
            None
            if row["undo_data_json"] is None
            else json.loads(row["undo_data_json"])
        )
        return AuditRecord(
            row["audit_id"],
            datetime.fromisoformat(row["occurred_at_utc"]),
            row["turn_id"],
            row["correlation_id"],
            row["tool_call_id"],
            row["tool_name"],
            arguments,
            RiskLevel[row["risk"]],
            ConfirmationMode(row["confirmation"]),
            row["policy_version"],
            ToolExecutionStatus(row["status"]),
            row["safe_message"],
            row["duration_ms"],
            undo_data,
            AuditUndoState(row["undo_state"]),
            row["undone_by"],
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise AuditConfigurationError("审计存储已关闭")
