"""本地分层记忆的隐私策略与 SQLite 事实存储"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid4


class MemoryError(RuntimeError):
    """记忆子系统可报告的基础错误"""

    code = "memory.error"


class MemoryConfigurationError(MemoryError):
    """记忆内容、设置或生命周期无效"""

    code = "memory.configuration"


class MemoryStorageError(MemoryError):
    """SQLite 记忆读写失败"""

    code = "memory.storage"


class MemoryStatus(str, Enum):
    """长期记忆的审核与删除状态"""

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CONFLICTED = "conflicted"
    DELETED = "deleted"


class MemorySensitivity(str, Enum):
    """记忆内容的稳定敏感等级"""

    NORMAL = "normal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """从本地事实源读取的不可变记忆记录"""

    id: str
    category: str
    content: str
    source_turn_id: str
    created_at: datetime
    updated_at: datetime
    confidence: float
    sensitivity: MemorySensitivity
    expires_at: datetime | None
    status: MemoryStatus


class MemoryPolicy:
    """在数据库写入前识别禁止保存的高风险秘密"""

    _PATTERNS = (
        re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S+"),
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{16,}"),
        re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
        re.compile(
            r"(?:验证码|verification\s*code)\D{0,8}\d{6}\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b\d{17}[\dXx]\b"),
        re.compile(r"\b(?:\d[ -]?){16,19}\b"),
    )

    def is_prohibited(self, content: str) -> bool:
        return any(pattern.search(content) for pattern in self._PATTERNS)


class MemoryStore:
    """提供事务化生命周期和 FTS5 检索的本地记忆事实源"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        policy: MemoryPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        enabled: bool = True,
    ) -> None:
        path = Path(database_path)
        if not path.parent.exists():
            raise MemoryConfigurationError("记忆数据库父目录不存在")
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
            raise MemoryStorageError("无法打开记忆数据库") from error
        self._policy = policy or MemoryPolicy()
        self._clock = clock
        self._enabled = enabled
        self._closed = False
        self._lock = threading.RLock()
        self._migrate()

    def diagnostics(self) -> dict[str, object]:
        with self._lock:
            self._ensure_open()
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            fts = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='memory_fts'"
            ).fetchone()
            return {"schema_version": version, "fts5": fts is not None}

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def create_candidate(
        self,
        category: str,
        content: str,
        source_turn_id: str,
        confidence: float,
        *,
        sensitivity: MemorySensitivity = MemorySensitivity.NORMAL,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        return self._create(
            category,
            content,
            source_turn_id,
            confidence,
            sensitivity,
            expires_at,
            MemoryStatus.CANDIDATE,
        )

    def create_confirmed(
        self,
        *,
        category: str,
        content: str,
        source_turn_id: str,
        confidence: float = 1.0,
        sensitivity: MemorySensitivity = MemorySensitivity.NORMAL,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        self._validate_input(category, content, source_turn_id, confidence, expires_at)
        with self._lock:
            self._ensure_open()
            existing = self._connection.execute(
                """
                SELECT 1 FROM memories
                WHERE category = ? AND status = ? AND content != ?
                LIMIT 1
                """,
                (category, MemoryStatus.CONFIRMED.value, content),
            ).fetchone()
        status = MemoryStatus.CONFLICTED if existing else MemoryStatus.CONFIRMED
        return self._create(
            category,
            content,
            source_turn_id,
            confidence,
            sensitivity,
            expires_at,
            status,
        )

    def confirm(self, memory_id: str) -> MemoryRecord:
        self._ensure_writes_enabled()
        self._validate_uuid(memory_id, "记忆 ID")
        now = self._now()
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is None or row["status"] != MemoryStatus.CANDIDATE.value:
                raise MemoryConfigurationError("记忆候选不存在或状态无效")
            conflict = self._connection.execute(
                """
                SELECT 1 FROM memories
                WHERE category = ? AND status = ? AND content != ? AND id != ?
                LIMIT 1
                """,
                (
                    row["category"],
                    MemoryStatus.CONFIRMED.value,
                    row["content"],
                    memory_id,
                ),
            ).fetchone()
            status = MemoryStatus.CONFLICTED if conflict else MemoryStatus.CONFIRMED
            self._connection.execute(
                "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now.isoformat(), memory_id),
            )
            self._sync_fts(memory_id)
            record = self.get(memory_id)
            assert record is not None
            return record

    def resolve_conflict(self, memory_id: str) -> MemoryRecord:
        """显式采用冲突记录并软删除同类别旧确认记录"""

        self._ensure_writes_enabled()
        self._validate_uuid(memory_id, "记忆 ID")
        now = self._now().isoformat()
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is None or row["status"] != MemoryStatus.CONFLICTED.value:
                raise MemoryConfigurationError("冲突记忆不存在或状态无效")
            replaced = self._connection.execute(
                "SELECT id FROM memories WHERE category = ? AND status = ? AND id != ?",
                (
                    row["category"],
                    MemoryStatus.CONFIRMED.value,
                    memory_id,
                ),
            ).fetchall()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "UPDATE memories SET status = ?, updated_at = ? "
                    "WHERE category = ? AND status = ? AND id != ?",
                    (
                        MemoryStatus.DELETED.value,
                        now,
                        row["category"],
                        MemoryStatus.CONFIRMED.value,
                        memory_id,
                    ),
                )
                self._connection.execute(
                    "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
                    (MemoryStatus.CONFIRMED.value, now, memory_id),
                )
                for replaced_row in replaced:
                    self._connection.execute(
                        "DELETE FROM memory_fts WHERE id = ?",
                        (replaced_row["id"],),
                    )
                self._sync_fts(memory_id)
                self._connection.execute("COMMIT")
            except sqlite3.Error as error:
                self._connection.execute("ROLLBACK")
                raise MemoryStorageError("冲突记忆解决失败") from error
            record = self.get(memory_id)
            assert record is not None
            return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        self._validate_uuid(memory_id, "记忆 ID")
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            return None if row is None else self._record(row)

    def list_all(self) -> tuple[MemoryRecord, ...]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM memories WHERE status != ? ORDER BY created_at",
                (MemoryStatus.DELETED.value,),
            ).fetchall()
            return tuple(self._record(row) for row in rows)

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        statuses: frozenset[MemoryStatus] = frozenset({MemoryStatus.CONFIRMED}),
        limit: int = 20,
    ) -> tuple[MemoryRecord, ...]:
        if not query.strip() or not 1 <= limit <= 100:
            raise MemoryConfigurationError("记忆检索参数无效")
        if not statuses or MemoryStatus.DELETED in statuses:
            raise MemoryConfigurationError("记忆检索状态无效")
        now = self._now().isoformat()
        placeholders = ",".join("?" for _ in statuses)
        if len(query.strip()) < 3:
            sql = (
                "SELECT m.* FROM memories m "
                f"WHERE m.content LIKE ? ESCAPE '\\' AND m.status IN ({placeholders}) "
                "AND (m.expires_at IS NULL OR m.expires_at > ?)"
            )
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters: list[object] = [
                f"%{escaped}%",
                *(status.value for status in statuses),
                now,
            ]
            order = " ORDER BY m.updated_at DESC LIMIT ?"
        else:
            sql = (
                "SELECT m.* FROM memory_fts f JOIN memories m ON m.id=f.id "
                f"WHERE memory_fts MATCH ? AND m.status IN ({placeholders}) "
                "AND (m.expires_at IS NULL OR m.expires_at > ?)"
            )
            parameters = [query, *(status.value for status in statuses), now]
            order = " ORDER BY bm25(memory_fts), m.updated_at DESC LIMIT ?"
        if category is not None:
            sql += " AND m.category = ?"
            parameters.append(category)
        sql += order
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as error:
                raise MemoryStorageError("记忆检索失败") from error
            return tuple(self._record(row) for row in rows)

    def delete(self, memory_id: str) -> bool:
        self._validate_uuid(memory_id, "记忆 ID")
        with self._lock:
            self._ensure_open()
            cursor = self._connection.execute(
                "UPDATE memories SET status = ?, updated_at = ? WHERE id = ? AND status != ?",
                (
                    MemoryStatus.DELETED.value,
                    self._now().isoformat(),
                    memory_id,
                    MemoryStatus.DELETED.value,
                ),
            )
            self._connection.execute("DELETE FROM memory_fts WHERE id = ?", (memory_id,))
            return cursor.rowcount == 1

    def clear_date(self, target: date) -> int:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT id FROM memories WHERE substr(created_at, 1, 10) = ? AND status != ?",
                (target.isoformat(), MemoryStatus.DELETED.value),
            ).fetchall()
            for row in rows:
                self.delete(row["id"])
            return len(rows)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _create(
        self,
        category: str,
        content: str,
        source_turn_id: str,
        confidence: float,
        sensitivity: MemorySensitivity,
        expires_at: datetime | None,
        status: MemoryStatus,
    ) -> MemoryRecord:
        self._ensure_writes_enabled()
        self._validate_input(category, content, source_turn_id, confidence, expires_at)
        if self._policy.is_prohibited(content):
            raise MemoryConfigurationError("内容包含禁止保存的敏感信息")
        memory_id = str(uuid4())
        now = self._now()
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        category,
                        content,
                        source_turn_id,
                        now.isoformat(),
                        now.isoformat(),
                        confidence,
                        sensitivity.value,
                        None if expires_at is None else expires_at.astimezone(UTC).isoformat(),
                        status.value,
                    ),
                )
                self._sync_fts(memory_id)
                self._connection.execute("COMMIT")
            except sqlite3.Error as error:
                self._connection.execute("ROLLBACK")
                raise MemoryStorageError("记忆写入失败") from error
            record = self.get(memory_id)
            assert record is not None
            return record

    def _sync_fts(self, memory_id: str) -> None:
        self._connection.execute("DELETE FROM memory_fts WHERE id = ?", (memory_id,))
        self._connection.execute(
            """
            INSERT INTO memory_fts(id, content, category)
            SELECT id, content, category FROM memories
            WHERE id = ? AND status != ?
            """,
            (memory_id, MemoryStatus.DELETED.value),
        )

    def _migrate(self) -> None:
        try:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, content TEXT NOT NULL,
                    source_turn_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, confidence REAL NOT NULL,
                    sensitivity TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED, content, category UNINDEXED, tokenize='trigram')"
            )
            self._connection.execute("PRAGMA user_version=1")
        except sqlite3.Error as error:
            raise MemoryStorageError("记忆数据库迁移失败") from error

    def _validate_input(self, category, content, source_turn_id, confidence, expires_at) -> None:
        if not isinstance(category, str) or not category.strip() or len(category) > 64:
            raise MemoryConfigurationError("记忆类别无效")
        if not isinstance(content, str) or not content.strip() or len(content) > 4096:
            raise MemoryConfigurationError("记忆正文无效")
        self._validate_uuid(source_turn_id, "来源轮次 ID")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise MemoryConfigurationError("记忆置信度必须在零到一之间")
        if expires_at is not None and expires_at.tzinfo is None:
            raise MemoryConfigurationError("记忆过期时间必须包含时区")

    def _ensure_writes_enabled(self) -> None:
        if not self._enabled:
            raise MemoryConfigurationError("记忆写入已关闭")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise MemoryConfigurationError("记忆时钟必须包含时区")
        return now.astimezone(UTC)

    @staticmethod
    def _validate_uuid(value: str, label: str) -> None:
        try:
            UUID(value)
        except (ValueError, AttributeError) as error:
            raise MemoryConfigurationError(f"{label} 必须是 UUID") from error

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            row["id"], row["category"], row["content"], row["source_turn_id"],
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]), row["confidence"],
            MemorySensitivity(row["sensitivity"]),
            None if row["expires_at"] is None else datetime.fromisoformat(row["expires_at"]),
            MemoryStatus(row["status"]),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryConfigurationError("记忆存储已关闭")
