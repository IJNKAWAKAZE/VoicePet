import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from core.memory import MemoryStore
from core.session_archive import SessionArchiveError, SessionArchiveStore
from core.session_context import SessionContext
from core.session_data import SessionDataManager

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


@pytest.fixture
def archive(tmp_path):
    current = [NOW]
    store = SessionArchiveStore(tmp_path / "test.db", retention_days=1,
                                summary_retention_days=7, clock=lambda: current[0])
    yield store, current
    store.close()


def add_turn(store, session_id=None, text="规划发布"):
    return store.archive_turn(str(uuid4()), text, "先完成测试", session_id or str(uuid4()))


def test_expired_chat_keeps_summary_but_explicit_session_delete_removes_it(archive):
    store, current = archive
    turn = add_turn(store)
    summary = store.save_summary(turn.turn_id, "发布准备", ("测试",), decisions=("使用 QML",))
    current[0] += timedelta(days=2)
    assert store.list_turns() == ()
    assert store.list_session_turns(turn.session_id) == ()
    assert store.list_summaries(turn.session_id) == (summary,)
    assert store.list_sessions()[0].id == turn.session_id
    assert store.purge_expired() == 1
    assert store.list_summaries(turn.session_id) == (summary,)
    assert store.discard_session(turn.session_id)
    assert store.list_summaries() == ()
    with pytest.raises(SessionArchiveError):
        add_turn(store, turn.session_id)


def test_retry_does_not_extend_summary_expiry_and_new_config_only_affects_new_sources(archive):
    store, current = archive
    first = add_turn(store)
    summary = store.save_summary(first.turn_id, "发布", ())
    current[0] += timedelta(hours=12)
    store.configure(enabled=True, retention_days=1, summary_retention_days=3)
    retried = store.save_summary(first.turn_id, "发布准备", ("打包",))
    assert retried.id == summary.id
    assert retried.expires_at == NOW + timedelta(days=7)
    later = add_turn(store, first.session_id)
    new = store.save_summary(later.turn_id, "发布验收", ())
    assert new.expires_at == current[0] + timedelta(days=3)


def test_summary_rejects_cross_session_sources_and_stale_generation(archive):
    store, _ = archive
    first, second = add_turn(store), add_turn(store)
    with pytest.raises(SessionArchiveError):
        store.save_summary(second.turn_id, "混合", (), source_turn_ids=(first.turn_id, second.turn_id))
    generation = store.session_generation(first.session_id)
    assert store.sources_valid(first.session_id, (first.turn_id,), generation)
    store.clear_all()
    assert not store.sources_valid(first.session_id, (first.turn_id,), generation)
    with pytest.raises(SessionArchiveError):
        store.save_summary(first.turn_id, "迟到", (), expected_generation=generation)


def test_deleted_summary_suppresses_each_source_but_allows_new_turn(archive):
    store, _ = archive
    first = add_turn(store)
    second = add_turn(store, first.session_id)
    summary = store.save_summary(second.turn_id, "发布", (), source_turn_ids=(first.turn_id, second.turn_id))
    assert store.delete_summary(summary.id)
    assert not store.delete_summary(summary.id)
    for source in (first, second):
        with pytest.raises(SessionArchiveError):
            store.save_summary(source.turn_id, "重复", ())
    third = add_turn(store, first.session_id)
    assert store.save_summary(third.turn_id, "新计划", ()).source_turn_ids == (third.turn_id,)


def test_clear_date_removes_cross_day_summary_range_but_preserves_long_term(tmp_path):
    current = [NOW]
    path = tmp_path / "date.db"
    store = SessionArchiveStore(path, retention_days=7, summary_retention_days=7, clock=lambda: current[0])
    memories = MemoryStore(path, clock=lambda: current[0])
    try:
        first = add_turn(store)
        current[0] += timedelta(days=1)
        second = add_turn(store, first.session_id)
        other = add_turn(store)
        store.save_summary(second.turn_id, "跨日发布", (), source_turn_ids=(first.turn_id, second.turn_id))
        kept = store.save_summary(other.turn_id, "独立话题", ())
        memory = memories.create_confirmed(category="preference", content="喜欢猫", source_turn_id=first.turn_id)
        assert store.clear_date(NOW.date()) == 2
        assert store.list_summaries() == (kept,)
        assert memories.get(memory.id).content == "喜欢猫"
    finally:
        memories.close()
        store.close()


def test_summary_only_session_can_be_activated_and_resumed(archive):
    store, current = archive
    turn = add_turn(store)
    store.save_summary(turn.turn_id, "发布", ())
    current[0] += timedelta(days=2)
    manager = SessionDataManager(store, SessionContext())
    assert manager.activate(turn.session_id) == ()
    assert manager.current_session_id == turn.session_id
    assert manager.resume_latest() == ()


def test_summary_source_metadata_survives_restart_and_reports_expiry(tmp_path):
    path = tmp_path / "reopen.db"
    store = SessionArchiveStore(path, retention_days=1, summary_retention_days=7, clock=lambda: NOW)
    turn = add_turn(store, text="发布测试计划")
    store.save_summary(turn.turn_id, "发布准备", ())
    store.close()
    reopened = SessionArchiveStore(path, clock=lambda: NOW + timedelta(days=2))
    try:
        manager = SessionDataManager(reopened, SessionContext())
        assert manager.list_summaries()[0].source_title == "发布测试计划"
        label = manager.source_label(turn.turn_id)
        assert label["title"] == "发布测试计划"
        assert label["status"] == "expired"
    finally:
        reopened.close()


@pytest.mark.parametrize("kwargs", [
    {"unfinished_items": "打包"}, {"decisions": "使用 QML"}, {"decisions": [""]},
    {"decisions": ["x"] * 11}, {"topic": ""}, {"topic": "x" * 501},
    {"decisions": ["我的密码是 synthetic-secret"]},
])
def test_direct_summary_store_enforces_bounds_and_secret_policy(archive, kwargs):
    store, _ = archive
    turn = add_turn(store)
    options = {"topic": "发布", "unfinished_items": (), "decisions": ()}
    options.update(kwargs)
    with pytest.raises(SessionArchiveError):
        store.save_summary(turn.turn_id, **options)
    assert store.list_summaries() == ()


def test_turn_id_cannot_be_reassigned_to_another_session(archive):
    store, _ = archive
    turn = add_turn(store)
    with pytest.raises(SessionArchiveError):
        store.archive_turn(turn.turn_id, "另一会话", "答复", str(uuid4()))


def test_discarded_turn_id_cannot_be_reassigned_and_deleted_label_is_minimal(archive):
    store, current = archive
    turn = add_turn(store, text="不能保留的标题")
    store.save_summary(turn.turn_id, "发布", ())
    current[0] += timedelta(hours=1)

    assert store.discard_session(turn.session_id) is True
    assert store.source_label(turn.turn_id) == {
        "title": "",
        "status": "deleted",
        "created_at": turn.created_at,
        "session_id": turn.session_id,
    }
    with pytest.raises(SessionArchiveError):
        store.archive_turn(turn.turn_id, "伪新会话", "答复", str(uuid4()))


def test_purge_expired_summary_cleans_its_progress_and_job_references(tmp_path):
    current = [NOW]
    store = SessionArchiveStore(
        tmp_path / "purge.db",
        retention_days=7,
        summary_retention_days=1,
        clock=lambda: current[0],
    )
    try:
        turn = add_turn(store)
        store.save_summary(turn.turn_id, "发布", ())
        store._connection.execute(
            "INSERT INTO memory_progress(turn_id,session_id,extracted,summarized) "
            "VALUES (?,?,1,1)",
            (turn.turn_id, turn.session_id),
        )
        store._connection.execute(
            "INSERT INTO memory_jobs(id,session_id,source_turn_ids_json,generation,"
            "status,next_run_at,created_at,started_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                turn.session_id,
                json.dumps([turn.turn_id]),
                0,
                "running",
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        current[0] += timedelta(days=2)

        assert store.purge_expired() == 1
        assert store.list_turns() == (turn,)
        assert store._connection.execute("SELECT * FROM memory_progress").fetchall() == []
        job = store._connection.execute(
            "SELECT status,started_at FROM memory_jobs"
        ).fetchone()
        assert tuple(job) == ("cancelled", NOW.isoformat())
    finally:
        store.close()


def test_clear_all_invalidates_session_whose_expired_body_was_already_purged(tmp_path):
    current = [NOW]
    store = SessionArchiveStore(
        tmp_path / "purged-session.db", retention_days=1, clock=lambda: current[0]
    )
    try:
        turn = add_turn(store)
        current[0] += timedelta(days=2)
        assert store.purge_expired() == 1
        assert store.clear_all() == 0
        assert store.session_generation(turn.session_id) == 1
        with pytest.raises(SessionArchiveError):
            add_turn(store, turn.session_id)
    finally:
        store.close()


def test_clear_date_removes_summary_using_purged_sources_original_local_date(tmp_path):
    local_zone = timezone(timedelta(hours=-8))
    start = datetime(2026, 9, 6, 23, 30, tzinfo=local_zone)
    current = [start]
    store = SessionArchiveStore(
        tmp_path / "purged-date.db",
        retention_days=1,
        summary_retention_days=7,
        clock=lambda: current[0],
    )
    try:
        first = add_turn(store)
        current[0] += timedelta(hours=12)
        second = add_turn(store, first.session_id)
        unrelated = add_turn(store)
        store.save_summary(
            second.turn_id,
            "跨日发布",
            (),
            source_turn_ids=(first.turn_id, second.turn_id),
        )
        kept = store.save_summary(unrelated.turn_id, "另一日期", ())
        current[0] += timedelta(hours=13)

        assert store.purge_expired() == 1
        store.close()
        changed_zone_now = current[0].astimezone(timezone(timedelta(hours=8)))
        store = SessionArchiveStore(
            tmp_path / "purged-date.db",
            retention_days=1,
            summary_retention_days=7,
            clock=lambda: changed_zone_now,
        )

        assert store.clear_date(first.session_date) == 1
        assert store.list_summaries() == (kept,)
        assert {turn.turn_id for turn in store.list_turns()} == {
            second.turn_id,
            unrelated.turn_id,
        }
    finally:
        store.close()
