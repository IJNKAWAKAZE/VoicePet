from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from core.memory import (
    MemoryConfigurationError,
    MemoryPolicy,
    MemoryStatus,
    MemoryStore,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_memory_schema_is_idempotent_and_records_are_immutable(tmp_path):
    database = tmp_path / "memory.db"
    store = MemoryStore(database, clock=lambda: NOW)
    record = store.create_candidate(
        category="preference",
        content="用户喜欢蓝色",
        source_turn_id=str(uuid4()),
        confidence=0.8,
    )

    assert store.diagnostics()["schema_version"] == 4
    assert store.diagnostics()["fts5"] is True
    assert record.status is MemoryStatus.CANDIDATE
    with pytest.raises(FrozenInstanceError):
        record.content = "changed"
    store.close()
    reopened = MemoryStore(database, clock=lambda: NOW)
    assert reopened.get(record.id) == record
    reopened.close()


@pytest.mark.parametrize(
    "content",
    [
        "password=secret123",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "验证码 123456",
        "身份证 11010519491231002X",
        "银行卡 6222020202020202020",
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_memory_policy_rejects_prohibited_secrets(content):
    assert MemoryPolicy().is_prohibited(content) is True


def test_memory_store_never_writes_prohibited_or_disabled_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    with pytest.raises(MemoryConfigurationError, match="敏感"):
        store.create_confirmed(
            category="preference",
            content="password=secret123",
            source_turn_id=str(uuid4()),
        )
    store.set_enabled(False)
    with pytest.raises(MemoryConfigurationError, match="关闭"):
        store.create_candidate(
            category="preference",
            content="用户喜欢蓝色",
            source_turn_id=str(uuid4()),
            confidence=0.5,
        )
    assert store.list_all() == ()
    store.close()


def test_candidate_confirmation_allows_distinct_freeform_preferences(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    candidate = store.create_candidate(
        category="preferred_color",
        content="蓝色",
        source_turn_id=str(uuid4()),
        confidence=0.7,
    )
    confirmed = store.confirm(candidate.id)
    conflict = store.create_confirmed(
        category="preferred_color",
        content="红色",
        source_turn_id=str(uuid4()),
    )

    assert confirmed.status is MemoryStatus.CONFIRMED
    assert conflict.status is MemoryStatus.CONFIRMED
    assert store.get(confirmed.id).content == "蓝色"
    store.close()


def test_explicit_conflict_resolution_atomically_replaces_same_fact(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    existing = store.create_confirmed(
        category="preferred_color",
        content="蓝色",
        source_turn_id=str(uuid4()),
        fact_key="response.style",
    )
    candidate = store.create_candidate(
        category="preferred_color",
        content="红色",
        source_turn_id=str(uuid4()),
        confidence=0.8,
        fact_key="response.style",
    )
    conflicted = store.confirm(candidate.id)

    resolved = store.resolve_conflict(conflicted.id)

    assert resolved.status is MemoryStatus.CONFIRMED
    assert store.get(existing.id).status is MemoryStatus.DELETED
    assert [record.id for record in store.list_all()] == [resolved.id]
    store.close()


def test_conflict_resolution_rejects_non_conflicted_record(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    candidate = store.create_candidate(
        "preference",
        "安静",
        str(uuid4()),
        0.8,
    )

    with pytest.raises(MemoryConfigurationError, match="冲突"):
        store.resolve_conflict(candidate.id)

    store.close()


def test_search_uses_fts_and_filters_category_status_and_expiry(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    active = store.create_confirmed(
        category="food",
        content="用户喜欢四川火锅",
        source_turn_id=str(uuid4()),
    )
    store.create_confirmed(
        category="travel",
        content="用户想去四川旅行",
        source_turn_id=str(uuid4()),
    )
    store.create_candidate(
        category="food",
        content="用户可能喜欢四川小吃",
        source_turn_id=str(uuid4()),
        confidence=0.5,
    )
    store.create_confirmed(
        category="food",
        content="过期的四川偏好",
        source_turn_id=str(uuid4()),
        expires_at=NOW - timedelta(seconds=1),
    )

    results = store.search(
        "四川",
        category="food",
        statuses=frozenset({MemoryStatus.CONFIRMED}),
    )

    assert [record.id for record in results] == [active.id]
    store.close()


def test_soft_delete_removes_record_from_search_and_clear_date_is_scoped(tmp_path):
    current = [NOW]
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: current[0])
    first = store.create_confirmed(
        category="topic",
        content="第一天话题",
        source_turn_id=str(uuid4()),
    )
    current[0] = NOW + timedelta(days=1)
    second = store.create_confirmed(
        category="topic",
        content="第二天话题",
        source_turn_id=str(uuid4()),
    )

    store.delete(first.id)
    assert store.search("第一天") == ()
    assert store.get(first.id).status is MemoryStatus.DELETED
    assert store.clear_date(date(2026, 9, 3)) == 1
    assert store.get(second.id).status is MemoryStatus.DELETED
    store.close()


def test_memory_validates_uuid_confidence_and_text_limits(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    with pytest.raises(MemoryConfigurationError):
        store.create_candidate("category", "content", "bad-turn", 0.5)
    with pytest.raises(MemoryConfigurationError):
        store.create_candidate("category", "content", str(uuid4()), 1.1)
    with pytest.raises(MemoryConfigurationError):
        store.create_candidate("category", "x" * 4097, str(uuid4()), 0.5)
    store.close()
