"""独立保存可过期的会话轮次与短期摘要"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4


class SessionArchiveError(RuntimeError):
    """会话归档配置、校验或 SQLite 操作失败"""

    code = "session.archive"


@dataclass(frozen=True, slots=True)
class SessionTurnRecord:
    """一个已完成且带自动过期时间的会话轮次"""

    id: str
    turn_id: str
    user_text: str
    assistant_text: str
    session_date: date
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ShortTermSummaryRecord:
    """绑定来源轮次的当天话题与未完成事项摘要"""

    id: str
    source_turn_id: str
    topic: str
    unfinished_items: tuple[str, ...]
    session_date: date
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class SessionArchiveStore:
    """在共享数据库内维护独立且可硬删除的会话数据层"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        retention_days: int = 7,
        enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        if not 1 <= retention_days <= 365:
            raise SessionArchiveError("会话保留天数必须在一到三百六十五之间")
        path = Path(database_path)
        if not path.parent.exists():
            raise SessionArchiveError("会话数据库父目录不存在")
        try:
            self._connection = sqlite3.connect(
                path,
                isolation_level=None,
                timeout=5,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as error:
            raise SessionArchiveError("无法打开会话数据库") from error
        self._retention_days = retention_days
        self._enabled = bool(enabled)
        self._clock = clock
        self._closed = False
        self._lock = threading.RLock()
        self._migrate()

    def archive_turn(
        self,
        turn_id: str,
        user_text: str,
        assistant_text: str,
    ) -> SessionTurnRecord:
        """按来源轮次幂等归档一个完整问答"""

        self._ensure_writes_enabled()
        self._validate_uuid(turn_id, "轮次 ID")
        user = self._validate_text(user_text, "用户文本", 4096)
        assistant = self._validate_text(assistant_text, "助手文本", 16_000)
        now = self._now()
        with self._lock:
            self._ensure_open()
            existing = self._connection.execute(
                "SELECT * FROM session_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if existing is not None:
                return self._turn_record(existing)
            try:
                self._connection.execute(
                    "INSERT INTO session_turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        turn_id,
                        user,
                        assistant,
                        now.date().isoformat(),
                        now.astimezone(UTC).isoformat(),
                        (now.astimezone(UTC) + timedelta(days=self._retention_days)).isoformat(),
                    ),
                )
            except sqlite3.Error as error:
                raise SessionArchiveError("会话轮次写入失败") from error
            row = self._connection.execute(
                "SELECT * FROM session_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            assert row is not None
            return self._turn_record(row)

    def save_summary(
        self,
        source_turn_id: str,
        topic: str,
        unfinished_items: Sequence[str],
    ) -> ShortTermSummaryRecord:
        """按来源轮次新增或更新结构化短期摘要"""

        self._ensure_writes_enabled()
        self._validate_uuid(source_turn_id, "来源轮次 ID")
        normalized_topic = self._validate_text(topic, "摘要话题", 500)
        items = tuple(
            self._validate_text(item, "未完成事项", 200)
            for item in unfinished_items
        )
        if len(items) > 10:
            raise SessionArchiveError("未完成事项数量过多")
        now = self._now()
        expires_at = now.astimezone(UTC) + timedelta(days=self._retention_days)
        with self._lock:
            self._ensure_open()
            turn = self._connection.execute(
                "SELECT session_date FROM session_turns WHERE turn_id = ?",
                (source_turn_id,),
            ).fetchone()
            if turn is None:
                raise SessionArchiveError("短期摘要缺少已归档来源轮次")
            existing = self._connection.execute(
                "SELECT id, created_at FROM short_term_summaries WHERE source_turn_id = ?",
                (source_turn_id,),
            ).fetchone()
            summary_id = str(uuid4()) if existing is None else existing["id"]
            created_at = now.astimezone(UTC).isoformat() if existing is None else existing["created_at"]
            try:
                self._connection.execute(
                    "INSERT INTO short_term_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(source_turn_id) DO UPDATE SET "
                    "topic=excluded.topic, unfinished_json=excluded.unfinished_json, "
                    "updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                    (
                        summary_id,
                        source_turn_id,
                        normalized_topic,
                        json.dumps(items, ensure_ascii=False),
                        turn["session_date"],
                        created_at,
                        now.astimezone(UTC).isoformat(),
                        expires_at.isoformat(),
                    ),
                )
            except sqlite3.Error as error:
                raise SessionArchiveError("短期摘要写入失败") from error
            row = self._connection.execute(
                "SELECT * FROM short_term_summaries WHERE source_turn_id = ?",
                (source_turn_id,),
            ).fetchone()
            assert row is not None
            return self._summary_record(row)

    def list_turns(self) -> tuple[SessionTurnRecord, ...]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM session_turns ORDER BY created_at"
            ).fetchall()
            return tuple(self._turn_record(row) for row in rows)

    def list_summaries(self) -> tuple[ShortTermSummaryRecord, ...]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM short_term_summaries ORDER BY created_at"
            ).fetchall()
            return tuple(self._summary_record(row) for row in rows)

    def clear_date(self, target: date) -> int:
        """硬删除指定本地日期的会话正文和短期摘要"""

        with self._lock:
            self._ensure_open()
            summary_count = self._connection.execute(
                "SELECT count(*) FROM short_term_summaries WHERE session_date = ?",
                (target.isoformat(),),
            ).fetchone()[0]
            turn_count = self._connection.execute(
                "SELECT count(*) FROM session_turns WHERE session_date = ?",
                (target.isoformat(),),
            ).fetchone()[0]
            self._connection.execute(
                "DELETE FROM short_term_summaries WHERE session_date = ?",
                (target.isoformat(),),
            )
            self._connection.execute(
                "DELETE FROM session_turns WHERE session_date = ?",
                (target.isoformat(),),
            )
            return int(summary_count + turn_count)

    def purge_expired(self) -> int:
        """硬删除超过保留期的全部会话层记录"""

        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            summary_count = self._connection.execute(
                "SELECT count(*) FROM short_term_summaries WHERE expires_at <= ?",
                (now,),
            ).fetchone()[0]
            turn_count = self._connection.execute(
                "SELECT count(*) FROM session_turns WHERE expires_at <= ?",
                (now,),
            ).fetchone()[0]
            self._connection.execute(
                "DELETE FROM short_term_summaries WHERE expires_at <= ?",
                (now,),
            )
            self._connection.execute(
                "DELETE FROM session_turns WHERE expires_at <= ?",
                (now,),
            )
            return int(summary_count + turn_count)

    def discard_turn(self, turn_id: str) -> bool:
        """回滚迟到轮次产生的会话归档与摘要"""

        self._validate_uuid(turn_id, "轮次 ID")
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                "DELETE FROM short_term_summaries WHERE source_turn_id = ?",
                (turn_id,),
            )
            cursor = self._connection.execute(
                "DELETE FROM session_turns WHERE turn_id = ?",
                (turn_id,),
            )
            return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock:
            try:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS session_turns ("
                    "id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE, "
                    "user_text TEXT NOT NULL, assistant_text TEXT NOT NULL, "
                    "session_date TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "expires_at TEXT NOT NULL)"
                )
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS short_term_summaries ("
                    "id TEXT PRIMARY KEY, source_turn_id TEXT NOT NULL UNIQUE, "
                    "topic TEXT NOT NULL, unfinished_json TEXT NOT NULL, "
                    "session_date TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
                )
            except sqlite3.Error as error:
                raise SessionArchiveError("会话数据库迁移失败") from error

    def _ensure_writes_enabled(self) -> None:
        if not self._enabled:
            raise SessionArchiveError("会话归档写入已关闭")

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionArchiveError("会话归档已关闭")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise SessionArchiveError("会话归档时钟必须包含时区")
        return value

    @staticmethod
    def _validate_uuid(value: str, label: str) -> None:
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError) as error:
            raise SessionArchiveError(f"{label} 必须是 UUID") from error

    @staticmethod
    def _validate_text(value: str, label: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise SessionArchiveError(f"{label}无效")
        return value.strip()

    @staticmethod
    def _turn_record(row: sqlite3.Row) -> SessionTurnRecord:
        return SessionTurnRecord(
            row["id"],
            row["turn_id"],
            row["user_text"],
            row["assistant_text"],
            date.fromisoformat(row["session_date"]),
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["expires_at"]),
        )

    @staticmethod
    def _summary_record(row: sqlite3.Row) -> ShortTermSummaryRecord:
        return ShortTermSummaryRecord(
            row["id"],
            row["source_turn_id"],
            row["topic"],
            tuple(json.loads(row["unfinished_json"])),
            date.fromisoformat(row["session_date"]),
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]),
            datetime.fromisoformat(row["expires_at"]),
        )
