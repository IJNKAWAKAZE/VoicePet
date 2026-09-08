from __future__ import annotations

import sqlite3
from pathlib import Path

MEMORY_COMPONENT = "memory_system"
MEMORY_SCHEMA_VERSION = 2

_TARGET_TABLES = (
    "memories",
    "memory_fts",
    "memory_changes",
    "memory_suppressions",
    "memory_evidence",
    "memory_sessions",
    "session_turns",
    "session_turn_attachments",
    "short_term_summaries",
    "summary_suppressions",
    "memory_jobs",
    "memory_progress",
)

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS voicepet_components(
        name TEXT PRIMARY KEY,
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE memories(
        id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        content TEXT NOT NULL,
        source_turn_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confidence REAL NOT NULL,
        sensitivity TEXT NOT NULL,
        expires_at TEXT,
        status TEXT NOT NULL,
        fact_key TEXT NOT NULL DEFAULT '',
        value TEXT NOT NULL DEFAULT '',
        cardinality TEXT NOT NULL DEFAULT 'multi',
        origin TEXT NOT NULL DEFAULT 'explicit',
        version INTEGER NOT NULL DEFAULT 1,
        conflict_id TEXT,
        keywords_json TEXT NOT NULL DEFAULT '[]',
        session_id TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE VIRTUAL TABLE memory_fts USING fts5(
        id UNINDEXED,
        content,
        keywords,
        category UNINDEXED,
        tokenize='trigram'
    )
    """,
    """
    CREATE TABLE memory_changes(
        id TEXT PRIMARY KEY,
        memory_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        source_turn_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        before_json TEXT,
        after_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        undone INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE memory_suppressions(
        source_turn_id TEXT NOT NULL,
        fact_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(source_turn_id, fact_fingerprint)
    )
    """,
    """
    CREATE TABLE memory_evidence(
        memory_id TEXT NOT NULL,
        source_turn_id TEXT NOT NULL,
        quote TEXT NOT NULL,
        PRIMARY KEY(memory_id, source_turn_id)
    )
    """,
    """
    CREATE TABLE memory_sessions(
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE session_turns(
        id TEXT PRIMARY KEY,
        turn_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        user_text TEXT NOT NULL,
        assistant_text TEXT NOT NULL,
        session_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE session_turn_attachments(
        turn_id TEXT NOT NULL,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        url TEXT NOT NULL,
        media_type TEXT NOT NULL,
        kind TEXT NOT NULL,
        PRIMARY KEY(turn_id, path)
    )
    """,
    """
    CREATE TABLE short_term_summaries(
        id TEXT PRIMARY KEY,
        source_turn_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        source_turn_ids_json TEXT NOT NULL,
        topic TEXT NOT NULL,
        decisions_json TEXT NOT NULL DEFAULT '[]',
        unfinished_json TEXT NOT NULL,
        session_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE summary_suppressions(
        session_id TEXT NOT NULL,
        source_turn_id TEXT NOT NULL,
        source_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(session_id, source_turn_id)
    )
    """,
    """
    CREATE TABLE memory_jobs(
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        source_turn_ids_json TEXT NOT NULL,
        generation INTEGER NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_run_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        error TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE memory_progress(
        turn_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        extracted INTEGER NOT NULL DEFAULT 0,
        summarized INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_session_turns_session_created ON session_turns(session_id, created_at)",
    "CREATE INDEX idx_summaries_session_expires ON short_term_summaries(session_id, expires_at)",
    "CREATE INDEX idx_memories_fact_status ON memories(fact_key, status)",
    "CREATE INDEX idx_memory_changes_memory_created ON memory_changes(memory_id, created_at)",
    "CREATE INDEX idx_memory_jobs_status_next_run ON memory_jobs(status, next_run_at)",
)

_REQUIRED_COLUMNS = {
    "memories": {
        "id", "category", "content", "source_turn_id", "created_at", "updated_at",
        "confidence", "sensitivity", "expires_at", "status", "fact_key", "value",
        "cardinality", "origin", "version", "conflict_id", "keywords_json", "session_id",
    },
    "memory_fts": {"id", "content", "keywords", "category"},
    "memory_changes": {
        "id", "memory_id", "session_id", "source_turn_id", "kind", "before_json",
        "after_version", "created_at", "undone",
    },
    "memory_suppressions": {"source_turn_id", "fact_fingerprint", "created_at"},
    "memory_evidence": {"memory_id", "source_turn_id", "quote"},
    "memory_sessions": {"id", "title", "created_at", "updated_at", "generation", "deleted"},
    "session_turns": {
        "id", "turn_id", "session_id", "user_text", "assistant_text", "session_date",
        "created_at", "expires_at",
    },
    "short_term_summaries": {
        "id", "source_turn_id", "session_id", "source_turn_ids_json", "topic",
        "decisions_json", "unfinished_json", "session_date", "created_at", "updated_at",
        "expires_at",
    },
    "summary_suppressions": {
        "session_id", "source_turn_id", "source_date", "created_at",
    },
    "memory_jobs": {
        "id", "session_id", "source_turn_ids_json", "generation", "status", "attempts",
        "next_run_at", "created_at", "started_at", "error",
    },
    "memory_progress": {"turn_id", "session_id", "extracted", "summarized"},
}


class MemorySchemaError(RuntimeError):
    pass


def ensure_memory_schema(connection: sqlite3.Connection) -> None:
    """创建或核对独立版本化的记忆结构"""
    if connection.in_transaction:
        raise MemorySchemaError("调用方事务进行中，拒绝初始化记忆结构")

    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_memory_schema(connection)
        connection.commit()
    except MemorySchemaError:
        _rollback_if_needed(connection)
        raise
    except sqlite3.Error as error:
        _rollback_if_needed(connection)
        raise MemorySchemaError("记忆结构初始化失败") from error


def reset_test_memory_data(path: str | Path) -> dict[str, int]:
    """显式清理测试数据库中的记忆逻辑表"""
    database_path = Path(path).expanduser().resolve()
    if not database_path.exists() or not database_path.is_file():
        raise MemorySchemaError("测试数据库路径必须是已存在的普通文件")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        counts = _existing_table_counts(connection)
        for table_name in _TARGET_TABLES:
            if _table_exists(connection, table_name):
                connection.execute(f"DROP TABLE {table_name}")
        _create_schema(connection)
        connection.commit()
        return counts
    except MemorySchemaError:
        if connection is not None:
            _rollback_if_needed(connection)
        raise
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            _rollback_if_needed(connection)
        raise MemorySchemaError("测试记忆数据重置失败") from error
    finally:
        if connection is not None:
            connection.close()


def _ensure_memory_schema(connection: sqlite3.Connection) -> None:
    component_version = _component_version(connection)
    target_exists = any(_table_exists(connection, table_name) for table_name in _TARGET_TABLES)
    if component_version is None and target_exists:
        raise MemorySchemaError("检测到旧记忆结构，请显式执行一次性测试数据重置")
    if component_version is not None and component_version != MEMORY_SCHEMA_VERSION:
        raise MemorySchemaError("记忆结构组件版本未知")
    if component_version == MEMORY_SCHEMA_VERSION:
        _validate_schema(connection)
        return

    _create_schema(connection)


def _component_version(connection: sqlite3.Connection) -> int | None:
    if not _table_exists(connection, "voicepet_components"):
        return None
    row = connection.execute(
        "SELECT version FROM voicepet_components WHERE name = ?", (MEMORY_COMPONENT,)
    ).fetchone()
    if row is None:
        return None
    if type(row[0]) is not int:
        raise MemorySchemaError("记忆结构组件版本未知")
    return row[0]


def _create_schema(connection: sqlite3.Connection) -> None:
    for statement in _DDL:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO voicepet_components(name, version) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET version = excluded.version",
        (MEMORY_COMPONENT, MEMORY_SCHEMA_VERSION),
    )


def _validate_schema(connection: sqlite3.Connection) -> None:
    for table_name, required_columns in _REQUIRED_COLUMNS.items():
        if not _table_exists(connection, table_name):
            raise MemorySchemaError("记忆结构缺少必要数据表")
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")
        }
        if not required_columns.issubset(columns):
            raise MemorySchemaError("记忆结构缺少必要字段")


def _existing_table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table_name: connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        for table_name in _TARGET_TABLES
        if _table_exists(connection, table_name)
    }


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (table_name,)
    ).fetchone() is not None


def _rollback_if_needed(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()
