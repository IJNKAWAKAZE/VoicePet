from uuid import uuid4

from core.memory import MemoryStore
from core.memory_context import MemoryContextAssembler


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
