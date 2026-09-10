"""Agent 线程、轮次和脱敏事件摘要的本地存储"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .agent_types import AgentApprovalMode, AgentEvent, validate_identifier
from .memory_schema import ensure_memory_schema


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class AgentBinding:
    session_id: str
    codex_thread_id: str | None
    runtime_id: str | None
    approval_mode_override: AgentApprovalMode | None


@dataclass(frozen=True, slots=True)
class AgentTurn:
    turn_id: str
    session_id: str
    approval_mode: AgentApprovalMode
    status: str
    codex_turn_id: str | None
    input_tokens: int
    output_tokens: int


class AgentStore:
    """每次操作使用独立连接，跨运行时线程共享数据库路径"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            ensure_memory_schema(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS agent_preferences ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self, *, write: bool = False):
        connection = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def bind_thread(self, session_id: str, thread_id: str, runtime_id: str) -> None:
        for value in (session_id, thread_id, runtime_id):
            validate_identifier(value)
        with self._connect(write=True) as connection:
            connection.execute(
                "INSERT INTO agent_session_bindings"
                "(session_id,codex_thread_id,runtime_id,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "codex_thread_id=excluded.codex_thread_id,runtime_id=excluded.runtime_id,"
                "updated_at=excluded.updated_at",
                (session_id, thread_id, runtime_id, _now()),
            )

    def set_session_mode(self, session_id: str, mode: AgentApprovalMode | None) -> None:
        validate_identifier(session_id)
        if mode is not None and not isinstance(mode, AgentApprovalMode):
            raise ValueError("Agent 审批模式无效")
        with self._connect(write=True) as connection:
            # 尚未创建线程的空会话也可以保存模式覆盖
            connection.execute(
                "INSERT INTO agent_session_bindings"
                "(session_id,approval_mode_override,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "approval_mode_override=excluded.approval_mode_override,updated_at=excluded.updated_at",
                (session_id, mode.value if mode else None, _now()),
            )

    def binding(self, session_id: str) -> AgentBinding | None:
        validate_identifier(session_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_session_bindings WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        mode = row["approval_mode_override"]
        return AgentBinding(row["session_id"], row["codex_thread_id"], row["runtime_id"], AgentApprovalMode(mode) if mode else None)

    def session_mode(self, session_id: str) -> AgentApprovalMode | None:
        binding = self.binding(session_id)
        return binding.approval_mode_override if binding else None

    def global_mode(self) -> AgentApprovalMode | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM agent_preferences WHERE key='global_mode'"
            ).fetchone()
        if row is None:
            return None
        try:
            return AgentApprovalMode(row["value"])
        except ValueError:
            return None

    def set_global_mode(self, mode: AgentApprovalMode | None) -> None:
        if mode is not None and not isinstance(mode, AgentApprovalMode):
            raise TypeError("Agent 审批模式无效")
        with self._connect(write=True) as connection:
            if mode is None:
                connection.execute("DELETE FROM agent_preferences WHERE key='global_mode'")
            else:
                connection.execute(
                    "INSERT INTO agent_preferences(key,value) VALUES('global_mode',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (mode.value,),
                )

    def start_turn(self, turn_id: str, session_id: str, mode: AgentApprovalMode) -> None:
        validate_identifier(turn_id)
        validate_identifier(session_id)
        if not isinstance(mode, AgentApprovalMode):
            raise ValueError("Agent 审批模式无效")  # noqa: TRY004
        with self._connect(write=True) as connection:
            # 重复轮次必须拒绝，不能覆盖已执行任务的记录
            connection.execute(
                "INSERT INTO agent_turns(turn_id,session_id,approval_mode,status,started_at) "
                "VALUES(?,?,?,?,?)", (turn_id, session_id, mode.value, "running", _now()),
            )

    def bind_codex_turn(self, turn_id: str, codex_turn_id: str) -> bool:
        validate_identifier(turn_id)
        validate_identifier(codex_turn_id)
        with self._connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE agent_turns SET codex_turn_id=? WHERE turn_id=? "
                "AND status='running' AND (codex_turn_id IS NULL OR codex_turn_id=?)",
                (codex_turn_id, turn_id, codex_turn_id),
            )
            return cursor.rowcount == 1

    def turn(self, turn_id: str) -> AgentTurn | None:
        validate_identifier(turn_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_turns WHERE turn_id=?", (turn_id,)).fetchone()
        if row is None:
            return None
        return AgentTurn(row["turn_id"], row["session_id"], AgentApprovalMode(row["approval_mode"]), row["status"], row["codex_turn_id"], row["input_tokens"], row["output_tokens"])

    def finish_turn(self, turn_id: str, status: str, *, input_tokens: int = 0, output_tokens: int = 0) -> bool:
        validate_identifier(turn_id)
        if status not in {"completed", "cancelled", "failed", "outcome_unknown"}:
            raise ValueError("Agent 终态无效")
        if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (input_tokens, output_tokens)):
            raise ValueError("Agent 使用量无效")
        with self._connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE agent_turns SET status=?,completed_at=?,input_tokens=?,output_tokens=?,safe_summary=? "
                "WHERE turn_id=? AND status='running'",
                (status, _now(), input_tokens, output_tokens, status, turn_id),
            )
            return cursor.rowcount == 1

    def append_event_summary(self, event: AgentEvent, summary: str | None = None) -> bool:
        # 摘要只由固定事件类别生成，显示载荷和任意调用方正文不进入审计表
        with self._connect(write=True) as connection:
            owner = connection.execute("SELECT session_id,status FROM agent_turns WHERE turn_id=?", (event.turn_id,)).fetchone()
            if owner is None or owner["session_id"] != event.session_id:
                raise ValueError("Agent 事件归属无效")
            if owner["status"] != "running":
                return False
            cursor = connection.execute(
                "INSERT OR IGNORE INTO agent_events(turn_id,seq,event_type,safe_summary,created_at) VALUES(?,?,?,?,?)",
                (event.turn_id, event.seq, event.type.value, event.type.value, event.timestamp),
            )
            return cursor.rowcount == 1

    def delete_session(self, session_id: str) -> None:
        validate_identifier(session_id)
        with self._connect(write=True) as connection:
            connection.execute("DELETE FROM agent_events WHERE turn_id IN (SELECT turn_id FROM agent_turns WHERE session_id=?)", (session_id,))
            connection.execute("DELETE FROM agent_turns WHERE session_id=?", (session_id,))
            connection.execute("DELETE FROM agent_session_bindings WHERE session_id=?", (session_id,))

    def thread_id(self, session_id: str) -> str | None:
        binding = self.binding(session_id)
        return binding.codex_thread_id if binding else None

    def session_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM agent_session_bindings "
                "UNION SELECT session_id FROM agent_turns"
            ).fetchall()
        return tuple(row[0] for row in rows)

    def clear(self) -> None:
        with self._connect(write=True) as connection:
            for table in ("agent_events", "agent_turns", "agent_session_bindings", "agent_preferences"):
                connection.execute(f"DELETE FROM {table}")


