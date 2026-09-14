import json
import sqlite3
from uuid import uuid4

import pytest

import core.session_archive as session_archive_module
from core.memory import MemoryStore
from core.memory_schema import MEMORY_SCHEMA_VERSION
from core.session_archive import SessionArchiveError, SessionArchiveStore


@pytest.mark.parametrize("first", ["archive", "memory"])
def test_memory_and_archive_stores_share_schema_in_either_open_order(tmp_path, first):
    database = tmp_path / "assistant.db"
    if first == "archive":
        first_store = SessionArchiveStore(database)
        second_store = MemoryStore(database)
    else:
        first_store = MemoryStore(database)
        second_store = SessionArchiveStore(database)

    connection = sqlite3.connect(database)
    component = connection.execute(
        "SELECT version FROM voicepet_components WHERE name = 'memory_system'"
    ).fetchone()
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(short_term_summaries)")
    }

    assert component == (MEMORY_SCHEMA_VERSION,)
    assert {"session_id", "source_turn_ids_json", "decisions_json"} <= columns
    connection.close()
    second_store.close()
    first_store.close()


def test_summary_insert_names_v2_columns_and_preserves_source_identity(tmp_path):
    database = tmp_path / "assistant.db"
    store = SessionArchiveStore(database)
    session_id = str(uuid4())
    turn_id = str(uuid4())
    store.archive_turn(turn_id, "问题", "回答", session_id)

    store.save_summary(turn_id, "发布计划", ("完成测试",))

    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT session_id, source_turn_ids_json, decisions_json "
        "FROM short_term_summaries WHERE source_turn_id = ?",
        (turn_id,),
    ).fetchone()
    assert row == (session_id, json.dumps([turn_id]), json.dumps([]))
    connection.close()
    store.close()


def test_archive_schema_failure_closes_connection_and_reports_safe_error(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "assistant.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE voicepet_components(name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO voicepet_components(name, version) VALUES ('memory_system', 99)"
    )
    connection.commit()
    connection.close()

    real_connect = sqlite3.connect
    opened = []

    class TrackingConnection(sqlite3.Connection):
        was_closed = False

        def close(self):
            self.was_closed = True
            super().close()

    def tracking_connect(*args, **kwargs):
        tracked = real_connect(*args, **kwargs, factory=TrackingConnection)
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(session_archive_module.sqlite3, "connect", tracking_connect)

    with pytest.raises(SessionArchiveError, match="结构初始化失败"):
        SessionArchiveStore(database)

    assert len(opened) == 1
    assert opened[0].was_closed is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
