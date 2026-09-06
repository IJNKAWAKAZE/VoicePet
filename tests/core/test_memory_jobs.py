from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from core.memory_jobs import MemoryJobError, MemoryJobStore
from core.memory_schema import ensure_memory_schema

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


class FakeClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def add_session(
    path: Path,
    *,
    turn_count: int = 1,
    generation: int = 0,
    created_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> tuple[str, tuple[str, ...]]:
    session_id = str(uuid4())
    turn_ids = tuple(str(uuid4()) for _ in range(turn_count))
    connection = sqlite3.connect(path, isolation_level=None)
    ensure_memory_schema(connection)
    timestamp = created_at.isoformat()
    connection.execute(
        "INSERT INTO memory_sessions"
        "(id,title,created_at,updated_at,generation,deleted) VALUES (?,?,?,?,?,0)",
        (session_id, "测试会话", timestamp, timestamp, generation),
    )
    for index, turn_id in enumerate(turn_ids):
        turn_created = created_at + timedelta(seconds=index)
        turn_expires = expires_at or turn_created + timedelta(days=7)
        connection.execute(
            "INSERT INTO session_turns"
            "(id,turn_id,session_id,user_text,assistant_text,session_date,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                turn_id,
                session_id,
                f"用户消息 {index}",
                f"助手消息 {index}",
                turn_created.date().isoformat(),
                turn_created.isoformat(),
                turn_expires.isoformat(),
            ),
        )
    connection.close()
    return session_id, turn_ids


def progress_rows(path: Path) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(path, isolation_level=None)
    rows = tuple(
        connection.execute(
            "SELECT turn_id,session_id,extracted,summarized "
            "FROM memory_progress ORDER BY rowid"
        )
    )
    connection.close()
    return rows


def test_enqueue_creates_progress_and_debounces_into_one_pending_job(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=3)
    store = MemoryJobStore(path, clock=clock)

    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=2)
    store.enqueue(session_id, turn_ids[1], 0)
    store.enqueue(session_id, turn_ids[1], 0)

    assert store.list_jobs() == (
        store.list_jobs()[0].__class__(
            id=store.list_jobs()[0].id,
            session_id=session_id,
            source_turn_ids=turn_ids[:2],
            generation=0,
            status="pending",
            attempts=0,
            next_run_at=NOW + timedelta(seconds=7),
            created_at=NOW,
            started_at=None,
            error="",
        ),
    )
    assert progress_rows(path) == (
        (turn_ids[0], session_id, 0, 0),
        (turn_ids[1], session_id, 0, 0),
    )

    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(
        "INSERT INTO memory_progress VALUES (?,?,1,0)", (turn_ids[2], session_id)
    )
    connection.close()
    store.enqueue(session_id, turn_ids[2], 0)

    assert store.list_jobs()[0].source_turn_ids == turn_ids[:2]


@pytest.mark.parametrize("invalid_kind", ["missing", "wrong_session", "generation", "expired"])
def test_enqueue_rejects_invalid_source_without_creating_progress(tmp_path, invalid_kind):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    requested_session = session_id
    requested_turn = turn_ids[0]
    generation = 0
    if invalid_kind == "missing":
        requested_turn = str(uuid4())
    elif invalid_kind == "wrong_session":
        requested_session, _ = add_session(path)
    elif invalid_kind == "generation":
        generation = 1
    else:
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute(
            "UPDATE session_turns SET expires_at=? WHERE turn_id=?",
            ((NOW - timedelta(seconds=1)).isoformat(), requested_turn),
        )
        connection.close()
    store = MemoryJobStore(path, clock=clock)

    with pytest.raises(MemoryJobError, match="来源"):
        store.enqueue(requested_session, requested_turn, generation)

    assert store.list_jobs() == ()
    assert progress_rows(path) == ()


def test_claim_obeys_debounce_and_persisted_throttle_across_reopen(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=2)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)

    assert store.claim_due() is None
    clock.advance(seconds=5)
    first = store.claim_due()

    assert first is not None
    assert first.attempts == 1
    assert first.status == "running"
    assert first.started_at == clock.value
    store.finish(first.id, extracted_ids=(turn_ids[0],))
    store.enqueue(session_id, turn_ids[1], 0)
    store.close()

    reopened = MemoryJobStore(path, clock=clock)
    clock.advance(seconds=5)
    assert reopened.claim_due() is None
    clock.advance(seconds=25)
    second = reopened.claim_due()
    assert second is not None
    assert second.source_turn_ids == (turn_ids[1],)


def test_two_connections_can_claim_only_one_global_job(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    first_session, first_turns = add_session(path)
    second_session, second_turns = add_session(path)
    first_store = MemoryJobStore(path, clock=clock)
    second_store = MemoryJobStore(path, clock=clock)
    first_store.enqueue(first_session, first_turns[0], 0)
    second_store.enqueue(second_session, second_turns[0], 0)
    clock.advance(seconds=5)
    barrier = threading.Barrier(2)
    results = []

    def claim(store: MemoryJobStore) -> None:
        barrier.wait()
        results.append(store.claim_due())

    first_thread = threading.Thread(target=claim, args=(first_store,))
    second_thread = threading.Thread(target=claim, args=(second_store,))
    first_thread.start()
    second_thread.start()
    first_thread.join()
    second_thread.join()

    claimed = [job for job in results if job is not None]
    assert len(claimed) == 1
    assert sum(job.status == "running" for job in first_store.list_jobs()) == 1


def test_fail_retries_after_sixty_seconds_and_stops_after_two_attempts(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    first = store.claim_due()
    assert first is not None

    store.fail(first.id, "请求超时")

    retried = store.list_jobs()[0]
    assert retried.status == "pending"
    assert retried.next_run_at == clock.value + timedelta(seconds=60)
    assert retried.error == "请求超时"
    clock.advance(seconds=59)
    assert store.claim_due() is None
    clock.advance(seconds=1)
    second = store.claim_due()
    assert second is not None
    assert second.attempts == 2
    store.fail(second.id, "再次超时")

    failed = store.list_jobs()[0]
    assert failed.status == "failed"
    assert failed.attempts == 2
    assert progress_rows(path) == ((turn_ids[0], session_id, 0, 0),)


def test_exhausted_failed_source_remains_idempotent_across_restart_and_maintenance(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    first = store.claim_due()
    assert first is not None
    store.fail(first.id, "请求超时")
    clock.advance(seconds=60)
    second = store.claim_due()
    assert second is not None
    store.fail(second.id, "再次超时")
    store.close()

    reopened = MemoryJobStore(path, clock=clock)
    clock.advance(seconds=30)
    assert reopened.purge_terminal() == 0
    reopened.enqueue(session_id, turn_ids[0], 0)

    jobs = reopened.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert jobs[0].attempts == 2


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
def test_terminal_cleanup_preserves_started_at_for_thirty_seconds(
    tmp_path, terminal_status
):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    running = store.claim_due()
    assert running is not None
    if terminal_status == "completed":
        store.finish(running.id, extracted_ids=turn_ids)
    else:
        store.invalidate(session_id)

    clock.advance(seconds=29)
    assert store.purge_terminal() == 0
    assert store.list_jobs()[0].started_at == NOW + timedelta(seconds=5)
    clock.advance(seconds=1)
    assert store.purge_terminal() == 1
    assert store.list_jobs() == ()


def test_failed_cleanup_keeps_valid_source_markers_until_each_source_expires(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=2)
    store = MemoryJobStore(path, clock=clock)
    for turn_id in turn_ids:
        store.enqueue(session_id, turn_id, 0)
    clock.advance(seconds=5)
    first = store.claim_due()
    assert first is not None
    store.fail(first.id, "请求超时")
    clock.advance(seconds=60)
    second = store.claim_due()
    assert second is not None
    store.fail(second.id, "再次超时")
    clock.advance(seconds=30)

    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(
        "UPDATE session_turns SET expires_at=? WHERE turn_id=?",
        ((clock.value - timedelta(seconds=1)).isoformat(), turn_ids[0]),
    )
    connection.close()
    assert store.purge_terminal() == 0
    assert store.list_jobs()[0].source_turn_ids == (turn_ids[1],)

    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(
        "UPDATE session_turns SET expires_at=? WHERE turn_id=?",
        ((clock.value - timedelta(seconds=1)).isoformat(), turn_ids[1]),
    )
    connection.close()
    assert store.purge_terminal() == 1
    assert store.list_jobs() == ()


def test_recover_interrupted_preserves_attempt_budget_and_does_not_delay_twice(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    assert store.claim_due() is not None
    store.close()

    reopened = MemoryJobStore(path, clock=clock)
    reopened.recover_interrupted()
    recovered = reopened.list_jobs()[0]
    assert recovered.status == "pending"
    assert recovered.attempts == 1
    assert recovered.next_run_at == clock.value + timedelta(seconds=60)
    clock.advance(seconds=10)
    reopened.recover_interrupted()
    assert reopened.list_jobs()[0].next_run_at == recovered.next_run_at
    clock.advance(seconds=50)
    assert reopened.claim_due() is not None
    reopened.close()

    final_store = MemoryJobStore(path, clock=clock)
    final_store.recover_interrupted()
    final = final_store.list_jobs()[0]
    assert final.status == "failed"
    assert final.attempts == 2


def test_finish_marks_exact_coverage_and_accepts_prior_extracted_summary_sources(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=2)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    first = store.claim_due()
    assert first is not None
    store.finish(first.id, extracted_ids=(turn_ids[0],))

    clock.advance(seconds=25)
    store.enqueue(session_id, turn_ids[1], 0)
    clock.advance(seconds=5)
    second = store.claim_due()
    assert second is not None
    store.finish(
        second.id,
        extracted_ids=(turn_ids[1],),
        summarized_ids=turn_ids,
    )

    assert progress_rows(path) == (
        (turn_ids[0], session_id, 1, 1),
        (turn_ids[1], session_id, 1, 1),
    )
    assert store.list_unsummarized(session_id) == ()


def test_partial_finish_completes_original_and_requeues_only_uncovered_sources(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=3)
    store = MemoryJobStore(path, clock=clock)
    for turn_id in turn_ids:
        store.enqueue(session_id, turn_id, 0)
    clock.advance(seconds=5)
    original = store.claim_due()
    assert original is not None

    store.finish(original.id, extracted_ids=(turn_ids[0],))

    completed, pending = store.list_jobs()
    assert completed.status == "completed"
    assert completed.started_at == clock.value
    assert pending.source_turn_ids == turn_ids[1:]
    assert pending.status == "pending"
    assert pending.attempts == 0
    assert pending.next_run_at == clock.value + timedelta(seconds=5)
    assert pending.started_at is None
    clock.advance(seconds=5)
    assert store.claim_due() is None
    clock.advance(seconds=25)
    assert store.claim_due() is not None


def test_skip_source_fails_oversized_turn_without_blocking_later_turns(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=3)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    store.enqueue(session_id, turn_ids[1], 0)
    clock.advance(seconds=5)
    original = store.claim_due()
    assert original is not None

    store.skip_source(original.id, turn_ids[0])

    failed, pending = store.list_jobs()
    assert failed.status == "failed"
    assert failed.source_turn_ids == (turn_ids[0],)
    assert pending.source_turn_ids == (turn_ids[1],)
    assert progress_rows(path)[0] == (turn_ids[0], session_id, 0, 0)
    clock.advance(seconds=30)
    second = store.claim_due()
    assert second is not None
    store.finish(second.id, extracted_ids=(turn_ids[1],))
    clock.advance(seconds=30)
    store.enqueue(session_id, turn_ids[2], 0)
    clock.advance(seconds=5)
    later = store.claim_due()
    assert later is not None
    assert later.source_turn_ids == (turn_ids[2],)


def test_oversized_source_is_excluded_from_later_summary_candidates(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=2)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    oversized = store.claim_due()
    assert oversized is not None
    store.skip_source(oversized.id, turn_ids[0])

    clock.advance(seconds=30)
    store.enqueue(session_id, turn_ids[1], 0)

    assert store.list_unsummarized(session_id) == (turn_ids[1],)
    assert progress_rows(path) == (
        (turn_ids[0], session_id, 0, 0),
        (turn_ids[1], session_id, 0, 0),
    )


def test_generic_failed_source_remains_a_summary_candidate(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    first = store.claim_due()
    assert first is not None
    store.fail(first.id, "临时协议失败")
    clock.advance(seconds=60)
    second = store.claim_due()
    assert second is not None
    store.fail(second.id, "临时协议失败")

    assert store.list_unsummarized(session_id) == turn_ids


def test_finish_rejects_deleted_source_or_changed_generation_without_progress(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(session_id, turn_ids[0], 0)
    clock.advance(seconds=5)
    running = store.claim_due()
    assert running is not None
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(
        "UPDATE memory_sessions SET generation=generation+1 WHERE id=?", (session_id,)
    )
    connection.close()

    with pytest.raises(MemoryJobError, match="失效"):
        store.finish(running.id, extracted_ids=turn_ids)

    assert progress_rows(path) == ((turn_ids[0], session_id, 0, 0),)


def test_claim_cancels_invalidated_source_and_can_take_next_valid_job(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    stale_session, stale_turns = add_session(path)
    valid_session, valid_turns = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(stale_session, stale_turns[0], 0)
    store.enqueue(valid_session, valid_turns[0], 0)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("DELETE FROM session_turns WHERE turn_id=?", (stale_turns[0],))
    connection.close()
    clock.advance(seconds=5)

    claimed = store.claim_due()

    assert claimed is not None
    assert claimed.session_id == valid_session
    assert [job.status for job in store.list_jobs()] == ["cancelled", "running"]


def test_invalidate_cancels_pending_and_running_without_restarting_old_jobs(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    first_session, first_turns = add_session(path)
    second_session, second_turns = add_session(path)
    store = MemoryJobStore(path, clock=clock)
    store.enqueue(first_session, first_turns[0], 0)
    store.enqueue(second_session, second_turns[0], 0)
    clock.advance(seconds=5)
    running = store.claim_due()
    assert running is not None

    store.invalidate(running.session_id)
    store.invalidate()
    store.recover_interrupted()

    cancelled = store.list_jobs()
    assert all(job.status == "cancelled" for job in cancelled)
    assert any(job.started_at == clock.value for job in cancelled)
    assert store.claim_due() is None


def test_list_unsummarized_uses_progress_and_real_turn_order(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path, turn_count=3)
    store = MemoryJobStore(path, clock=clock)
    for turn_id in reversed(turn_ids):
        store.enqueue(session_id, turn_id, 0)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(
        "UPDATE memory_progress SET summarized=1 WHERE turn_id=?", (turn_ids[0],)
    )
    connection.execute(
        "UPDATE session_turns SET expires_at=? WHERE turn_id=?",
        ((NOW - timedelta(seconds=1)).isoformat(), turn_ids[2]),
    )
    connection.close()

    assert store.list_unsummarized(session_id) == (turn_ids[1],)


def test_close_is_idempotent_and_closed_operations_raise_safe_error(tmp_path):
    path = tmp_path / "jobs.db"
    clock = FakeClock()
    session_id, turn_ids = add_session(path)
    store = MemoryJobStore(path, clock=clock)

    store.close()
    store.close()

    with pytest.raises(MemoryJobError, match="关闭"):
        store.enqueue(session_id, turn_ids[0], 0)
    with pytest.raises(MemoryJobError, match="关闭"):
        store.list_jobs()
