import json
from uuid import uuid4

import core
from core.memory import MemoryStatus, MemoryStore
from core.memory_data import MemoryDataError, MemoryDataManager


def test_memory_data_types_are_publicly_exported():
    assert core.MemoryDataManager is MemoryDataManager
    assert core.MemoryDataError is MemoryDataError


def test_memory_data_manager_exports_visible_records_and_soft_deletes(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    first = store.create_confirmed(
        category="preference",
        content="喜欢低音量播报",
        source_turn_id=str(uuid4()),
    )
    second = store.create_candidate(
        "habit",
        "工作日早上查看天气",
        str(uuid4()),
        0.7,
    )
    manager = MemoryDataManager(store)
    destination = tmp_path / "exports" / "memory.json"

    assert manager.delete(first.id) is True
    assert manager.export_json(destination) == 1
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert payload["format"] == "voicepet-memory-export"
    assert payload["version"] == 1
    assert [item["id"] for item in payload["memories"]] == [second.id]
    assert payload["memories"][0]["status"] == "candidate"
    assert first.content not in destination.read_text(encoding="utf-8")
    store.close()


def test_memory_data_manager_confirms_candidate_and_resolves_conflict(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    store.create_confirmed(
        category="preference",
        content="旧偏好",
        source_turn_id=str(uuid4()),
    )
    candidate = store.create_candidate(
        "preference",
        "新偏好",
        str(uuid4()),
        0.8,
    )
    manager = MemoryDataManager(store)

    conflicted = manager.confirm(candidate.id)
    resolved = manager.resolve_conflict(conflicted.id)

    assert conflicted.status is MemoryStatus.CONFLICTED
    assert resolved.status is MemoryStatus.CONFIRMED
    assert [record.content for record in manager.list_records()] == ["新偏好"]
    store.close()
