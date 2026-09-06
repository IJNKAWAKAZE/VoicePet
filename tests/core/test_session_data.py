from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext
from core.session_data import SessionDataManager


def test_session_data_lists_latest_first_and_clears_live_context(tmp_path):
    current = [datetime(2026, 9, 4, 10, tzinfo=UTC)]
    archive = SessionArchiveStore(
        tmp_path / "assistant.db",
        clock=lambda: current[0],
    )
    context = SessionContext()
    manager = SessionDataManager(archive, context)
    first = str(uuid4())
    second = str(uuid4())
    session_id = context.session_id
    archive.archive_turn(first, "第一问", "第一答", session_id)
    current[0] += timedelta(minutes=1)
    archive.archive_turn(second, "第二问", "第二答", session_id)
    context.add_turn("当前问题", "当前回答")

    records = manager.list_records()
    assert len(records) == 1
    assert records[0].id == session_id
    assert records[0].turn_count == 2
    assert records[0].is_active is True
    assert [turn.turn_id for turn in manager.list_turns(session_id)] == [first, second]
    assert manager.delete(session_id) is True
    assert context.build_history() == ()

    context.add_turn("另一个问题", "另一个回答")
    assert manager.clear() == 0
    assert manager.list_records() == ()
    assert context.build_history() == ()
    archive.close()


def test_session_data_switches_context_and_new_session_breaks_only_chat_history(tmp_path):
    archive = SessionArchiveStore(tmp_path / "assistant.db")
    context = SessionContext()
    manager = SessionDataManager(archive, context)
    first_session = str(uuid4())
    second_session = str(uuid4())
    archive.archive_turn(str(uuid4()), "甲一", "甲答", first_session)
    archive.archive_turn(str(uuid4()), "乙一", "乙答", second_session)

    turns = manager.activate(first_session)

    assert [turn.user_text for turn in turns] == ["甲一"]
    assert context.session_id == first_session
    assert context.build_history()[0]["content"] == "甲一"
    new_session = manager.new()
    assert new_session not in {first_session, second_session}
    assert context.build_history() == ()
    archive.close()


def test_session_data_exposes_summary_deletion_and_minimal_source_labels(tmp_path):
    current = [datetime(2026, 9, 4, 10, tzinfo=UTC)]
    archive = SessionArchiveStore(
        tmp_path / "assistant.db",
        retention_days=1,
        summary_retention_days=7,
        clock=lambda: current[0],
    )
    manager = SessionDataManager(archive, SessionContext())
    turn = archive.archive_turn(str(uuid4()), "发布计划", "先测试", str(uuid4()))
    summary = archive.save_summary(turn.turn_id, "发布", ())

    assert manager.list_summaries(turn.session_id) == (summary,)
    assert manager.source_label(turn.turn_id) == {
        "title": "发布计划",
        "status": "available",
        "created_at": turn.created_at,
        "session_id": turn.session_id,
    }
    current[0] += timedelta(days=2)
    assert manager.source_label(turn.turn_id)["status"] == "expired"
    assert manager.source_label(str(uuid4())) == {
        "title": "",
        "status": "missing",
        "created_at": None,
        "session_id": "",
    }
    assert manager.delete_summary(summary.id) is True
    assert manager.delete_summary(summary.id) is False
    archive.close()
