"""持久化后台记忆整理任务"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from .memory_schema import MemorySchemaError, ensure_memory_schema
from .memory_transactions import immediate_transaction

_OVERSIZED_SOURCE_ERROR = "来源超出自动整理预算"


class MemoryJobError(RuntimeError):
    """后台记忆任务状态、校验或持久化操作失败"""

    code = "memory.jobs"


@dataclass(frozen=True, slots=True)
class MemoryJob:
    """一个可跨进程重启恢复的后台整理任务"""

    id: str
    session_id: str
    source_turn_ids: tuple[str, ...]
    generation: int
    status: str
    attempts: int
    next_run_at: datetime
    created_at: datetime
    started_at: datetime | None
    error: str


class MemoryJobStore:
    """使用独立 SQLite 连接维护后台整理任务和来源进度"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        path = Path(database_path)
        if not path.parent.exists():
            raise MemoryJobError("记忆任务数据库父目录不存在")
        if not callable(clock):
            raise MemoryJobError("记忆任务时钟无效")
        try:
            self._connection = sqlite3.connect(
                path,
                isolation_level=None,
                timeout=5,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=5000")
        except (OSError, sqlite3.Error) as error:
            raise MemoryJobError("无法打开记忆任务数据库") from error
        self._clock = clock
        self._closed = False
        self._lock = threading.RLock()
        try:
            ensure_memory_schema(self._connection)
            self._connection.execute("PRAGMA journal_mode=WAL")
        except (MemorySchemaError, sqlite3.Error) as error:
            self._closed = True
            self._connection.close()
            raise MemoryJobError("记忆任务数据库结构初始化失败") from error

    def enqueue(self, session_id: str, turn_id: str, generation: int) -> None:
        """验证真实来源并合并到同会话尚未开始的任务"""

        self._validate_uuid(session_id, "会话 ID")
        self._validate_uuid(turn_id, "来源轮次 ID")
        self._validate_generation(generation)
        now = self._now()
        timestamp = now.isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    if not self._sources_valid(
                        session_id, (turn_id,), generation, timestamp
                    ):
                        raise MemoryJobError("记忆任务来源已失效")
                    self._connection.execute(
                        "INSERT INTO memory_progress"
                        "(turn_id,session_id,extracted,summarized) VALUES (?,?,0,0) "
                        "ON CONFLICT(turn_id) DO NOTHING",
                        (turn_id, session_id),
                    )
                    progress = self._connection.execute(
                        "SELECT session_id,extracted FROM memory_progress WHERE turn_id=?",
                        (turn_id,),
                    ).fetchone()
                    if progress is None or progress["session_id"] != session_id:
                        raise MemoryJobError("记忆任务来源进度已损坏")
                    if bool(progress["extracted"]):
                        return
                    retained_rows = self._connection.execute(
                        "SELECT source_turn_ids_json FROM memory_jobs "
                        "WHERE status!='cancelled'"
                    ).fetchall()
                    if any(
                        turn_id in self._decode_sources(row["source_turn_ids_json"])
                        for row in retained_rows
                    ):
                        return
                    pending = self._connection.execute(
                        "SELECT id,source_turn_ids_json FROM memory_jobs "
                        "WHERE session_id=? AND generation=? AND status='pending' "
                        "AND attempts=0 ORDER BY created_at,rowid LIMIT 1",
                        (session_id, generation),
                    ).fetchone()
                    next_run = (now + timedelta(seconds=5)).isoformat()
                    if pending is None:
                        self._insert_pending(
                            session_id,
                            (turn_id,),
                            generation,
                            created_at=timestamp,
                            next_run_at=next_run,
                        )
                        return
                    sources = self._decode_sources(pending["source_turn_ids_json"])
                    self._connection.execute(
                        "UPDATE memory_jobs SET source_turn_ids_json=?,next_run_at=? "
                        "WHERE id=?",
                        (self._encode_sources((*sources, turn_id)), next_run, pending["id"]),
                    )
            except MemoryJobError:
                raise
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务入队失败") from error

    def claim_due(self) -> MemoryJob | None:
        """在写事务内原子领取一个到期任务"""

        now = self._now()
        timestamp = now.isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    running = self._connection.execute(
                        "SELECT 1 FROM memory_jobs WHERE status='running' LIMIT 1"
                    ).fetchone()
                    if running is not None:
                        return None
                    latest = self._connection.execute(
                        "SELECT MAX(started_at) FROM memory_jobs WHERE started_at IS NOT NULL"
                    ).fetchone()[0]
                    if latest is not None:
                        latest_start = self._parse_datetime(latest, "任务启动时间")
                        if now - latest_start < timedelta(seconds=30):
                            return None
                    while True:
                        row = self._connection.execute(
                            "SELECT * FROM memory_jobs WHERE status='pending' "
                            "AND next_run_at<=? ORDER BY next_run_at,created_at,rowid LIMIT 1",
                            (timestamp,),
                        ).fetchone()
                        if row is None:
                            return None
                        sources = self._decode_sources(row["source_turn_ids_json"])
                        if not self._sources_valid(
                            row["session_id"], sources, row["generation"], timestamp
                        ):
                            self._connection.execute(
                                "UPDATE memory_jobs SET status='cancelled',error='' WHERE id=?",
                                (row["id"],),
                            )
                            continue
                        self._connection.execute(
                            "UPDATE memory_jobs SET status='running',attempts=attempts+1,"
                            "started_at=?,error='' WHERE id=?",
                            (timestamp, row["id"]),
                        )
                        claimed = self._connection.execute(
                            "SELECT * FROM memory_jobs WHERE id=?", (row["id"],)
                        ).fetchone()
                        assert claimed is not None
                        return self._job_record(claimed)
            except MemoryJobError:
                raise
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务领取失败") from error

    def finish(
        self,
        job_id: str,
        *,
        extracted_ids: Sequence[str],
        summarized_ids: Sequence[str] = (),
    ) -> None:
        """提交真实覆盖进度并为未覆盖来源创建新任务"""

        self._validate_uuid(job_id, "任务 ID")
        extracted = self._validate_id_sequence(extracted_ids, "提取来源")
        summarized = self._validate_id_sequence(summarized_ids, "摘要来源")
        now = self._now()
        timestamp = now.isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    row = self._connection.execute(
                        "SELECT * FROM memory_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if row is None or row["status"] != "running":
                        raise MemoryJobError("记忆任务已不在运行")
                    sources = self._decode_sources(row["source_turn_ids_json"])
                    if not self._sources_valid(
                        row["session_id"], sources, row["generation"], timestamp
                    ):
                        raise MemoryJobError("记忆任务来源或代数已失效")
                    if not set(extracted).issubset(sources):
                        raise MemoryJobError("提取覆盖范围不属于当前任务")
                    if summarized and not self._sources_valid(
                        row["session_id"], summarized, row["generation"], timestamp
                    ):
                        raise MemoryJobError("摘要来源或代数已失效")
                    self._validate_summary_progress(summarized, extracted, row["session_id"])
                    if extracted:
                        placeholders = self._placeholders(extracted)
                        cursor = self._connection.execute(
                            f"UPDATE memory_progress SET extracted=1 "
                            f"WHERE session_id=? AND turn_id IN ({placeholders})",
                            (row["session_id"], *extracted),
                        )
                        if cursor.rowcount != len(extracted):
                            raise MemoryJobError("提取来源进度已损坏")
                    if summarized:
                        placeholders = self._placeholders(summarized)
                        cursor = self._connection.execute(
                            f"UPDATE memory_progress SET summarized=1 "
                            f"WHERE session_id=? AND turn_id IN ({placeholders})",
                            (row["session_id"], *summarized),
                        )
                        if cursor.rowcount != len(summarized):
                            raise MemoryJobError("摘要来源进度已损坏")
                    self._connection.execute(
                        "UPDATE memory_jobs SET status='completed',error='' WHERE id=?",
                        (job_id,),
                    )
                    extracted_set = set(extracted)
                    uncovered = tuple(source for source in sources if source not in extracted_set)
                    if uncovered:
                        self._insert_pending(
                            row["session_id"],
                            uncovered,
                            row["generation"],
                            created_at=timestamp,
                            next_run_at=(now + timedelta(seconds=5)).isoformat(),
                        )
            except MemoryJobError:
                raise
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务完成状态写入失败") from error

    def fail(self, job_id: str, message: str) -> None:
        """记录安全短错误并在剩余额度内安排重试"""

        self._validate_uuid(job_id, "任务 ID")
        safe_message = self._safe_error(message)
        now = self._now()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    row = self._connection.execute(
                        "SELECT attempts,status FROM memory_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if row is None or row["status"] != "running":
                        return
                    if row["attempts"] < 2:
                        self._connection.execute(
                            "UPDATE memory_jobs SET status='pending',next_run_at=?,error=? "
                            "WHERE id=?",
                            ((now + timedelta(seconds=60)).isoformat(), safe_message, job_id),
                        )
                    else:
                        self._connection.execute(
                            "UPDATE memory_jobs SET status='failed',error=? WHERE id=?",
                            (safe_message, job_id),
                        )
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务失败状态写入失败") from error

    def skip_source(self, job_id: str, turn_id: str) -> None:
        """隔离单个超预算来源并重新安排同任务内其余来源"""

        self._validate_uuid(job_id, "任务 ID")
        self._validate_uuid(turn_id, "来源轮次 ID")
        now = self._now()
        timestamp = now.isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    row = self._connection.execute(
                        "SELECT * FROM memory_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if row is None or row["status"] != "running":
                        raise MemoryJobError("记忆任务已不在运行")
                    sources = self._decode_sources(row["source_turn_ids_json"])
                    if turn_id not in sources:
                        raise MemoryJobError("跳过来源不属于当前任务")
                    if not self._sources_valid(
                        row["session_id"], sources, row["generation"], timestamp
                    ):
                        self._connection.execute(
                            "UPDATE memory_jobs SET status='cancelled',error='' WHERE id=?",
                            (job_id,),
                        )
                        return
                    remaining = tuple(
                        source
                        for source in sources
                        if source != turn_id and not self._is_extracted(source, row["session_id"])
                    )
                    self._connection.execute(
                        "UPDATE memory_jobs SET source_turn_ids_json=?,status='failed',error=? "
                        "WHERE id=?",
                        (
                            self._encode_sources((turn_id,)),
                            _OVERSIZED_SOURCE_ERROR,
                            job_id,
                        ),
                    )
                    if remaining:
                        self._insert_pending(
                            row["session_id"],
                            remaining,
                            row["generation"],
                            created_at=timestamp,
                            next_run_at=(now + timedelta(seconds=5)).isoformat(),
                        )
            except MemoryJobError:
                raise
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务来源跳过失败") from error

    def invalidate(self, session_id: str | None = None) -> None:
        """取消全部或指定会话内尚未结束的任务"""

        if session_id is not None:
            self._validate_uuid(session_id, "会话 ID")
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    if session_id is None:
                        self._connection.execute(
                            "UPDATE memory_jobs SET status='cancelled',error='' "
                            "WHERE status IN ('pending','running')"
                        )
                    else:
                        self._connection.execute(
                            "UPDATE memory_jobs SET status='cancelled',error='' "
                            "WHERE session_id=? AND status IN ('pending','running')",
                            (session_id,),
                        )
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务取消失败") from error

    def recover_interrupted(self) -> None:
        """将上次进程中断时的运行任务计入失败额度"""

        now = self._now()
        retry_at = (now + timedelta(seconds=60)).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    rows = self._connection.execute(
                        "SELECT id,attempts FROM memory_jobs WHERE status='running'"
                    ).fetchall()
                    for row in rows:
                        if row["attempts"] < 2:
                            self._connection.execute(
                                "UPDATE memory_jobs SET status='pending',next_run_at=?,error=? "
                                "WHERE id=?",
                                (retry_at, "自动整理被中断", row["id"]),
                            )
                        else:
                            self._connection.execute(
                                "UPDATE memory_jobs SET status='failed',error=? WHERE id=?",
                                ("自动整理被中断", row["id"]),
                            )
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务中断恢复失败") from error

    def purge_terminal(self) -> int:
        """清理已离开限流窗口且不再承担来源抑制的终态任务"""

        now = self._now()
        timestamp = now.isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    rows = self._connection.execute(
                        "SELECT * FROM memory_jobs "
                        "WHERE status IN ('completed','cancelled','failed')"
                    ).fetchall()
                    deleted = 0
                    for row in rows:
                        if row["started_at"] is not None:
                            started_at = self._parse_datetime(
                                row["started_at"], "任务启动时间"
                            )
                            if now - started_at < timedelta(seconds=30):
                                continue
                        if row["status"] == "failed":
                            sources = self._decode_sources(row["source_turn_ids_json"])
                            valid_sources = self._valid_sources(
                                row["session_id"], sources, row["generation"], timestamp
                            )
                            if valid_sources:
                                if valid_sources != sources:
                                    self._connection.execute(
                                        "UPDATE memory_jobs SET source_turn_ids_json=? WHERE id=?",
                                        (self._encode_sources(valid_sources), row["id"]),
                                    )
                                continue
                        cursor = self._connection.execute(
                            "DELETE FROM memory_jobs WHERE id=?", (row["id"],)
                        )
                        deleted += cursor.rowcount
                    return deleted
            except MemoryJobError:
                raise
            except sqlite3.Error as error:
                raise MemoryJobError("记忆终态任务清理失败") from error

    def list_unsummarized(self, session_id: str) -> tuple[str, ...]:
        """按真实轮次顺序返回有效且尚未总结的来源"""

        self._validate_uuid(session_id, "会话 ID")
        now = self._now().isoformat()
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    "SELECT t.turn_id FROM session_turns t "
                    "JOIN memory_sessions s ON s.id=t.session_id "
                    "JOIN memory_progress p ON p.turn_id=t.turn_id "
                    "WHERE t.session_id=? AND s.deleted=0 AND t.expires_at>? "
                    "AND p.session_id=t.session_id AND p.summarized=0 "
                    "ORDER BY t.created_at,t.rowid",
                    (session_id, now),
                ).fetchall()
                skipped_rows = self._connection.execute(
                    "SELECT source_turn_ids_json FROM memory_jobs "
                    "WHERE session_id=? AND status='failed' AND error=?",
                    (session_id, _OVERSIZED_SOURCE_ERROR),
                ).fetchall()
                skipped = {
                    source
                    for row in skipped_rows
                    for source in self._decode_sources(row["source_turn_ids_json"])
                }
                return tuple(row["turn_id"] for row in rows if row["turn_id"] not in skipped)
            except sqlite3.Error as error:
                raise MemoryJobError("未总结来源读取失败") from error

    def list_jobs(self) -> tuple[MemoryJob, ...]:
        """按创建顺序返回持久化任务状态"""

        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    "SELECT * FROM memory_jobs ORDER BY created_at,rowid"
                ).fetchall()
                return tuple(self._job_record(row) for row in rows)
            except MemoryJobError:
                raise
            except sqlite3.Error as error:
                raise MemoryJobError("记忆任务状态读取失败") from error

    def close(self) -> None:
        """幂等关闭任务数据库连接"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _insert_pending(
        self,
        session_id: str,
        sources: tuple[str, ...],
        generation: int,
        *,
        created_at: str,
        next_run_at: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO memory_jobs"
            "(id,session_id,source_turn_ids_json,generation,status,attempts,"
            "next_run_at,created_at,started_at,error) VALUES (?,?,?,?,?,0,?,?,NULL,'')",
            (
                str(uuid4()),
                session_id,
                self._encode_sources(sources),
                generation,
                "pending",
                next_run_at,
                created_at,
            ),
        )

    def _sources_valid(
        self,
        session_id: str,
        sources: Sequence[str],
        generation: int,
        timestamp: str,
    ) -> bool:
        if not sources or type(generation) is not int or generation < 0:
            return False
        session = self._connection.execute(
            "SELECT generation,deleted FROM memory_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if (
            session is None
            or bool(session["deleted"])
            or session["generation"] != generation
        ):
            return False
        placeholders = self._placeholders(sources)
        rows = self._connection.execute(
            f"SELECT turn_id,session_id,expires_at FROM session_turns "
            f"WHERE turn_id IN ({placeholders})",
            tuple(sources),
        ).fetchall()
        indexed = {row["turn_id"]: row for row in rows}
        return len(indexed) == len(sources) and all(
            indexed[source]["session_id"] == session_id
            and indexed[source]["expires_at"] > timestamp
            for source in sources
        )

    def _valid_sources(
        self,
        session_id: str,
        sources: Sequence[str],
        generation: int,
        timestamp: str,
    ) -> tuple[str, ...]:
        session = self._connection.execute(
            "SELECT generation,deleted FROM memory_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if (
            session is None
            or bool(session["deleted"])
            or session["generation"] != generation
        ):
            return ()
        placeholders = self._placeholders(sources)
        rows = self._connection.execute(
            f"SELECT turn_id,session_id,expires_at FROM session_turns "
            f"WHERE turn_id IN ({placeholders})",
            tuple(sources),
        ).fetchall()
        indexed = {row["turn_id"]: row for row in rows}
        return tuple(
            source
            for source in sources
            if source in indexed
            and indexed[source]["session_id"] == session_id
            and indexed[source]["expires_at"] > timestamp
        )

    def _validate_summary_progress(
        self,
        summarized: tuple[str, ...],
        extracted: tuple[str, ...],
        session_id: str,
    ) -> None:
        if not summarized:
            return
        placeholders = self._placeholders(summarized)
        rows = self._connection.execute(
            f"SELECT turn_id,extracted FROM memory_progress WHERE session_id=? "
            f"AND turn_id IN ({placeholders})",
            (session_id, *summarized),
        ).fetchall()
        states = {row["turn_id"]: bool(row["extracted"]) for row in rows}
        current = set(extracted)
        if len(states) != len(summarized) or any(
            not states[source] and source not in current for source in summarized
        ):
            raise MemoryJobError("摘要来源尚未完成提取")

    def _is_extracted(self, turn_id: str, session_id: str) -> bool:
        row = self._connection.execute(
            "SELECT extracted FROM memory_progress WHERE turn_id=? AND session_id=?",
            (turn_id, session_id),
        ).fetchone()
        return row is not None and bool(row["extracted"])

    def _job_record(self, row: sqlite3.Row) -> MemoryJob:
        return MemoryJob(
            id=row["id"],
            session_id=row["session_id"],
            source_turn_ids=self._decode_sources(row["source_turn_ids_json"]),
            generation=row["generation"],
            status=row["status"],
            attempts=row["attempts"],
            next_run_at=self._parse_datetime(row["next_run_at"], "任务运行时间"),
            created_at=self._parse_datetime(row["created_at"], "任务创建时间"),
            started_at=(
                None
                if row["started_at"] is None
                else self._parse_datetime(row["started_at"], "任务启动时间")
            ),
            error=row["error"],
        )

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception as error:
            raise MemoryJobError("记忆任务时钟读取失败") from error
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise MemoryJobError("记忆任务时钟必须包含时区")
        return value.astimezone(UTC)

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryJobError("记忆任务存储已关闭")

    @staticmethod
    def _validate_uuid(value: str, label: str) -> None:
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError) as error:
            raise MemoryJobError(f"{label} 必须是 UUID") from error

    @staticmethod
    def _validate_generation(value: int) -> None:
        if type(value) is not int or value < 0:
            raise MemoryJobError("会话 generation 无效")

    @classmethod
    def _validate_id_sequence(
        cls, values: Sequence[str], label: str
    ) -> tuple[str, ...]:
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
            raise MemoryJobError(f"{label}范围无效")
        result = tuple(values)
        if len(set(result)) != len(result):
            raise MemoryJobError(f"{label}不能重复")
        for value in result:
            cls._validate_uuid(value, label)
        return result

    @staticmethod
    def _safe_error(message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise MemoryJobError("任务错误提示无效")
        normalized = " ".join(message.split())
        if len(normalized) > 160:
            return "自动整理失败"
        return normalized

    @staticmethod
    def _encode_sources(sources: Sequence[str]) -> str:
        return json.dumps(tuple(sources), separators=(",", ":"))

    @staticmethod
    def _decode_sources(value: str) -> tuple[str, ...]:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as error:
            raise MemoryJobError("记忆任务来源数据损坏") from error
        if (
            not isinstance(decoded, list)
            or not decoded
            or any(not isinstance(item, str) for item in decoded)
            or len(set(decoded)) != len(decoded)
        ):
            raise MemoryJobError("记忆任务来源数据损坏")
        return tuple(decoded)

    @staticmethod
    def _parse_datetime(value: object, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise MemoryJobError(f"{label}数据损坏") from error
        if parsed.tzinfo is None:
            raise MemoryJobError(f"{label}数据损坏")
        return parsed.astimezone(UTC)

    @staticmethod
    def _placeholders(values: Sequence[object]) -> str:
        return ",".join("?" for _ in values)
