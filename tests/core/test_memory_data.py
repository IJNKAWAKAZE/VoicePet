import json
from datetime import UTC, datetime
from uuid import uuid4

import core
from core.memory import MemoryStatus, MemoryStore
from core.memory_data import MemoryDataError, MemoryDataManager
from core.memory_facts import MemoryChange


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
    assert payload["version"] == 2
    assert [item["id"] for item in payload["memories"]] == [second.id]
    assert payload["memories"][0]["status"] == "candidate"
    assert first.content not in destination.read_text(encoding="utf-8")
    store.close()


def test_memory_data_manager_exports_version_two_fact_fields(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    record = store.create_confirmed(
        category="preference",
        content="回答保持简短",
        source_turn_id=str(uuid4()),
        fact_key="response.length",
        value="concise",
        session_id=str(uuid4()),
    )
    manager = MemoryDataManager(store)
    destination = tmp_path / "memory.json"

    manager.export_json(destination)
    exported = json.loads(destination.read_text(encoding="utf-8"))["memories"][0]

    assert exported == {
        "id": record.id,
        "category": "preference",
        "content": "回答保持简短",
        "source_turn_id": record.source_turn_id,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "confidence": 1.0,
        "sensitivity": "normal",
        "expires_at": None,
        "status": "confirmed",
        "fact_key": "response.length",
        "value": "concise",
        "cardinality": "single",
        "origin": "explicit",
        "version": 1,
        "conflict_id": None,
        "keywords": [],
        "session_id": record.session_id,
    }
    store.close()


def test_memory_data_manager_forwards_versioned_change_operations():
    sentinel_record = object()
    sentinel_change = MemoryChange(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        "edit",
        2,
        datetime.now(UTC),
        False,
    )

    class RecordingStore:
        def __init__(self):
            self.calls = []

        def edit(self, memory_id, content, expected_version):
            self.calls.append(("edit", memory_id, content, expected_version))
            return sentinel_record

        def undo(self, change_id):
            self.calls.append(("undo", change_id))
            return True

        def list_changes(self, session_id=None):
            self.calls.append(("list_changes", session_id))
            return (sentinel_change,)

    store = RecordingStore()
    manager = MemoryDataManager(store)
    memory_id = str(uuid4())
    change_id = str(uuid4())
    session_id = str(uuid4())

    assert manager.edit(memory_id, "新的正文", 3) is sentinel_record
    assert manager.undo(change_id) is True
    assert manager.list_changes(session_id) == (sentinel_change,)
    assert store.calls == [
        ("edit", memory_id, "新的正文", 3),
        ("undo", change_id),
        ("list_changes", session_id),
    ]


def test_memory_data_manager_confirms_candidate_and_resolves_conflict(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    store.create_confirmed(
        category="preference",
        content="详细回答",
        source_turn_id=str(uuid4()),
        fact_key="response.length",
        value="detailed",
    )
    candidate = store.create_candidate(
        "preference",
        "简短回答",
        str(uuid4()),
        0.8,
        fact_key="response.length",
        value="concise",
    )
    manager = MemoryDataManager(store)

    conflicted = manager.confirm(candidate.id)
    resolved = manager.resolve_conflict(conflicted.id)

    assert conflicted.status is MemoryStatus.CONFLICTED
    assert resolved.status is MemoryStatus.CONFIRMED
    assert [record.content for record in manager.list_records()] == ["简短回答"]
    store.close()
