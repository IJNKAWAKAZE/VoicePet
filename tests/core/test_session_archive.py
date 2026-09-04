from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

import core
from core.session_archive import (
    SessionArchiveError,
    SessionArchiveStore,
    SessionTurnRecord,
    ShortTermSummaryRecord,
)

NOW = datetime(2026, 9, 2, 23, 30, tzinfo=timezone(timedelta(hours=8)))


def test_session_archive_types_are_publicly_exported():
    assert core.SessionArchiveStore is SessionArchiveStore
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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retention_days": 0},
        {"retention_days": 366},
    ],
)
def test_archive_rejects_invalid_retention(tmp_path, kwargs):
    with pytest.raises(SessionArchiveError):
        SessionArchiveStore(tmp_path / "assistant.db", **kwargs)
