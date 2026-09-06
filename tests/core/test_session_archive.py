import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

import core
from core.session_archive import (
    SessionArchiveError,
    SessionArchiveStore,
    SessionRecord,
    SessionTurnRecord,
    ShortTermSummaryRecord,
)

NOW = datetime(2026, 9, 2, 23, 30, tzinfo=timezone(timedelta(hours=8)))


def test_session_archive_types_are_publicly_exported():
    assert core.SessionArchiveStore is SessionArchiveStore
    assert core.SessionRecord is SessionRecord
    assert core.SessionTurnRecord is SessionTurnRecord
    assert core.ShortTermSummaryRecord is ShortTermSummaryRecord


def test_archive_is_immutable_idempotent_and_reopens_from_shared_database(tmp_path):
    database = tmp_path / "assistant.db"
    turn_id = str(uuid4())
    store = SessionArchiveStore(database, clock=lambda: NOW)

    first = store.archive_turn(turn_id, "第一问", "第一答")
    second = store.archive_turn(turn_id, "不会覆盖", "不会覆盖")

    assert first == second
    assert first.session_date == date(2026, 9, 2)
    assert first.session_id == turn_id
    assert first.expires_at == NOW.astimezone(UTC) + timedelta(days=7)
    with pytest.raises(FrozenInstanceError):
        first.user_text = "changed"
    assert store.list_turns() == (first,)
    store.close()

    reopened = SessionArchiveStore(database, clock=lambda: NOW)
    assert reopened.list_turns() == (first,)
    reopened.close()


def test_summary_is_structured_and_upserts_by_source_turn(tmp_path):
    store = SessionArchiveStore(tmp_path / "assistant.db", clock=lambda: NOW)
    turn_id = str(uuid4())
    store.archive_turn(turn_id, "帮我规划发布", "先完成测试")

    first = store.save_summary(turn_id, "发布计划", ("完成测试", "打包"))
    updated = store.save_summary(turn_id, "发布准备", ("打包",))

    assert isinstance(first, ShortTermSummaryRecord)
    assert updated.id == first.id
    assert updated.topic == "发布准备"
    assert updated.unfinished_items == ("打包",)
    assert store.list_summaries() == (updated,)
    store.close()


def test_clear_date_hard_deletes_only_target_day_turns_and_summaries(tmp_path):
    current = [NOW]
    store = SessionArchiveStore(
        tmp_path / "assistant.db",
        clock=lambda: current[0],
    )
    first_turn = str(uuid4())
    second_turn = str(uuid4())
    store.archive_turn(first_turn, "第一天", "回复")
    store.save_summary(first_turn, "第一天话题", ())
    current[0] = NOW + timedelta(days=1)
    store.archive_turn(second_turn, "第二天", "回复")
    store.save_summary(second_turn, "第二天话题", ())

    affected = store.clear_date(date(2026, 9, 3))

    assert affected == 2
    assert [record.turn_id for record in store.list_turns()] == [first_turn]
    assert [record.source_turn_id for record in store.list_summaries()] == [
        first_turn
    ]
    store.close()


def test_expired_rows_are_purged_and_disabled_store_rejects_writes(tmp_path):
    current = [NOW]
    database = tmp_path / "assistant.db"
    store = SessionArchiveStore(
        database,
        retention_days=1,
        clock=lambda: current[0],
    )
    turn_id = str(uuid4())
    store.archive_turn(turn_id, "问题", "回复")
    store.save_summary(turn_id, "话题", ())
    current[0] = NOW + timedelta(days=2)

    assert store.purge_expired() == 2
    assert store.list_turns() == ()
    assert store.list_summaries() == ()
    store.close()

    disabled = SessionArchiveStore(database, enabled=False)
    with pytest.raises(SessionArchiveError, match="关闭"):
        disabled.archive_turn(str(uuid4()), "问题", "回复")
    disabled.close()


def test_clear_all_hard_deletes_turns_and_summaries(tmp_path):
    store = SessionArchiveStore(tmp_path / "assistant.db", clock=lambda: NOW)
    turn_id = str(uuid4())
    store.archive_turn(turn_id, "问题", "回复")
    store.save_summary(turn_id, "话题", ("待办",))

    assert store.clear_all() == 2
    assert store.list_turns() == ()
    assert store.list_summaries() == ()
    assert store.clear_all() == 0
    store.close()


def test_multiple_turns_are_grouped_into_one_session_with_details(tmp_path):
    current = [NOW]
    store = SessionArchiveStore(
        tmp_path / "assistant.db",
        clock=lambda: current[0],
    )
    session_id = str(uuid4())
    first = store.archive_turn(str(uuid4()), "第一问", "第一答", session_id)
    current[0] += timedelta(minutes=2)
    second = store.archive_turn(str(uuid4()), "第二问", "第二答", session_id)

    sessions = store.list_sessions()

    assert sessions == (
        SessionRecord(
            session_id,
            "第一问",
            2,
            first.created_at,
            second.created_at,
            "第二问",
            "第二答",
        ),
    )
    assert store.list_session_turns(session_id) == (first, second)
    assert store.discard_session(session_id) is True
    assert store.list_sessions() == ()
    store.close()


def test_legacy_turn_rows_are_rejected_without_modifying_database(tmp_path):
    database = tmp_path / "assistant.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE session_turns (id TEXT PRIMARY KEY, "
        "turn_id TEXT NOT NULL UNIQUE, user_text TEXT NOT NULL, "
        "assistant_text TEXT NOT NULL, session_date TEXT NOT NULL, "
        "created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    created = NOW.astimezone(UTC).isoformat()
    expires = (NOW.astimezone(UTC) + timedelta(days=7)).isoformat()
    for index in range(2):
        connection.execute(
            "INSERT INTO session_turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                str(uuid4()),
                f"问题{index}",
                f"回答{index}",
                NOW.date().isoformat(),
                created,
                expires,
            ),
        )
    connection.commit()
    connection.close()
    original_bytes = database.read_bytes()

    with pytest.raises(SessionArchiveError, match="结构"):
        SessionArchiveStore(database, clock=lambda: NOW)

    assert database.read_bytes() == original_bytes
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT user_text, assistant_text FROM session_turns ORDER BY rowid"
    ).fetchall() == [("问题0", "回答0"), ("问题1", "回答1")]
    connection.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retention_days": 0},
        {"retention_days": 366},
        {"retention_days": True},
        {"summary_retention_days": 0},
        {"summary_retention_days": 366},
        {"summary_retention_days": False},
    ],
)
def test_archive_rejects_invalid_retention(tmp_path, kwargs):
    with pytest.raises(SessionArchiveError):
        SessionArchiveStore(tmp_path / "assistant.db", **kwargs)
