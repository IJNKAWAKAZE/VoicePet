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

from .memory import MemoryPolicy
from .memory_recall import escape_like, query_terms, sql_relevance
from .memory_schema import MemorySchemaError, ensure_memory_schema
from .memory_transactions import immediate_transaction


class SessionArchiveError(RuntimeError):
    """会话归档配置、校验或 SQLite 操作失败"""

    code = "session.archive"


@dataclass(frozen=True, slots=True)
class SessionTurnRecord:
    """一个已完成且带自动过期时间的会话轮次"""

    id: str
    turn_id: str
    session_id: str
    user_text: str
    assistant_text: str
    session_date: date
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """包含多轮问答的会话列表摘要"""

    id: str
    title: str
    turn_count: int
    created_at: datetime
    updated_at: datetime
    last_user_text: str
    last_assistant_text: str
    is_active: bool = False


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
    session_id: str = ""
    source_turn_ids: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    source_title: str = ""


class SessionArchiveStore:
    """在共享数据库内维护独立且可硬删除的会话数据层"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        retention_days: int = 7,
        summary_retention_days: int | None = None,
        enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._validate_retention(retention_days, "会话保留天数")
        if summary_retention_days is None:
            summary_retention_days = retention_days
        self._validate_retention(summary_retention_days, "摘要保留天数")
        if type(enabled) is not bool:
            raise SessionArchiveError("会话归档开关必须是布尔值")
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
            self._connection.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as error:
            raise SessionArchiveError("无法打开会话数据库") from error
        self._retention_days = retention_days
        self._summary_retention_days = summary_retention_days
        self._enabled = enabled
        self._policy = MemoryPolicy()
        self._clock = clock
        self._closed = False
        self._lock = threading.RLock()
        try:
            self._migrate()
            self._connection.execute("PRAGMA journal_mode=WAL")
        except SessionArchiveError:
            self._closed = True
            self._connection.close()
            raise

    def archive_turn(
        self,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        session_id: str | None = None,
    ) -> SessionTurnRecord:
        """按来源轮次幂等归档一个完整问答"""

        self._validate_uuid(turn_id, "轮次 ID")
        normalized_session_id = session_id or turn_id
        self._validate_uuid(normalized_session_id, "会话 ID")
        user = self._validate_text(user_text, "用户文本", 4096)
        assistant = self._validate_text(assistant_text, "助手文本", 16_000)
        now = self._now()
        with self._lock:
            self._ensure_open()
            self._ensure_writes_enabled()
            try:
                with immediate_transaction(self._connection):
                    existing = self._connection.execute(
                        "SELECT * FROM session_turns WHERE turn_id = ?",
                        (turn_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing["session_id"] != normalized_session_id:
                            raise SessionArchiveError("轮次 ID 已属于其他会话")
                        return self._turn_record(existing)
                    suppressed = self._connection.execute(
                        "SELECT 1 FROM summary_suppressions WHERE source_turn_id=? LIMIT 1",
                        (turn_id,),
                    ).fetchone()
                    if suppressed is not None:
                        raise SessionArchiveError("轮次 ID 已被删除或失效")
                    session = self._connection.execute(
                        "SELECT deleted FROM memory_sessions WHERE id = ?",
                        (normalized_session_id,),
                    ).fetchone()
                    if session is not None and bool(session["deleted"]):
                        raise SessionArchiveError("已删除会话不能重新写入")
                    timestamp = now.astimezone(UTC).isoformat()
                    if session is None:
                        self._connection.execute(
                            "INSERT INTO memory_sessions"
                            "(id,title,created_at,updated_at,generation,deleted) "
                            "VALUES (?,?,?,?,0,0)",
                            (normalized_session_id, user[:500], timestamp, timestamp),
                        )
                    else:
                        self._connection.execute(
                            "UPDATE memory_sessions SET updated_at=? WHERE id=?",
                            (timestamp, normalized_session_id),
                        )
                    self._connection.execute(
                        "INSERT INTO session_turns "
                        "(id, turn_id, session_id, user_text, assistant_text, "
                        "session_date, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid4()),
                            turn_id,
                            normalized_session_id,
                            user,
                            assistant,
                            now.date().isoformat(),
                            timestamp,
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
        *,
        source_turn_ids: Sequence[str] = (),
        decisions: Sequence[str] = (),
        expected_generation: int | None = None,
    ) -> ShortTermSummaryRecord:
        """按来源轮次新增或更新结构化短期摘要"""

        self._validate_uuid(source_turn_id, "来源轮次 ID")
        normalized_topic = self._validate_text(topic, "摘要话题", 500)
        items = self._validate_text_sequence(
            unfinished_items, "未完成事项", limit=200, maximum=10
        )
        normalized_decisions = self._validate_text_sequence(
            decisions, "摘要决定", limit=200, maximum=10
        )
        sources = self._normalize_sources(source_turn_id, source_turn_ids)
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 0
        ):
            raise SessionArchiveError("会话 generation 无效")
        if any(
            self._policy.is_prohibited(text)
            for text in (normalized_topic, *normalized_decisions, *items)
        ):
            raise SessionArchiveError("短期摘要包含禁止保存的敏感信息")
        now = self._now()
        now_utc = now.astimezone(UTC)
        with self._lock:
            self._ensure_open()
            self._ensure_writes_enabled()
            try:
                with immediate_transaction(self._connection):
                    turns = self._source_rows(sources)
                    if len(turns) != len(sources):
                        raise SessionArchiveError("短期摘要缺少已归档来源轮次")
                    session_ids = {row["session_id"] for row in turns}
                    if len(session_ids) != 1:
                        raise SessionArchiveError("短期摘要来源不能跨会话")
                    session_id = next(iter(session_ids))
                    session = self._connection.execute(
                        "SELECT title,generation,deleted FROM memory_sessions WHERE id=?",
                        (session_id,),
                    ).fetchone()
                    if session is None or bool(session["deleted"]):
                        raise SessionArchiveError("短期摘要来源会话无效")
                    if expected_generation is not None and (
                        session["generation"] != expected_generation
                    ):
                        raise SessionArchiveError("短期摘要来源会话 generation 已失效")
                    if any(row["expires_at"] <= now_utc.isoformat() for row in turns):
                        raise SessionArchiveError("短期摘要来源轮次已过期")
                    suppressed = self._connection.execute(
                        "SELECT 1 FROM summary_suppressions WHERE session_id=? "
                        f"AND source_turn_id IN ({self._placeholders(sources)}) LIMIT 1",
                        (session_id, *sources),
                    ).fetchone()
                    if suppressed is not None:
                        raise SessionArchiveError("短期摘要来源已被抑制")
                    existing = self._connection.execute(
                        "SELECT id,created_at,expires_at FROM short_term_summaries "
                        "WHERE source_turn_id=?",
                        (source_turn_id,),
                    ).fetchone()
                    summary_id = str(uuid4()) if existing is None else existing["id"]
                    created_at = now_utc.isoformat() if existing is None else existing["created_at"]
                    expires_at = (
                        max(datetime.fromisoformat(row["created_at"]) for row in turns)
                        + timedelta(days=self._summary_retention_days)
                        if existing is None
                        else datetime.fromisoformat(existing["expires_at"])
                    )
                    last_turn = turns[-1]
                    self._connection.execute(
                        "INSERT INTO short_term_summaries "
                        "(id, source_turn_id, session_id, source_turn_ids_json, topic, "
                        "decisions_json, unfinished_json, session_date, created_at, "
                        "updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(source_turn_id) DO UPDATE SET "
                        "session_id=excluded.session_id,"
                        "source_turn_ids_json=excluded.source_turn_ids_json,"
                        "topic=excluded.topic,decisions_json=excluded.decisions_json,"
                        "unfinished_json=excluded.unfinished_json,"
                        "session_date=excluded.session_date,updated_at=excluded.updated_at",
                        (
                            summary_id,
                            source_turn_id,
                            session_id,
                            json.dumps(sources),
                            normalized_topic,
                            json.dumps(normalized_decisions, ensure_ascii=False),
                            json.dumps(items, ensure_ascii=False),
                            last_turn["session_date"],
                            created_at,
                            now_utc.isoformat(),
                            expires_at.isoformat(),
                        ),
                    )
                    self._connection.execute(
                        "UPDATE memory_sessions SET updated_at=? WHERE id=?",
                        (now_utc.isoformat(), session_id),
                    )
            except sqlite3.Error as error:
                raise SessionArchiveError("短期摘要写入失败") from error
            row = self._connection.execute(
                "SELECT * FROM short_term_summaries WHERE source_turn_id = ?",
                (source_turn_id,),
            ).fetchone()
            assert row is not None
            return self._summary_record(row)

    def session_generation(self, session_id: str) -> int | None:
        """返回会话当前 generation，未知会话返回空"""

        self._validate_uuid(session_id, "会话 ID")
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT generation FROM memory_sessions WHERE id=?", (session_id,)
            ).fetchone()
            return None if row is None else int(row["generation"])

    def sources_valid(
        self,
        session_id: str,
        turn_ids: Sequence[str],
        generation: int,
    ) -> bool:
        """核对异步结果仍绑定同代且有效的会话来源"""

        self._validate_uuid(session_id, "会话 ID")
        if type(generation) is not int or generation < 0:
            return False
        sources = self._validate_source_ids(turn_ids)
        if not sources:
            return False
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            session = self._connection.execute(
                "SELECT generation,deleted FROM memory_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if (
                session is None
                or bool(session["deleted"])
                or session["generation"] != generation
            ):
                return False
            rows = self._source_rows(sources)
            return len(rows) == len(sources) and all(
                row["session_id"] == session_id and row["expires_at"] > now
                for row in rows
            )

    def configure(
        self,
        *,
        enabled: bool,
        retention_days: int,
        summary_retention_days: int,
    ) -> None:
        """立即更新后续写入资格与新记录保留期"""

        if type(enabled) is not bool:
            raise SessionArchiveError("会话归档开关必须是布尔值")
        self._validate_retention(retention_days, "会话保留天数")
        self._validate_retention(summary_retention_days, "摘要保留天数")
        with self._lock:
            self._ensure_open()
            self._enabled = enabled
            self._retention_days = retention_days
            self._summary_retention_days = summary_retention_days

    def list_turns(self) -> tuple[SessionTurnRecord, ...]:
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM session_turns WHERE expires_at>? ORDER BY created_at",
                (now,),
            ).fetchall()
            return tuple(self._turn_record(row) for row in rows)

    def list_session_turns(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[SessionTurnRecord, ...]:
        """按时间顺序返回一个会话内的全部轮次"""

        self._validate_uuid(session_id, "会话 ID")
        self._validate_optional_limit(limit)
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            if limit is None:
                rows = self._connection.execute(
                    "SELECT * FROM session_turns WHERE session_id=? AND expires_at>? "
                    "ORDER BY created_at,rowid",
                    (session_id, now),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM (SELECT *,rowid AS archive_rowid FROM session_turns "
                    "WHERE session_id=? AND expires_at>? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT ?) "
                    "ORDER BY created_at,archive_rowid",
                    (session_id, now, limit),
                ).fetchall()
            return tuple(self._turn_record(row) for row in rows)

    def list_sessions(self) -> tuple[SessionRecord, ...]:
        """按更新时间返回具有有效正文或摘要的会话"""

        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM memory_sessions s WHERE s.deleted=0 AND ("
                "EXISTS(SELECT 1 FROM session_turns t WHERE t.session_id=s.id "
                "AND t.expires_at>?) OR EXISTS(SELECT 1 FROM short_term_summaries m "
                "WHERE m.session_id=s.id AND m.expires_at>?)) ORDER BY s.updated_at",
                (now, now),
            ).fetchall()
            records: list[SessionRecord] = []
            for row in rows:
                latest = self._connection.execute(
                    "SELECT user_text, assistant_text FROM session_turns "
                    "WHERE session_id=? AND expires_at>? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (row["id"], now),
                ).fetchone()
                turn_count = self._connection.execute(
                    "SELECT count(*) FROM session_turns WHERE session_id=? AND expires_at>?",
                    (row["id"], now),
                ).fetchone()[0]
                records.append(
                    SessionRecord(
                        row["id"],
                        row["title"],
                        int(turn_count),
                        datetime.fromisoformat(row["created_at"]),
                        datetime.fromisoformat(row["updated_at"]),
                        "" if latest is None else latest["user_text"],
                        "" if latest is None else latest["assistant_text"],
                    )
                )
            return tuple(records)

    def list_summaries(
        self,
        session_id: str | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[ShortTermSummaryRecord, ...]:
        if session_id is not None:
            self._validate_uuid(session_id, "会话 ID")
        self._validate_optional_limit(limit)
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            if session_id is None:
                if limit is None:
                    rows = self._connection.execute(
                        "SELECT * FROM short_term_summaries WHERE expires_at>? "
                        "ORDER BY created_at,rowid", (now,)
                    ).fetchall()
                else:
                    rows = self._connection.execute(
                        "SELECT * FROM (SELECT *,rowid AS archive_rowid "
                        "FROM short_term_summaries WHERE expires_at>? "
                        "ORDER BY created_at DESC,rowid DESC LIMIT ?) "
                        "ORDER BY created_at,archive_rowid", (now, limit)
                    ).fetchall()
            else:
                if limit is None:
                    rows = self._connection.execute(
                        "SELECT * FROM short_term_summaries WHERE session_id=? "
                        "AND expires_at>? ORDER BY created_at,rowid", (session_id, now)
                    ).fetchall()
                else:
                    rows = self._connection.execute(
                        "SELECT * FROM (SELECT *,rowid AS archive_rowid "
                        "FROM short_term_summaries WHERE session_id=? AND expires_at>? "
                        "ORDER BY created_at DESC,rowid DESC LIMIT ?) "
                        "ORDER BY created_at,archive_rowid", (session_id, now, limit)
                    ).fetchall()
            return tuple(self._summary_record(row) for row in rows)

    def search_summaries(
        self,
        hints: Sequence[tuple[str, int]],
        *,
        exclude_session_id: str = "",
        limit: int = 10,
    ) -> tuple[ShortTermSummaryRecord, ...]:
        """用单条有界 SQL 召回其他会话中的相关有效摘要"""

        if type(limit) is not int or not 1 <= limit <= 100:
            raise SessionArchiveError("摘要召回数量无效")
        if exclude_session_id:
            self._validate_uuid(exclude_session_id, "排除会话 ID")
        try:
            terms = query_terms(hints)
        except (TypeError, ValueError) as error:
            raise SessionArchiveError("摘要召回提示无效") from error
        if not terms:
            return ()
        clauses = []
        parameters: list[object] = []
        for term, _ in terms:
            clauses.append(
                "(topic LIKE ? ESCAPE '\\' OR decisions_json LIKE ? ESCAPE '\\' "
                "OR unfinished_json LIKE ? ESCAPE '\\')"
            )
            escaped = f"%{escape_like(term)}%"
            parameters.extend((escaped, escaped, escaped))
        now = self._now().astimezone(UTC).isoformat()
        score, score_parameters = sql_relevance("topic || ' ' || decisions_json || ' ' || unfinished_json", terms)
        sql = (
            f"SELECT *,({score}) AS recall_score FROM short_term_summaries WHERE ("
            + " OR ".join(clauses)
            + ") AND expires_at>?"
        )
        parameters.append(now)
        if exclude_session_id:
            sql += " AND session_id!=?"
            parameters.append(exclude_session_id)
        sql += " ORDER BY recall_score DESC,updated_at DESC,rowid DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(sql, [*score_parameters, *parameters]).fetchall()
            except sqlite3.Error as error:
                raise SessionArchiveError("短期摘要召回失败") from error
            return tuple(self._summary_record(row) for row in rows if row["recall_score"] >= 2)

    def source_label(self, turn_id: str) -> dict[str, object]:
        """返回来源轮次的最小标签且不泄露正文"""

        self._validate_uuid(turn_id, "轮次 ID")
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT t.session_id,t.created_at,t.expires_at,s.title,s.deleted "
                "FROM session_turns t LEFT JOIN memory_sessions s ON s.id=t.session_id "
                "WHERE t.turn_id=?", (turn_id,)
            ).fetchone()
            if row is not None:
                status = (
                    "deleted" if bool(row["deleted"])
                    else "expired" if row["expires_at"] <= now
                    else "available"
                )
                return {
                    "title": "" if status == "deleted" else (row["title"] or ""),
                    "status": status,
                    "created_at": datetime.fromisoformat(row["created_at"]),
                    "session_id": row["session_id"],
                }
            suppressed = self._connection.execute(
                "SELECT x.session_id,x.created_at,s.title,s.deleted "
                "FROM summary_suppressions x LEFT JOIN memory_sessions s "
                "ON s.id=x.session_id WHERE x.source_turn_id=? "
                "ORDER BY x.created_at DESC LIMIT 1", (turn_id,)
            ).fetchone()
            if suppressed is not None:
                deleted = bool(suppressed["deleted"])
                return {
                    "title": "" if deleted else (suppressed["title"] or ""),
                    "status": "deleted" if deleted else "missing",
                    "created_at": datetime.fromisoformat(suppressed["created_at"]),
                    "session_id": suppressed["session_id"],
                }
            return {"title": "", "status": "missing", "created_at": None, "session_id": ""}

    def clear_date(self, target: date) -> int:
        """硬删除指定本地日期的会话正文和短期摘要"""

        if not isinstance(target, date) or isinstance(target, datetime):
            raise SessionArchiveError("清理日期无效")
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    turns = self._connection.execute(
                        "SELECT turn_id,session_id FROM session_turns WHERE session_date=?",
                        (target.isoformat(),),
                    ).fetchall()
                    turn_ids = {row["turn_id"] for row in turns}
                    turn_ids.update(
                        row["source_turn_id"]
                        for row in self._connection.execute(
                            "SELECT source_turn_id FROM summary_suppressions "
                            "WHERE source_date=?",
                            (target.isoformat(),),
                        ).fetchall()
                    )
                    summaries = self._summaries_containing(turn_ids)
                    self._suppress_summary_rows(summaries, now)
                    self._suppress_sources(
                        ((row["session_id"], row["turn_id"]) for row in turns), now
                    )
                    if summaries:
                        self._connection.execute(
                            f"DELETE FROM short_term_summaries WHERE id IN "
                            f"({self._placeholders(summaries)})",
                            tuple(row["id"] for row in summaries),
                        )
                    self._connection.execute(
                        "DELETE FROM session_turns WHERE session_date=?", (target.isoformat(),)
                    )
                    self._cleanup_turn_references(turn_ids)
                    return len(turns) + len(summaries)
            except sqlite3.Error as error:
                raise SessionArchiveError("按日期清理会话失败") from error

    def purge_expired(self) -> int:
        """硬删除超过保留期的全部会话层记录"""

        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    summaries = self._connection.execute(
                        "SELECT * FROM short_term_summaries WHERE expires_at<=?", (now,)
                    ).fetchall()
                    turns = self._connection.execute(
                        "SELECT turn_id,session_id FROM session_turns WHERE expires_at<=?", (now,)
                    ).fetchall()
                    expired_sources = {
                        source
                        for row in summaries
                        for source in self._decode_sources(row["source_turn_ids_json"])
                    } | {row["turn_id"] for row in turns}
                    self._suppress_summary_rows(summaries, now)
                    self._suppress_sources(
                        ((row["session_id"], row["turn_id"]) for row in turns), now
                    )
                    self._connection.execute(
                        "DELETE FROM short_term_summaries WHERE expires_at<=?", (now,)
                    )
                    self._connection.execute(
                        "DELETE FROM session_turns WHERE expires_at<=?", (now,)
                    )
                    self._cleanup_turn_references(expired_sources)
                    return len(summaries) + len(turns)
            except sqlite3.Error as error:
                raise SessionArchiveError("过期会话清理失败") from error

    def discard_turn(self, turn_id: str) -> bool:
        """回滚迟到轮次产生的会话归档与摘要"""

        self._validate_uuid(turn_id, "轮次 ID")
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    turn = self._connection.execute(
                        "SELECT session_id FROM session_turns WHERE turn_id=?", (turn_id,)
                    ).fetchone()
                    summaries = self._summaries_containing({turn_id})
                    self._suppress_summary_rows(summaries, now)
                    if turn is not None:
                        self._suppress_sources(((turn["session_id"], turn_id),), now)
                    if summaries:
                        self._connection.execute(
                            f"DELETE FROM short_term_summaries WHERE id IN "
                            f"({self._placeholders(summaries)})",
                            tuple(row["id"] for row in summaries),
                        )
                    cursor = self._connection.execute(
                        "DELETE FROM session_turns WHERE turn_id=?", (turn_id,)
                    )
                    self._cleanup_turn_references({turn_id})
                    return cursor.rowcount == 1
            except sqlite3.Error as error:
                raise SessionArchiveError("会话轮次删除失败") from error

    def discard_session(self, session_id: str) -> bool:
        """硬删除一个会话内的全部轮次和关联摘要"""

        self._validate_uuid(session_id, "会话 ID")
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    session = self._connection.execute(
                        "SELECT deleted FROM memory_sessions WHERE id=?", (session_id,)
                    ).fetchone()
                    if session is None or bool(session["deleted"]):
                        return False
                    summaries = self._connection.execute(
                        "SELECT * FROM short_term_summaries WHERE session_id=?", (session_id,)
                    ).fetchall()
                    turns = self._connection.execute(
                        "SELECT turn_id FROM session_turns WHERE session_id=?", (session_id,)
                    ).fetchall()
                    self._suppress_summary_rows(summaries, now)
                    self._suppress_sources(
                        ((session_id, row["turn_id"]) for row in turns), now
                    )
                    self._connection.execute(
                        "DELETE FROM short_term_summaries WHERE session_id=?", (session_id,)
                    )
                    self._connection.execute(
                        "DELETE FROM session_turns WHERE session_id=?", (session_id,)
                    )
                    self._connection.execute(
                        "UPDATE memory_sessions SET title='',updated_at=?,"
                        "generation=generation+1,deleted=1 WHERE id=?",
                        (now, session_id),
                    )
                    self._connection.execute(
                        "UPDATE memory_jobs SET status='cancelled',error='' WHERE session_id=?",
                        (session_id,),
                    )
                    self._connection.execute(
                        "DELETE FROM memory_progress WHERE session_id=?", (session_id,)
                    )
                    return True
            except sqlite3.Error as error:
                raise SessionArchiveError("会话删除失败") from error

    def clear_all(self) -> int:
        """硬删除全部会话正文和短期摘要"""

        with self._lock:
            self._ensure_open()
            now = self._now().astimezone(UTC).isoformat()
            try:
                with immediate_transaction(self._connection):
                    summaries = self._connection.execute(
                        "SELECT * FROM short_term_summaries"
                    ).fetchall()
                    turns = self._connection.execute(
                        "SELECT turn_id,session_id FROM session_turns"
                    ).fetchall()
                    session_ids = {
                        row["session_id"] for row in summaries
                    } | {row["session_id"] for row in turns}
                    session_ids.update(
                        row["session_id"]
                        for row in self._connection.execute(
                            "SELECT DISTINCT x.session_id FROM summary_suppressions x "
                            "JOIN memory_sessions s ON s.id=x.session_id WHERE s.deleted=0"
                        ).fetchall()
                    )
                    self._suppress_summary_rows(summaries, now)
                    self._suppress_sources(
                        ((row["session_id"], row["turn_id"]) for row in turns), now
                    )
                    self._connection.execute("DELETE FROM short_term_summaries")
                    self._connection.execute("DELETE FROM session_turns")
                    if session_ids:
                        placeholders = self._placeholders(session_ids)
                        self._connection.execute(
                            f"UPDATE memory_sessions SET title='',updated_at=?,"
                            f"generation=generation+1,deleted=1 WHERE id IN ({placeholders})",
                            (now, *session_ids),
                        )
                        self._connection.execute(
                            f"UPDATE memory_jobs SET status='cancelled',error='' "
                            f"WHERE session_id IN ({placeholders})", tuple(session_ids)
                        )
                        self._connection.execute(
                            f"DELETE FROM memory_progress WHERE session_id IN ({placeholders})",
                            tuple(session_ids),
                        )
                    return len(summaries) + len(turns)
            except sqlite3.Error as error:
                raise SessionArchiveError("全部会话清理失败") from error

    def delete_summary(self, summary_id: str) -> bool:
        """删除摘要并抑制其完整来源范围"""

        self._validate_uuid(summary_id, "摘要 ID")
        now = self._now().astimezone(UTC).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    row = self._connection.execute(
                        "SELECT * FROM short_term_summaries WHERE id=?", (summary_id,)
                    ).fetchone()
                    if row is None:
                        return False
                    self._suppress_summary_rows((row,), now)
                    self._connection.execute(
                        "DELETE FROM short_term_summaries WHERE id=?", (summary_id,)
                    )
                    return True
            except sqlite3.Error as error:
                raise SessionArchiveError("短期摘要删除失败") from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock:
            try:
                ensure_memory_schema(self._connection)
            except MemorySchemaError as error:
                raise SessionArchiveError("会话数据库结构初始化失败") from error

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

    def _normalize_sources(
        self,
        source_turn_id: str,
        source_turn_ids: Sequence[str],
    ) -> tuple[str, ...]:
        if isinstance(source_turn_ids, (str, bytes, bytearray)) or not isinstance(
            source_turn_ids, Sequence
        ):
            raise SessionArchiveError("摘要来源轮次范围无效")
        sources = (
            (source_turn_id,)
            if len(source_turn_ids) == 0
            else self._validate_source_ids(source_turn_ids)
        )
        if len(set(sources)) != len(sources):
            raise SessionArchiveError("摘要来源轮次不能重复")
        if sources[-1] != source_turn_id:
            raise SessionArchiveError("摘要最后来源轮次不匹配")
        return sources

    def _validate_source_ids(self, values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
            raise SessionArchiveError("摘要来源轮次范围无效")
        sources = tuple(values)
        for value in sources:
            self._validate_uuid(value, "来源轮次 ID")
        return sources

    def _source_rows(self, sources: Sequence[str]) -> list[sqlite3.Row]:
        if not sources:
            return []
        rows = self._connection.execute(
            f"SELECT * FROM session_turns WHERE turn_id IN ({self._placeholders(sources)})",
            tuple(sources),
        ).fetchall()
        indexed = {row["turn_id"]: row for row in rows}
        return [indexed[source] for source in sources if source in indexed]

    def _summaries_containing(self, turn_ids: set[str]) -> list[sqlite3.Row]:
        if not turn_ids:
            return []
        rows = self._connection.execute("SELECT * FROM short_term_summaries").fetchall()
        return [
            row
            for row in rows
            if turn_ids.intersection(self._decode_sources(row["source_turn_ids_json"]))
        ]

    def _suppress_summary_rows(
        self,
        rows: Sequence[sqlite3.Row],
        timestamp: str,
    ) -> None:
        pairs = (
            (row["session_id"], source)
            for row in rows
            for source in self._decode_sources(row["source_turn_ids_json"])
        )
        self._suppress_sources(pairs, timestamp)

    def _suppress_sources(
        self,
        pairs: object,
        timestamp: str,
    ) -> None:
        records = []
        for session_id, source_id in pairs:
            source = self._connection.execute(
                "SELECT created_at,session_date FROM session_turns WHERE turn_id=?",
                (source_id,),
            ).fetchone()
            created_at = timestamp if source is None else source["created_at"]
            source_date = (
                datetime.fromisoformat(timestamp).date().isoformat()
                if source is None
                else source["session_date"]
            )
            records.append(
                (session_id, source_id, source_date, created_at)
            )
        self._connection.executemany(
            "INSERT INTO summary_suppressions"
            "(session_id,source_turn_id,source_date,created_at) "
            "VALUES (?,?,?,?) ON CONFLICT(session_id,source_turn_id) DO NOTHING",
            records,
        )

    def _cleanup_turn_references(self, turn_ids: set[str]) -> None:
        if not turn_ids:
            return
        placeholders = self._placeholders(turn_ids)
        self._connection.execute(
            f"DELETE FROM memory_progress WHERE turn_id IN ({placeholders})",
            tuple(turn_ids),
        )
        jobs = self._connection.execute(
            "SELECT id,source_turn_ids_json FROM memory_jobs"
        ).fetchall()
        job_ids = [
            row["id"]
            for row in jobs
            if turn_ids.intersection(self._decode_sources(row["source_turn_ids_json"]))
        ]
        if job_ids:
            self._connection.execute(
                f"UPDATE memory_jobs SET status='cancelled',error='' "
                f"WHERE id IN ({self._placeholders(job_ids)})",
                tuple(job_ids),
            )

    @staticmethod
    def _decode_sources(value: str) -> tuple[str, ...]:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as error:
            raise SessionArchiveError("摘要来源数据损坏") from error
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise SessionArchiveError("摘要来源数据损坏")
        return tuple(decoded)

    @staticmethod
    def _placeholders(values: object) -> str:
        return ",".join("?" for _ in values)

    @staticmethod
    def _validate_retention(value: int, label: str) -> None:
        if type(value) is not int or not 1 <= value <= 365:
            raise SessionArchiveError(f"{label}必须在一到三百六十五之间")

    @staticmethod
    def _validate_optional_limit(value: int | None) -> None:
        if value is not None and (type(value) is not int or not 1 <= value <= 100):
            raise SessionArchiveError("会话查询数量无效")

    @classmethod
    def _validate_text_sequence(
        cls,
        value: Sequence[str],
        label: str,
        *,
        limit: int,
        maximum: int,
    ) -> tuple[str, ...]:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise SessionArchiveError(f"{label}无效")
        if len(value) > maximum:
            raise SessionArchiveError(f"{label}数量过多")
        return tuple(cls._validate_text(item, label, limit) for item in value)

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
            row["session_id"],
            row["user_text"],
            row["assistant_text"],
            date.fromisoformat(row["session_date"]),
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["expires_at"]),
        )

    def _summary_record(self, row: sqlite3.Row) -> ShortTermSummaryRecord:
        session = self._connection.execute(
            "SELECT title,deleted FROM memory_sessions WHERE id=?", (row["session_id"],)
        ).fetchone()
        source_title = (
            session["title"]
            if session is not None and not bool(session["deleted"])
            else ""
        )
        return ShortTermSummaryRecord(
            row["id"],
            row["source_turn_id"],
            row["topic"],
            tuple(json.loads(row["unfinished_json"])),
            date.fromisoformat(row["session_date"]),
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]),
            datetime.fromisoformat(row["expires_at"]),
            row["session_id"],
            tuple(json.loads(row["source_turn_ids_json"])),
            tuple(json.loads(row["decisions_json"])),
            source_title,
        )
