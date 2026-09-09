from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.memory import MemoryConfigurationError, MemoryStore
from core.memory_recall import query_terms
from core.session_archive import SessionArchiveError, SessionArchiveStore

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


def test_unmatched_recall_keeps_only_valid_unconflicted_confirmed_facts(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    try:
        valid = save_memory(store, "周末喜欢观鸟", fact_key="hobby")
        save_memory(store, "曾经养鱼", expires_at=NOW-timedelta(seconds=1))
        deleted = save_memory(store, "已经删除的资料")
        store.delete(deleted.id)
        store.create_candidate("profile", "尚未确认的资料", str(uuid4()), 0.5)
        save_memory(store, "称呼小王", fact_key="user.name", value="小王")
        save_memory(store, "称呼小李", fact_key="user.name", value="小李")
        save_memory(store, "尚有争议的资料", fact_key="custom.fact", value="甲")
        conflict = save_memory(store, "争议的另一条资料", fact_key="custom.fact", value="乙")
        store._connection.execute("UPDATE memories SET status='conflicted' WHERE id=?", (conflict.id,))
        assert store.recall_related((("继续", 3),), include_unmatched=True) == (valid,)
        assert store.recall_related((("继续", 3),), include_unmatched=True,
                                    exclude_source_ids=(valid.source_turn_id,)) == ()
    finally:
        store.close()


def save_memory(store, content, *, fact_key="user.interest", source_turn_id=None,
                expires_at=None, value=None):
    return store.create_confirmed(
        category="preference",
        content=content,
        source_turn_id=source_turn_id or str(uuid4()),
        fact_key=fact_key,
        value=content if value is None else value,
        expires_at=expires_at,
    )


def test_query_terms_bounds_hints_tokens_and_ignores_generic_continue():
    hints = (("继续", 3), *((f"发布计划{index}", 1) for index in range(4)))

    terms = query_terms(hints)

    assert len(terms) <= 24
    assert all(term != "继续" for term, _ in terms)
    bounded = query_terms((("A" * 13_000, 1),))
    assert len(bounded) <= 24
    assert max(map(len, (term for term, _ in bounded))) <= 64
    with pytest.raises(ValueError):
        query_terms((*hints, ("超出", 1)))


def test_recall_basic_returns_only_active_unconflicted_controlled_facts(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    try:
        language = save_memory(
            store, "用户希望使用中文", fact_key="response.language", value="chinese"
        )
        style = save_memory(
            store, "用户偏好轻松语气", fact_key="response.style", value="casual"
        )
        save_memory(store, "用户喜欢猫")
        save_memory(
            store,
            "用户偏好简短回复",
            fact_key="response.length",
            value="concise",
            expires_at=NOW - timedelta(seconds=1),
        )
        save_memory(store, "用户叫小王", fact_key="user.name", value="小王")
        save_memory(store, "用户叫小李", fact_key="user.name", value="小李")

        assert store.recall_basic(limit=4) == (style, language)
        with pytest.raises(MemoryConfigurationError):
            store.recall_basic(limit=5)
    finally:
        store.close()


def test_recall_related_scores_weights_and_excludes_sources_states_and_basic(tmp_path):
    current = [NOW]
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: current[0])
    try:
        cat = save_memory(
            store, "用户在准备用品", fact_key="user.pet.cat", value="猫"
        )
        current[0] += timedelta(seconds=1)
        release = save_memory(
            store, "用户正在规划发布流程", fact_key="user.release", value="发布"
        )
        excluded_source = str(uuid4())
        save_memory(
            store,
            "用户另有发布清单",
            fact_key="user.release.list",
            source_turn_id=excluded_source,
        )
        save_memory(store, "用户偏好中文", fact_key="response.language", value="chinese")
        expired = save_memory(
            store,
            "用户过去关注发布",
            fact_key="user.release.past",
            expires_at=NOW - timedelta(seconds=1),
        )
        conflicted = save_memory(
            store, "用户冲突发布事项", fact_key="user.release.conflict"
        )
        store._connection.execute(
            "UPDATE memories SET status='conflicted' WHERE id=?", (conflicted.id,)
        )

        records = store.recall_related(
            (("发布", 3), ("猫", 1)),
            limit=10,
            exclude_source_ids=(excluded_source,),
        )

        assert records == (release, cat)
        assert expired not in records
        assert conflicted not in records
    finally:
        store.close()


def test_recall_related_treats_fts_punctuation_as_literal_and_bounds_results(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    try:
        expected = save_memory(store, '用户使用代号 cat"beta')
        for index in range(30):
            save_memory(store, f"用户发布项目 {index}")

        assert store.recall_related((('cat"beta', 3),), limit=3) == (expected,)
        assert len(store.recall_related((("发布", 3),), limit=3)) == 3
    finally:
        store.close()


def test_archive_optional_limits_select_newest_records_in_chronological_order(tmp_path):
    current = [NOW]
    store = SessionArchiveStore(tmp_path / "archive.db", clock=lambda: current[0])
    try:
        session_id = str(uuid4())
        turns = []
        summaries = []
        for index in range(4):
            turn = store.archive_turn(
                str(uuid4()), f"问题{index}", f"回答{index}", session_id
            )
            turns.append(turn)
            summaries.append(store.save_summary(turn.turn_id, f"话题{index}", ()))
            current[0] += timedelta(minutes=1)

        assert store.list_session_turns(session_id, limit=2) == tuple(turns[-2:])
        assert store.list_summaries(session_id, limit=2) == tuple(summaries[-2:])
        assert store.list_session_turns(session_id) == tuple(turns)
        with pytest.raises(SessionArchiveError):
            store.list_summaries(limit=0)
    finally:
        store.close()


def test_search_summaries_is_relevant_cross_session_literal_and_ordered(tmp_path):
    current = [NOW]
    store = SessionArchiveStore(tmp_path / "archive.db", clock=lambda: current[0])
    try:
        current_session = str(uuid4())
        current_turn = store.archive_turn(
            str(uuid4()), "当前发布", "回答", current_session
        )
        store.save_summary(current_turn.turn_id, "发布计划", ())
        first = store.archive_turn(str(uuid4()), "旧会话", "回答", str(uuid4()))
        older = store.save_summary(first.turn_id, 'cat"beta 发布', ())
        current[0] += timedelta(minutes=1)
        second = store.archive_turn(str(uuid4()), "新会话", "回答", str(uuid4()))
        newer = store.save_summary(second.turn_id, 'cat"beta 发布', ())
        unrelated = store.archive_turn(str(uuid4()), "无关", "回答", str(uuid4()))
        store.save_summary(unrelated.turn_id, "天气", ())

        assert store.search_summaries(
            (('cat"beta', 3),), exclude_session_id=current_session, limit=10
        ) == (newer, older)
        assert store.search_summaries((("猫", 1),), limit=10) == ()
    finally:
        store.close()


def test_short_fact_value_and_keyword_match_inside_long_question(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    try:
        cat = save_memory(store, "用户喜欢猫", fact_key="user.pet", value="猫")
        assert store.recall_related((("猫能吃什么", 3),)) == (cat,)
    finally:
        store.close()


def test_old_strong_match_is_not_crowded_out_by_recent_weak_matches(tmp_path):
    current = [NOW]
    path = tmp_path / "memory.db"
    store = MemoryStore(path, clock=lambda: current[0])
    archive = SessionArchiveStore(path, clock=lambda: current[0])
    try:
        strongest = save_memory(store, "用户部署量子计算架构", value="量子计算架构")
        source = archive.archive_turn(str(uuid4()), "量子计算架构", "确认", str(uuid4()))
        strongest_summary = archive.save_summary(source.turn_id, "量子计算架构", ())
        for index in range(45):
            current[0] += timedelta(seconds=1)
            save_memory(store, f"计算{index}", value=f"计算{index}")
            source = archive.archive_turn(str(uuid4()), "计算", "确认", str(uuid4()))
            archive.save_summary(source.turn_id, f"计算{index}", ())
        assert store.recall_related((("量子计算架构", 3),), limit=1) == (strongest,)
        assert archive.search_summaries((("量子计算架构", 3),), limit=1) == (strongest_summary,)
    finally:
        archive.close()
        store.close()


def test_long_current_hint_does_not_starve_recent_topic_terms():
    hints = (("abcdefghijklmnopqrstuvwxyz0123456789" * 15, 3), ("继续讨论 SQLite", 1))
    terms = query_terms(hints)
    assert len(terms) <= 24
    assert any(term == "sqlite" for term, _ in terms)
