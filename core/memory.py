"""本地分层记忆的隐私策略与 SQLite 事实存储"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid4

from core.memory_facts import (
    SINGLE_VALUE_FACT_KEYS,
    FactEvidence,
    FactProposal,
    MemoryChange,
    fact_fingerprint,
    freeform_fact_key,
    is_direct_user_assertion,
    is_explicit_correction,
    normalize_fact_value,
    proposal_value_is_supported,
)
from core.memory_recall import (
    MAX_HINT_CHARS,
    escape_like,
    literal_fts_query,
    query_terms,
    sql_relevance,
)
from core.memory_schema import (
    MEMORY_SCHEMA_VERSION,
    MemorySchemaError,
    ensure_memory_schema,
)
from core.memory_transactions import immediate_transaction


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
    fact_key: str = ""
    value: str = ""
    cardinality: str = "multi"
    origin: str = "explicit"
    version: int = 1
    conflict_id: str | None = None
    keywords: tuple[str, ...] = ()
    session_id: str = ""
    source_title: str = ""
    source_state: str = "unavailable"
    source_time: datetime | None = None


class MemoryPolicy:
    """在数据库写入前识别禁止保存的高风险秘密"""

    _PATTERNS = (
        re.compile(r"(?:密码|口令)\s*(?:是|为|[:：=])\s*\S+"),
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
        try:
            ensure_memory_schema(self._connection)
        except MemorySchemaError:
            self._connection.close()
            self._closed = True
            raise

    def diagnostics(self) -> dict[str, object]:
        with self._lock:
            self._ensure_open()
            fts = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='memory_fts'"
            ).fetchone()
            return {"schema_version": MEMORY_SCHEMA_VERSION, "fts5": fts is not None}

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    def create_candidate(
        self, category: str, content: str, source_turn_id: str, confidence: float, *,
        sensitivity: MemorySensitivity = MemorySensitivity.NORMAL,
        expires_at: datetime | None = None, fact_key: str | None = None,
        value: str | None = None, explicit_update: bool = False, session_id: str = "",
    ) -> MemoryRecord:
        return self._create(
            category, content, source_turn_id, confidence, sensitivity, expires_at,
            MemoryStatus.CANDIDATE, fact_key, value, explicit_update, session_id,
        )

    def create_confirmed(
        self, *, category: str, content: str, source_turn_id: str,
        confidence: float = 1.0, sensitivity: MemorySensitivity = MemorySensitivity.NORMAL,
        expires_at: datetime | None = None, fact_key: str | None = None,
        value: str | None = None, explicit_update: bool = False, session_id: str = "",
    ) -> MemoryRecord:
        return self._create(
            category, content, source_turn_id, confidence, sensitivity, expires_at,
            MemoryStatus.CONFIRMED, fact_key, value, explicit_update, session_id,
        )

    def save_fact(
        self, proposal: FactProposal, evidence: FactEvidence, *,
        expected_generation: int | None = None,
    ) -> MemoryChange | None:
        """核验证据后在同一写事务中保存自动事实"""

        self._validate_input(proposal.category, proposal.content, evidence.turn_id,
                             proposal.confidence, None)
        self._validate_uuid(evidence.session_id, "来源会话 ID")
        self._validate_fact_fields(proposal.fact_key, proposal.value, proposal.quote,
                                   proposal.keywords)
        self._validate_text(evidence.user_text, 12000, "来源正文")
        self._reject_secrets(proposal.content, proposal.value, proposal.quote, *proposal.keywords)
        if proposal.quote not in evidence.user_text:
            raise MemoryConfigurationError("引用不属于来源用户消息")
        if not proposal_value_is_supported(proposal.fact_key, proposal.value, proposal.quote):
            raise MemoryConfigurationError("事实值缺少用户原文依据")
        if proposal.stability not in {"stable", "uncertain", "temporary"}:
            raise MemoryConfigurationError("记忆稳定性无效")
        if proposal.sensitivity not in {item.value for item in MemorySensitivity}:
            raise MemoryConfigurationError("记忆敏感等级无效")
        if type(proposal.explicit_update) is not bool:
            raise MemoryConfigurationError("记忆更新标记无效")
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 0
        ):
            raise MemoryConfigurationError("来源会话版本无效")
        with self._write_transaction():
            self._ensure_open()
            self._ensure_writes_enabled()
            self._validate_source(evidence, expected_generation)
            if proposal.sensitivity != "normal" or proposal.stability == "temporary":
                return None
            if re.search(r"今天|这次|此刻|暂时|今晚", evidence.user_text):
                return None
            uncertain = re.search(
                r"假如|假设|如果|他说|她说|扮演|角色设定|小说|可能|也许|不确定",
                evidence.user_text,
            )
            direct = is_direct_user_assertion(
                proposal.fact_key,
                proposal.value,
                proposal.quote,
            )
            status = (
                MemoryStatus.CONFIRMED
                if proposal.stability == "stable" and proposal.confidence >= .85
                and not uncertain and direct else MemoryStatus.CANDIDATE
            )
            _, change = self._save(
                category=proposal.category, content=proposal.content,
                source_turn_id=evidence.turn_id, confidence=proposal.confidence,
                sensitivity=MemorySensitivity.NORMAL, expires_at=None, status=status,
                fact_key=proposal.fact_key, value=proposal.value,
                explicit_update=proposal.explicit_update and self._is_correction(proposal.quote),
                session_id=evidence.session_id, origin="automatic",
                quote=proposal.quote, keywords=proposal.keywords,
            )
            return change

    def confirm(self, memory_id: str) -> MemoryRecord:
        self._validate_uuid(memory_id, "记忆 ID")
        with self._write_transaction():
            self._ensure_open()
            row = self._active_row(memory_id)
            if row["status"] != MemoryStatus.CANDIDATE.value:
                raise MemoryConfigurationError("记忆候选不存在或状态无效")
            peers = self._peers(row["fact_key"], exclude=memory_id)
            duplicate = next((peer for peer in peers if normalize_fact_value(peer["value"])
                              == normalize_fact_value(row["value"])), None)
            if duplicate is not None:
                self._consolidate_duplicate(row, duplicate["id"])
                if duplicate["status"] != "candidate":
                    self._connection.execute(
                        "UPDATE memories SET origin='manual', version=version+1, "
                        "updated_at=? WHERE id=?",
                        (self._now().isoformat(), duplicate["id"]),
                    )
                    self._sync_fts(duplicate["id"])
                    result = self.get(duplicate["id"])
                    assert result is not None
                    return result
                row = self._active_row(duplicate["id"])
                memory_id = row["id"]
                peers = self._peers(row["fact_key"], exclude=memory_id)
            conflict = self._confirmed_peer(peers) if row["cardinality"] == "single" else None
            status = MemoryStatus.CONFLICTED if conflict else MemoryStatus.CONFIRMED
            self._connection.execute(
                "UPDATE memories SET status=?, origin='manual', conflict_id=?, "
                "version=version+1, updated_at=? WHERE id=?",
                (status.value, None if conflict is None else conflict["id"],
                 self._now().isoformat(), memory_id),
            )
            self._sync_fts(memory_id)
            result = self.get(memory_id)
            assert result is not None
            return result

    def resolve_conflict(self, memory_id: str) -> MemoryRecord:
        """采用具体单值事实，不影响同类别其他偏好"""

        self._validate_uuid(memory_id, "记忆 ID")
        with self._write_transaction():
            self._ensure_open()
            row = self._active_row(memory_id)
            if row["status"] != MemoryStatus.CONFLICTED.value:
                raise MemoryConfigurationError("冲突记忆不存在或状态无效")
            replaced = self._connection.execute(
                "SELECT * FROM memories WHERE id=? AND status='confirmed'",
                (row["conflict_id"],),
            ).fetchone()
            if replaced is None:
                raise MemoryConfigurationError("冲突关联的原事实不存在或状态无效")
            self._relink_conflicts(replaced["id"], memory_id)
            self._erase(replaced)
            self._connection.execute(
                "UPDATE memories SET status='confirmed', origin='manual', conflict_id=NULL, "
                "version=version+1, updated_at=? WHERE id=?",
                (self._now().isoformat(), memory_id),
            )
            self._sync_fts(memory_id)
            result = self.get(memory_id)
            assert result is not None
            return result

    def edit(self, memory_id: str, content: str, expected_version: int) -> MemoryRecord:
        self._validate_uuid(memory_id, "记忆 ID")
        self._validate_text(content, 4096, "记忆正文")
        self._reject_secrets(content)
        if type(expected_version) is not int:
            raise MemoryConfigurationError("记忆版本无效")
        with self._write_transaction():
            self._ensure_open()
            row = self._active_row(memory_id)
            if row["version"] != expected_version:
                raise MemoryConfigurationError("记忆版本已变化，请刷新后编辑")
            sources = {row["source_turn_id"]}
            sources.update(item[0] for item in self._connection.execute(
                "SELECT source_turn_id FROM memory_evidence WHERE memory_id=?", (memory_id,),
            ))
            for source in sources:
                self._suppress(source, row["fact_key"], row["value"])
            edited_value = content.strip()[:1000]
            self._connection.execute(
                "UPDATE memories SET content=?, value=?, keywords_json='[]', origin='manual', "
                "version=version+1, updated_at=? WHERE id=?",
                (content, edited_value, self._now().isoformat(), memory_id),
            )
            self._sync_fts(memory_id)
            result = self.get(memory_id)
            assert result is not None
            return result

    def undo(self, change_id: str) -> bool:
        self._validate_uuid(change_id, "变更 ID")
        with self._write_transaction():
            self._ensure_open()
            change = self._connection.execute(
                "SELECT * FROM memory_changes WHERE id=?", (change_id,),
            ).fetchone()
            if change is None:
                raise MemoryConfigurationError("记忆变更不存在")
            if change["undone"]:
                return False
            row = self._active_row(change["memory_id"])
            if row["version"] != change["after_version"]:
                raise MemoryConfigurationError("记忆已经有后续修改，不能撤销旧版本")
            self._suppress(change["source_turn_id"], row["fact_key"], row["value"])
            if change["before_json"] is None:
                self._erase(row)
            else:
                before = json.loads(change["before_json"])
                # 只恢复可信本地快照字段，版本始终递增
                columns = (
                    "category", "content", "source_turn_id", "confidence", "sensitivity",
                    "expires_at", "status", "fact_key", "value", "cardinality",
                    "origin", "conflict_id", "keywords_json", "session_id",
                )
                assignment = ", ".join(f"{column}=?" for column in columns)
                self._connection.execute(
                    f"UPDATE memories SET {assignment}, version=version+1, updated_at=? WHERE id=?",
                    (*[before[column] for column in columns], self._now().isoformat(), row["id"]),
                )
                self._connection.execute(
                    "DELETE FROM memory_evidence WHERE memory_id=? AND source_turn_id=?",
                    (row["id"], change["source_turn_id"]),
                )
                self._sync_fts(row["id"])
            self._connection.execute(
                "UPDATE memory_changes SET undone=1, before_json=NULL WHERE id=?", (change_id,),
            )
            return True

    def list_changes(self, session_id: str | None = None) -> tuple[MemoryChange, ...]:
        if session_id is not None:
            self._validate_uuid(session_id, "会话 ID")
        with self._lock:
            self._ensure_open()
            sql = "SELECT * FROM memory_changes"
            parameters = ()
            if session_id is not None:
                sql += " WHERE session_id=?"
                parameters = (session_id,)
            rows = self._connection.execute(sql + " ORDER BY created_at, rowid", parameters)
            return tuple(self._change(row) for row in rows)

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
                "SELECT * FROM memories WHERE status != ? "
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at, rowid",
                (MemoryStatus.DELETED.value, self._now().isoformat()),
            ).fetchall()
            return tuple(self._record(row) for row in rows)

    def recall_basic(self, *, limit: int = 4) -> tuple[MemoryRecord, ...]:
        """用有界 SQL 读取有效且无冲突的受控基础偏好"""

        if type(limit) is not int or not 1 <= limit <= 4:
            raise MemoryConfigurationError("基础记忆召回数量无效")
        keys = tuple(sorted(SINGLE_VALUE_FACT_KEYS))
        placeholders = ",".join("?" for _ in keys)
        now = self._now().isoformat()
        sql = (
            f"SELECT m.* FROM memories m WHERE m.fact_key IN ({placeholders}) "
            "AND m.status='confirmed' "
            "AND (m.expires_at IS NULL OR m.expires_at>?) "
            "AND NOT EXISTS (SELECT 1 FROM memories c WHERE c.fact_key=m.fact_key "
            "AND c.status='conflicted' "
            "AND (c.expires_at IS NULL OR c.expires_at>?)) "
            "ORDER BY m.updated_at DESC,m.rowid DESC LIMIT ?"
        )
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(sql, (*keys, now, now, limit)).fetchall()
            return tuple(self._record(row) for row in rows)

    def recall_related(
        self,
        hints: Sequence[tuple[str, int]],
        *,
        limit: int = 10,
        exclude_source_ids: Sequence[str] = (),
        include_unmatched: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        """按字面相关性排序；组装提示时可补入未匹配事实供模型判断"""

        if type(limit) is not int or not 1 <= limit <= 100:
            raise MemoryConfigurationError("相关记忆召回数量无效")
        if type(include_unmatched) is not bool:
            raise MemoryConfigurationError("记忆召回模式无效")
        if isinstance(exclude_source_ids, (str, bytes, bytearray)) or not isinstance(
            exclude_source_ids, Sequence
        ):
            raise MemoryConfigurationError("记忆来源排除范围无效")
        excluded = tuple(exclude_source_ids)
        if len(excluded) > 100:
            raise MemoryConfigurationError("记忆来源排除范围过大")
        for source_id in excluded:
            self._validate_uuid(source_id, "排除来源轮次 ID")
        try:
            terms = query_terms(hints)
        except (TypeError, ValueError) as error:
            raise MemoryConfigurationError("记忆召回提示无效") from error
        if not terms and not include_unmatched:
            return ()
        long_terms = tuple(term for term, _ in terms if len(term) >= 3)
        short_terms = tuple(term for term, _ in terms if len(term) < 3)
        relevance: list[str] = []
        parameters: list[object] = []
        if long_terms:
            relevance.append(
                "m.id IN (SELECT id FROM memory_fts WHERE memory_fts MATCH ?)"
            )
            parameters.append(literal_fts_query(long_terms))
        for term in short_terms:
            relevance.append(
                "(m.content LIKE ? ESCAPE '\\' OR m.value LIKE ? ESCAPE '\\' "
                "OR m.keywords_json LIKE ? ESCAPE '\\')"
            )
            escaped = f"%{escape_like(term)}%"
            parameters.extend((escaped, escaped, escaped))
        score, score_parameters = sql_relevance(
            "m.content || ' ' || m.value || ' ' || m.keywords_json", terms,
        )
        for hint, weight in hints:
            text = hint[:MAX_HINT_CHARS].casefold()
            value_match = "(m.value!='' AND instr(?,lower(m.value))>0)"
            keyword_match = "EXISTS(SELECT 1 FROM json_each(m.keywords_json) k WHERE k.value!='' AND instr(?,lower(k.value))>0)"
            relevance.extend((value_match, keyword_match))
            parameters.extend((text, text))
            score += f" + CASE WHEN {value_match} THEN ? ELSE 0 END + CASE WHEN {keyword_match} THEN ? ELSE 0 END"
            score_parameters.extend((text, weight * 10, text, weight * 5))
        basic_keys = tuple(sorted(SINGLE_VALUE_FACT_KEYS))
        basic_placeholders = ",".join("?" for _ in basic_keys)
        now = self._now().isoformat()
        # 字面相似度只用于优先排序，不把模型尚未看到的事实判为无关。
        if include_unmatched:
            relevance = ["1"]
            parameters = []
        sql = (
            f"SELECT m.*,({score}) AS recall_score FROM memories m WHERE ("
            + " OR ".join(relevance)
            + ") AND m.status='confirmed' "
            f"AND m.fact_key NOT IN ({basic_placeholders}) "
            "AND (m.expires_at IS NULL OR m.expires_at>?) "
            "AND NOT EXISTS (SELECT 1 FROM memories c WHERE c.fact_key=m.fact_key "
            "AND c.status='conflicted' "
            "AND (c.expires_at IS NULL OR c.expires_at>?))"
        )
        parameters.extend((*basic_keys, now, now))
        if excluded:
            sql += f" AND m.source_turn_id NOT IN ({','.join('?' for _ in excluded)})"
            parameters.extend(excluded)
        sql += " ORDER BY recall_score DESC,m.updated_at DESC,m.rowid DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(sql, [*score_parameters, *parameters]).fetchall()
            except sqlite3.Error as error:
                raise MemoryStorageError("相关记忆召回失败") from error
            return tuple(self._record(row) for row in rows
                         if include_unmatched or row["recall_score"] > 0)

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
                "WHERE (m.content LIKE ? ESCAPE '\\' "
                "OR m.keywords_json LIKE ? ESCAPE '\\') "
                f"AND m.status IN ({placeholders}) "
                "AND (m.expires_at IS NULL OR m.expires_at > ?)"
            )
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters: list[object] = [
                f"%{escaped}%",
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
            literal_query = '"' + query.replace('"', '""') + '"'
            parameters = [literal_query, *(status.value for status in statuses), now]
            order = " ORDER BY bm25(memory_fts), m.updated_at DESC LIMIT ?"
        if category is not None:
            sql += " AND m.category = ?"
            parameters.append(category)
        if statuses == frozenset({MemoryStatus.CONFIRMED}):
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM memories c "
                "WHERE c.fact_key=m.fact_key AND c.cardinality='single' "
                "AND c.status='conflicted' AND (c.expires_at IS NULL OR c.expires_at > ?))"
            )
            parameters.append(now)
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
        with self._write_transaction():
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,),
            ).fetchone()
            if row is None or row["status"] == "deleted":
                return False
            self._erase(row)
            return True

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
        self, category: str, content: str, source_turn_id: str, confidence: float,
        sensitivity: MemorySensitivity, expires_at: datetime | None, status: MemoryStatus,
        fact_key: str | None = None, value: str | None = None,
        explicit_update: bool = False, session_id: str = "",
    ) -> MemoryRecord:
        self._validate_input(category, content, source_turn_id, confidence, expires_at)
        key = freeform_fact_key(content) if fact_key is None else fact_key
        fact_value = content if value is None else value
        self._validate_fact_fields(key, fact_value, content, ())
        self._reject_secrets(content, fact_value)
        if not isinstance(sensitivity, MemorySensitivity):
            raise MemoryConfigurationError("记忆敏感等级无效")
        if session_id:
            self._validate_uuid(session_id, "来源会话 ID")
        with self._write_transaction():
            self._ensure_open()
            self._ensure_writes_enabled()
            record, _ = self._save(
                category=category, content=content, source_turn_id=source_turn_id,
                confidence=confidence, sensitivity=sensitivity, expires_at=expires_at,
                status=status, fact_key=key, value=fact_value,
                explicit_update=explicit_update and self._is_correction(content),
                session_id=session_id, origin="explicit", quote=content, keywords=(),
            )
            if record is None:
                raise MemoryConfigurationError("该来源的记忆已删除或撤销")
            return record

    def _save(
        self, *, category, content, source_turn_id, confidence, sensitivity,
        expires_at, status, fact_key, value, explicit_update, session_id, origin, quote, keywords,
    ) -> tuple[MemoryRecord | None, MemoryChange | None]:
        """调用方持有写事务后才能执行事实身份判断"""

        fingerprint = fact_fingerprint(fact_key, value)
        suppressed = self._connection.execute(
            "SELECT 1 FROM memory_suppressions WHERE source_turn_id=? AND fact_fingerprint=?",
            (source_turn_id, fingerprint),
        ).fetchone()
        if suppressed:
            return None, None
        peers = self._peers(fact_key)
        duplicate = next((row for row in peers if normalize_fact_value(row["value"])
                          == normalize_fact_value(value)), None)
        single = fact_key in SINGLE_VALUE_FACT_KEYS
        current = self._confirmed_peer(peers) if single else None
        replacing_current = (
            single
            and current is not None
            and status is MemoryStatus.CONFIRMED
            and explicit_update
            and normalize_fact_value(current["value"]) != normalize_fact_value(value)
        )
        promote = (
            duplicate is not None
            and duplicate["status"] == "candidate"
            and status is MemoryStatus.CONFIRMED
            and not replacing_current
        )
        if duplicate is not None and not promote and not replacing_current:
            self._add_evidence(duplicate["id"], source_turn_id, quote)
            return self._record(duplicate), None
        now = self._now().isoformat()
        before_json = None
        conflict_id = None
        if replacing_current:
            memory_id = current["id"]
            before_json = json.dumps(dict(current), ensure_ascii=False)
            version = current["version"] + 1
            kind = "update"
            self._suppress_row_identity(current)
            if duplicate is not None and duplicate["id"] != memory_id:
                self._consolidate_duplicate(duplicate, memory_id)
        elif promote:
            memory_id = duplicate["id"]
            before_json = json.dumps(dict(duplicate), ensure_ascii=False)
            version = duplicate["version"] + 1
            kind = "update"
            if current is not None:
                status = MemoryStatus.CONFLICTED
                conflict_id = current["id"]
        elif current is not None and status is MemoryStatus.CONFIRMED:
            memory_id = str(uuid4())
            version = 1
            status = MemoryStatus.CONFLICTED
            conflict_id = current["id"]
            kind = "conflict"
        else:
            memory_id = str(uuid4())
            version = 1
            kind = "candidate" if status is MemoryStatus.CANDIDATE else "add"
        values = (
            memory_id, category, content, source_turn_id,
            json.loads(before_json)["created_at"] if before_json else now,
            now, confidence, sensitivity.value,
            None if expires_at is None else expires_at.astimezone(UTC).isoformat(),
            status.value, fact_key, value, "single" if single else "multi",
            origin, version, conflict_id, json.dumps(keywords, ensure_ascii=False), session_id,
        )
        self._connection.execute(
            "INSERT INTO memories (id,category,content,source_turn_id,created_at,updated_at,"
            "confidence,sensitivity,expires_at,status,fact_key,value,cardinality,origin,version,"
            "conflict_id,keywords_json,session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET category=excluded.category,content=excluded.content,"
            "source_turn_id=excluded.source_turn_id,updated_at=excluded.updated_at,"
            "confidence=excluded.confidence,sensitivity=excluded.sensitivity,"
            "expires_at=excluded.expires_at,status=excluded.status,value=excluded.value,"
            "origin=excluded.origin,version=excluded.version,conflict_id=excluded.conflict_id,"
            "keywords_json=excluded.keywords_json,session_id=excluded.session_id",
            values,
        )
        self._add_evidence(memory_id, source_turn_id, quote)
        self._sync_fts(memory_id)
        change = MemoryChange(str(uuid4()), memory_id, session_id, source_turn_id,
                              kind, version, datetime.fromisoformat(now), False)
        self._connection.execute(
            "INSERT INTO memory_changes "
            "(id,memory_id,session_id,source_turn_id,kind,before_json,after_version,created_at,undone) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (change.id, memory_id, session_id, source_turn_id, kind, before_json, version, now),
        )
        return self.get(memory_id), change

    def _peers(self, fact_key: str, *, exclude: str = "") -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM memories WHERE fact_key=? AND id!=? AND status!='deleted' "
            "AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at, rowid",
            (fact_key, exclude, self._now().isoformat()),
        ).fetchall()

    @staticmethod
    def _confirmed_peer(peers):
        return next((row for row in peers if row["status"] == "confirmed"), None)

    def _active_row(self, memory_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None or row["status"] == "deleted":
            raise MemoryConfigurationError("记忆不存在或已删除")
        if row["expires_at"] is not None and row["expires_at"] <= self._now().isoformat():
            raise MemoryConfigurationError("记忆已过期")
        return row

    def _add_evidence(self, memory_id: str, source_turn_id: str, quote: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_evidence(memory_id,source_turn_id,quote) VALUES (?,?,?)",
            (memory_id, source_turn_id, quote),
        )

    def _suppress(self, source_turn_id: str, fact_key: str, value: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_suppressions VALUES (?,?,?)",
            (source_turn_id, fact_fingerprint(fact_key, value), self._now().isoformat()),
        )

    def _suppress_row_identity(self, row: sqlite3.Row) -> None:
        """抑制事实当前值与所有已有来源的组合"""

        sources = {row["source_turn_id"]}
        sources.update(item[0] for item in self._connection.execute(
            "SELECT source_turn_id FROM memory_evidence WHERE memory_id=?",
            (row["id"],),
        ))
        for source_turn_id in sources:
            self._suppress(source_turn_id, row["fact_key"], row["value"])

    def _consolidate_duplicate(self, duplicate: sqlite3.Row, target_id: str) -> None:
        """转移重复事实证据并清除重复正文而不抑制有效来源"""

        self._connection.execute(
            "INSERT OR IGNORE INTO memory_evidence SELECT ?, source_turn_id, quote "
            "FROM memory_evidence WHERE memory_id=?",
            (target_id, duplicate["id"]),
        )
        self._relink_conflicts(duplicate["id"], target_id)
        self._connection.execute(
            "UPDATE memories SET category='',content='',value='',fact_key='',keywords_json='[]', "
            "source_turn_id='',session_id='',conflict_id=NULL,status='deleted', "
            "version=version+1,updated_at=? WHERE id=?",
            (self._now().isoformat(), duplicate["id"]),
        )
        self._connection.execute(
            "DELETE FROM memory_evidence WHERE memory_id=?",
            (duplicate["id"],),
        )
        self._connection.execute(
            "UPDATE memory_changes SET before_json=NULL WHERE memory_id=?",
            (duplicate["id"],),
        )
        self._connection.execute("DELETE FROM memory_fts WHERE id=?", (duplicate["id"],))

    def _relink_conflicts(self, previous_id: str, target_id: str) -> None:
        """更新冲突关联并使旧乐观版本立即失效"""

        self._connection.execute(
            "UPDATE memories SET conflict_id=?, version=version+1, updated_at=? "
            "WHERE conflict_id=? AND status='conflicted' AND id!=?",
            (target_id, self._now().isoformat(), previous_id, target_id),
        )

    def _erase(self, row: sqlite3.Row) -> None:
        """清除正文与所有历史副本，仅保留不可逆指纹防止旧来源重放"""

        versions = [dict(row)]
        versions.extend(
            json.loads(change[0]) for change in self._connection.execute(
                "SELECT before_json FROM memory_changes WHERE memory_id=? AND before_json IS NOT NULL",
                (row["id"],),
            )
        )
        sources = {item["source_turn_id"] for item in versions}
        sources.update(item[0] for item in self._connection.execute(
            "SELECT source_turn_id FROM memory_evidence WHERE memory_id=?", (row["id"],),
        ))
        for version in versions:
            for source in sources:
                self._suppress(source, version["fact_key"], version["value"])
        self._connection.execute(
            "UPDATE memories SET category='',content='',value='',fact_key='',keywords_json='[]', "
            "source_turn_id='',session_id='',conflict_id=NULL,status='deleted', "
            "version=version+1,updated_at=? WHERE id=?", (self._now().isoformat(), row["id"]),
        )
        self._connection.execute("DELETE FROM memory_evidence WHERE memory_id=?", (row["id"],))
        self._connection.execute(
            "UPDATE memory_changes SET before_json=NULL WHERE memory_id=?", (row["id"],),
        )
        self._connection.execute("DELETE FROM memory_fts WHERE id=?", (row["id"],))

    def _validate_source(self, evidence: FactEvidence, generation: int | None) -> None:
        if generation is None:
            return
        valid = self._connection.execute(
            "SELECT 1 FROM memory_sessions s JOIN session_turns t ON t.session_id=s.id "
            "WHERE s.id=? AND s.generation=? AND s.deleted=0 AND t.turn_id=? "
            "AND t.user_text=? AND t.expires_at>?",
            (evidence.session_id, generation, evidence.turn_id, evidence.user_text,
             self._now().isoformat()),
        ).fetchone()
        if valid is None:
            raise MemoryConfigurationError("来源会话或消息已失效")

    @staticmethod
    def _change(row: sqlite3.Row) -> MemoryChange:
        return MemoryChange(
            row["id"], row["memory_id"], row["session_id"], row["source_turn_id"],
            row["kind"], row["after_version"], datetime.fromisoformat(row["created_at"]),
            bool(row["undone"]),
        )

    @staticmethod
    def _validate_text(value: str, maximum: int, label: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise MemoryConfigurationError(f"{label}无效")

    def _validate_fact_fields(self, fact_key, value, quote, keywords) -> None:
        self._validate_text(fact_key, 128, "事实键")
        self._validate_text(value, 1000, "事实值")
        self._validate_text(quote, 4096, "引用")
        if not isinstance(keywords, (tuple, list)) or len(keywords) > 8:
            raise MemoryConfigurationError("记忆关键词无效")
        for keyword in keywords:
            self._validate_text(keyword, 64, "记忆关键词")

    def _reject_secrets(self, *texts: str) -> None:
        if any(self._policy.is_prohibited(text) for text in texts):
            raise MemoryConfigurationError("内容包含禁止保存的敏感信息")

    @staticmethod
    def _is_correction(content: str) -> bool:
        return is_explicit_correction(content)

    def _sync_fts(self, memory_id: str) -> None:
        self._connection.execute("DELETE FROM memory_fts WHERE id = ?", (memory_id,))
        self._connection.execute(
            """
            INSERT INTO memory_fts(id, content, keywords, category)
            SELECT id, content, keywords_json, category FROM memories
            WHERE id = ? AND status != ?
            """,
            (memory_id, MemoryStatus.DELETED.value),
        )


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
        except (ValueError, TypeError, AttributeError) as error:
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
            row["fact_key"], row["value"], row["cardinality"], row["origin"],
            row["version"], row["conflict_id"], tuple(json.loads(row["keywords_json"])),
            row["session_id"],
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryConfigurationError("记忆存储已关闭")

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """统一连接状态检查和数据库错误转换，禁止泄露 SQL 或正文"""

        with self._lock:
            self._ensure_open()
            try:
                with immediate_transaction(self._connection):
                    yield
            except sqlite3.Error as error:
                raise MemoryStorageError("记忆写入失败") from error
