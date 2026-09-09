from __future__ import annotations

import sqlite3
import threading

import pytest

from core.memory_schema import (
    MemorySchemaError,
    ensure_memory_schema,
    reset_test_memory_data,
)


def test_initializer_preserves_unrelated_data(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)
    connection.execute("CREATE TABLE audit_probe(value TEXT)")
    connection.execute("INSERT INTO audit_probe VALUES ('keep')")

    ensure_memory_schema(connection)
    ensure_memory_schema(connection)

    assert connection.execute("SELECT value FROM audit_probe").fetchone() == ("keep",)


def test_initializer_preserves_target_records_after_reopen(tmp_path):
    path = tmp_path / "assistant.db"
    connection = sqlite3.connect(path, isolation_level=None)
    ensure_memory_schema(connection)
    connection.execute(
        "INSERT INTO memories "
        "(id, category, content, source_turn_id, created_at, updated_at, confidence, sensitivity, "
        "expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("memory-1", "fact", "Ada likes tea", "turn-1", "now", "now", 1.0, "private", None, "active"),
    )
    connection.close()

    reopened = sqlite3.connect(path, isolation_level=None)
    ensure_memory_schema(reopened)

    assert reopened.execute("SELECT content FROM memories").fetchone() == ("Ada likes tea",)


def test_initializer_rejects_legacy_target_table_without_changing_data(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)
    connection.execute("CREATE TABLE memories(id TEXT PRIMARY KEY, content TEXT NOT NULL)")
    connection.execute("INSERT INTO memories VALUES ('legacy-1', 'keep me')")

    with pytest.raises(MemorySchemaError, match="显式.*重置"):
        ensure_memory_schema(connection)

    assert connection.execute("SELECT content FROM memories").fetchone() == ("keep me",)


def test_initializer_rejects_unknown_component_version(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)
    connection.execute(
        "CREATE TABLE voicepet_components(name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    connection.execute("INSERT INTO voicepet_components VALUES ('memory_system', 99)")

    with pytest.raises(MemorySchemaError, match="未知"):
        ensure_memory_schema(connection)


@pytest.mark.parametrize("version", [2.5, "invalid"])
def test_initializer_rejects_malformed_component_version_and_rolls_back(tmp_path, version):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)
    connection.execute(
        "CREATE TABLE voicepet_components(name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    connection.execute("INSERT INTO voicepet_components VALUES ('memory_system', ?)", (version,))
    connection.execute("CREATE TABLE audit_probe(value TEXT)")
    connection.execute("INSERT INTO audit_probe VALUES ('keep')")

    with pytest.raises(MemorySchemaError, match="未知"):
        ensure_memory_schema(connection)

    assert not connection.in_transaction
    assert connection.execute(
        "SELECT version FROM voicepet_components WHERE name = 'memory_system'"
    ).fetchone() == (version,)
    assert connection.execute("SELECT value FROM audit_probe").fetchone() == ("keep",)


def test_initializer_rejects_missing_required_field(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)
    ensure_memory_schema(connection)
    connection.execute("ALTER TABLE memories DROP COLUMN value")

    with pytest.raises(MemorySchemaError, match="缺少"):
        ensure_memory_schema(connection)


def test_initializer_preserves_shared_user_version(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)
    connection.execute("PRAGMA user_version = 9")

    ensure_memory_schema(connection)

    assert connection.execute("PRAGMA user_version").fetchone() == (9,)


def test_initializer_creates_exact_memory_and_summary_column_contracts(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)

    ensure_memory_schema(connection)

    memories = [
        (row[1], row[2], row[3], row[4], row[5])
        for row in connection.execute("PRAGMA table_info(memories)")
    ]
    summaries = [
        (row[1], row[2], row[3], row[4], row[5])
        for row in connection.execute("PRAGMA table_info(short_term_summaries)")
    ]
    assert memories == [
        ("id", "TEXT", 0, None, 1),
        ("category", "TEXT", 1, None, 0),
        ("content", "TEXT", 1, None, 0),
        ("source_turn_id", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("confidence", "REAL", 1, None, 0),
        ("sensitivity", "TEXT", 1, None, 0),
        ("expires_at", "TEXT", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("fact_key", "TEXT", 1, "''", 0),
        ("value", "TEXT", 1, "''", 0),
        ("cardinality", "TEXT", 1, "'multi'", 0),
        ("origin", "TEXT", 1, "'explicit'", 0),
        ("version", "INTEGER", 1, "1", 0),
        ("conflict_id", "TEXT", 0, None, 0),
        ("keywords_json", "TEXT", 1, "'[]'", 0),
        ("session_id", "TEXT", 1, "''", 0),
    ]
    assert summaries == [
        ("id", "TEXT", 0, None, 1),
        ("source_turn_id", "TEXT", 1, None, 0),
        ("session_id", "TEXT", 1, None, 0),
        ("source_turn_ids_json", "TEXT", 1, None, 0),
        ("topic", "TEXT", 1, None, 0),
        ("decisions_json", "TEXT", 1, "'[]'", 0),
        ("unfinished_json", "TEXT", 1, None, 0),
        ("session_date", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("expires_at", "TEXT", 1, None, 0),
    ]


def test_initializer_creates_exact_memory_job_column_contract(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)

    ensure_memory_schema(connection)

    jobs = [
        (row[1], row[2], row[3], row[4], row[5])
        for row in connection.execute("PRAGMA table_info(memory_jobs)")
    ]
    assert jobs == [
        ("id", "TEXT", 0, None, 1),
        ("session_id", "TEXT", 1, None, 0),
        ("source_turn_ids_json", "TEXT", 1, None, 0),
        ("generation", "INTEGER", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("attempts", "INTEGER", 1, "0", 0),
        ("next_run_at", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("started_at", "TEXT", 0, None, 0),
        ("error", "TEXT", 1, "''", 0),
    ]


def test_initializer_preserves_original_source_date_for_summary_suppressions(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)

    ensure_memory_schema(connection)

    columns = [
        (row[1], row[2], row[3], row[4], row[5])
        for row in connection.execute("PRAGMA table_info(summary_suppressions)")
    ]
    assert columns == [
        ("session_id", "TEXT", 1, None, 1),
        ("source_turn_id", "TEXT", 1, None, 2),
        ("source_date", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ]
    connection.close()


def test_reset_removes_only_target_data_and_preserves_audit_data(tmp_path):
    path = tmp_path / "assistant.db"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE audit_probe(value TEXT)")
    connection.execute("INSERT INTO audit_probe VALUES ('keep')")
    ensure_memory_schema(connection)
    connection.execute(
        "INSERT INTO memories "
        "(id, category, content, source_turn_id, created_at, updated_at, confidence, sensitivity, "
        "expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("memory-1", "fact", "remove me", "turn-1", "now", "now", 1.0, "private", None, "active"),
    )
    connection.close()

    counts = reset_test_memory_data(path)
    reopened = sqlite3.connect(path, isolation_level=None)

    assert counts["memories"] == 1
    assert reopened.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)
    assert reopened.execute("SELECT value FROM audit_probe").fetchone() == ("keep",)


def test_initializer_rolls_back_all_ddl_when_sqlite_authorizer_rejects_a_statement(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)

    def deny_create_table(action, _arg1, _arg2, _database, _source):
        if action == sqlite3.SQLITE_CREATE_TABLE:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_create_table)
    with pytest.raises(MemorySchemaError):
        ensure_memory_schema(connection)
    connection.set_authorizer(None)

    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'voicepet_components'"
    ).fetchone() is None


def test_reset_rolls_back_when_sqlite_authorizer_rejects_a_statement(tmp_path):
    path = tmp_path / "assistant.db"
    connection = sqlite3.connect(path, isolation_level=None)
    ensure_memory_schema(connection)
    connection.execute(
        "INSERT INTO memories "
        "(id, category, content, source_turn_id, created_at, updated_at, confidence, sensitivity, "
        "expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("memory-1", "fact", "keep me", "turn-1", "now", "now", 1.0, "private", None, "active"),
    )

    def deny_create_table(action, _arg1, _arg2, _database, _source):
        if action == sqlite3.SQLITE_CREATE_TABLE:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_create_table)
    connection.close()

    # 重置工具使用独立连接，作者器通过连接工厂注入以验证真实 SQLite 回滚
    original_connect = sqlite3.connect

    def authorized_connect(*args, **kwargs):
        tool_connection = original_connect(*args, **kwargs)
        tool_connection.set_authorizer(deny_create_table)
        return tool_connection

    sqlite3.connect = authorized_connect
    try:
        with pytest.raises(MemorySchemaError):
            reset_test_memory_data(path)
    finally:
        sqlite3.connect = original_connect

    reopened = sqlite3.connect(path, isolation_level=None)
    assert reopened.execute("SELECT content FROM memories").fetchone() == ("keep me",)


def test_initializer_rejects_an_existing_transaction_without_ending_it(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db")
    connection.execute("CREATE TABLE audit_probe(value TEXT)")
    connection.execute("INSERT INTO audit_probe VALUES ('keep')")

    with pytest.raises(MemorySchemaError, match="事务"):
        ensure_memory_schema(connection)

    assert connection.in_transaction
    connection.rollback()


@pytest.mark.parametrize("target", ["missing.db", "directory"])
def test_reset_rejects_non_file_paths_without_creating_database(tmp_path, target):
    path = tmp_path / target
    if target == "directory":
        path.mkdir()

    with pytest.raises(MemorySchemaError):
        reset_test_memory_data(path)

    assert not path.exists() if target == "missing.db" else path.is_dir()


def test_initializer_keeps_callers_row_factory(tmp_path):
    connection = sqlite3.connect(tmp_path / "assistant.db", isolation_level=None)
    connection.row_factory = sqlite3.Row

    ensure_memory_schema(connection)

    row = connection.execute("SELECT name FROM voicepet_components").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["name"] == "memory_system"


def test_concurrent_empty_database_initializers_create_one_valid_schema(tmp_path):
    path = tmp_path / "assistant.db"
    sqlite3.connect(path).close()
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def initialize() -> None:
        connection = sqlite3.connect(path, isolation_level=None, timeout=5)
        try:
            barrier.wait()
            ensure_memory_schema(connection)
        except (MemorySchemaError, sqlite3.Error, threading.BrokenBarrierError) as error:
            errors.append(error)
        finally:
            connection.close()

    first = threading.Thread(target=initialize)
    second = threading.Thread(target=initialize)
    first.start()
    second.start()
    first.join()
    second.join()

    assert errors == []
    connection = sqlite3.connect(path, isolation_level=None)
    assert connection.execute(
        "SELECT version FROM voicepet_components WHERE name = 'memory_system'"
     ).fetchone() == (3,)
