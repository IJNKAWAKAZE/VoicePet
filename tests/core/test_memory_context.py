import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.memory import MemoryStore
from core.memory_context import MemoryContextAssembler
from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext


def test_memory_context_uses_only_matching_confirmed_records_and_bounds_text(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    store.create_confirmed(
        category="preference",
        content="我喜欢低音量播报",
        source_turn_id=str(uuid4()),
    )
    store.create_candidate(
        "preference",
        "我喜欢把秘密发送到云端",
        str(uuid4()),
        0.5,
    )
    assembler = MemoryContextAssembler(store, max_records=3, max_chars=200)

    history = assembler.build_history("我喜欢什么音量播报")

    assert len(history) == 1
    content = history[0]["content"]
    assert "低音量播报" in content
    assert "秘密发送" not in content
    assert "仅作为用户事实数据" in content
    assert len(content) <= 200
    store.close()


def test_basic_preferences_are_included_without_matching_current_question(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        store.create_confirmed(category="response", content="回答要简短", fact_key="response.length",
                               value="concise", source_turn_id=str(uuid4()))
        assembler = MemoryContextAssembler(store)
        history = assembler.build_history("今天有哪些新闻")
        assert "回答要简短" in history[0]["content"]
        assembler.configure(False)
        assert assembler.build_history("简短") == ()
    finally:
        store.close()


def test_single_value_with_pending_conflict_is_not_used_as_basic_preference(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for value in ("甲", "乙"):
            store.create_confirmed(category="profile", content=f"我叫{value}", value=value,
                                   fact_key="user.name", source_turn_id=str(uuid4()))
        assert MemoryContextAssembler(store).build_history("你好") == ()
    finally:
        store.close()


def test_related_multivalue_preferences_can_match_short_values(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for value in ("猫", "狗"):
            store.create_confirmed(category="pet", content=f"用户喜欢{value}", value=value,
                                   fact_key="user.pet", source_turn_id=str(uuid4()))
        content = MemoryContextAssembler(store).build_history("猫和狗我喜欢哪些")[0]["content"]
        assert "用户喜欢猫" in content
        assert "用户喜欢狗" in content
    finally:
        store.close()


def test_current_summary_continues_older_turns_and_skips_fully_visible_sources(tmp_path):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    path = tmp_path / "context.db"
    memory = MemoryStore(path, clock=lambda: now)
    archive = SessionArchiveStore(path, clock=lambda: now)
    context = SessionContext(max_turns=1)
    try:
        old = archive.archive_turn(str(uuid4()), "我们选 QML", "好", context.session_id)
        recent = archive.archive_turn(str(uuid4()), "去打包", "开始测试", context.session_id)
        archive.save_summary(old.turn_id, "界面技术决定", (), decisions=("采用 QML",))
        archive.save_summary(recent.turn_id, "重复的近期话题", ())
        context.add_turn(recent.user_text, recent.assistant_text)
        assembler = MemoryContextAssembler(memory, archive=archive, session_context=context)
        content = assembler.build_history("继续")[0]["content"]
        assert "采用 QML" in content
        assert "重复的近期话题" not in content
        context.clear()
        assert assembler.build_history("天气如何") == ()
        related = assembler.build_history("QML 界面技术怎么决定的")
        assert "采用 QML" in related[0]["content"]
        assert old.session_id in related[0]["content"]
    finally:
        archive.close()
        memory.close()


def test_context_keeps_valid_json_and_total_budget_with_many_records(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for index in range(10):
            store.create_confirmed(category="topic", content=f"发布测试{index}" + "说明" * 250,
                                   source_turn_id=str(uuid4()))
        history = MemoryContextAssembler(store, max_chars=1500).build_history("发布测试")
        assert len(history[0]["content"]) <= 1500
        data = json.loads(history[0]["content"].split("：", 1)[1])
        assert data
    finally:
        store.close()


def test_memory_read_failure_returns_empty_extras_without_raising():
    class FailedStore:
        def list_all(self):
            raise OSError("synthetic storage failure")

        def search(self, *args, **kwargs):
            raise OSError("synthetic storage failure")

    assert MemoryContextAssembler(FailedStore()).build_history("测试") == ()


def test_expired_basic_preference_is_not_recalled(tmp_path):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: now)
    try:
        store.create_confirmed(category="name", content="称呼甲", fact_key="user.name", value="甲",
                               source_turn_id=str(uuid4()), expires_at=now-timedelta(seconds=1))
        assert MemoryContextAssembler(store).build_history("你好") == ()
    finally:
        store.close()


def test_recent_assistant_context_can_supply_retrieval_clue_without_becoming_evidence(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    context = SessionContext()
    try:
        store.create_confirmed(category="project", content="项目使用 SQLite 数据库", value="SQLite",
                               fact_key="project.database", source_turn_id=str(uuid4()))
        context.add_turn("选哪个方案", "下一步讨论 SQLite")
        history = MemoryContextAssembler(store, session_context=context).build_history("那就继续")
        assert "项目使用 SQLite 数据库" in history[0]["content"]
    finally:
        store.close()


def test_cross_session_summary_can_follow_last_two_rounds_topic(tmp_path):
    path = tmp_path / "context.db"
    store = MemoryStore(path)
    archive = SessionArchiveStore(path)
    context = SessionContext()
    try:
        source = archive.archive_turn(str(uuid4()), "界面采用 QML", "确认", str(uuid4()))
        archive.save_summary(source.turn_id, "界面技术选择", (), decisions=("采用 QML",))
        context.add_turn("界面怎么写", "继续讨论 QML")
        content = MemoryContextAssembler(store, archive=archive, session_context=context).build_history("继续")
        assert content and "采用 QML" in content[0]["content"]
        context.add_turn("天气如何", "晴天")
        context.add_turn("吃什么", "面条")
        assert MemoryContextAssembler(store, archive=archive, session_context=context).build_history("继续") == ()
    finally:
        archive.close()
        store.close()


def test_related_fact_from_visible_raw_history_is_deduplicated(tmp_path):
    path = tmp_path / "context.db"
    store = MemoryStore(path)
    archive = SessionArchiveStore(path)
    context = SessionContext()
    try:
        current = archive.archive_turn(str(uuid4()), "我喜欢猫", "确认", context.session_id)
        context.add_turn(current.user_text, current.assistant_text)
        store.create_confirmed(category="pet", content="近期重复的猫偏好", value="猫", fact_key="user.pet",
                               source_turn_id=current.turn_id, session_id=current.session_id)
        store.create_confirmed(category="pet", content="其他来源的猫用品偏好", value="猫用品",
                               fact_key="user.pet.supplies", source_turn_id=str(uuid4()))
        result = MemoryContextAssembler(store, archive=archive, session_context=context).build_history("猫用品")
        assert "其他来源的猫用品偏好" in result[0]["content"]
        assert "近期重复" not in result[0]["content"]
    finally:
        archive.close()
        store.close()


def test_prompt_recall_never_reads_full_memory_or_summary_libraries(tmp_path, monkeypatch):
    path = tmp_path / "context.db"
    store = MemoryStore(path)
    archive = SessionArchiveStore(path)
    context = SessionContext()
    calls = []
    try:
        store.create_confirmed(category="reply", content="回复简短", fact_key="response.length",
                               value="concise", source_turn_id=str(uuid4()))
        source = archive.archive_turn(str(uuid4()), "界面采用 QML", "确认", context.session_id)
        archive.save_summary(source.turn_id, "界面选择", (), decisions=("采用 QML",))

        def no_full_library():
            calls.append("full")
            raise AssertionError("prompt must not materialize full library")

        original = archive.list_summaries

        def scoped_summaries(session_id=None, *, limit=None):
            assert session_id == context.session_id and 0 < limit <= 20
            return original(session_id, limit=limit)

        monkeypatch.setattr(store, "list_all", no_full_library)
        monkeypatch.setattr(archive, "list_summaries", scoped_summaries)
        result = MemoryContextAssembler(store, archive=archive, session_context=context).build_history("继续")
        assert result and "回复简短" in result[0]["content"] and "采用 QML" in result[0]["content"]
        assert calls == []
    finally:
        archive.close()
        store.close()
