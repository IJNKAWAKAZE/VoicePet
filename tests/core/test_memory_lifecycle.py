import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.memory import (
    MemoryConfigurationError,
    MemorySensitivity,
    MemoryStatus,
    MemoryStore,
)
from core.memory_facts import FactEvidence, FactProposal, MemoryChange

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    memory_store = MemoryStore(tmp_path / "memory.db", clock=lambda: NOW)
    yield memory_store
    memory_store.close()


def evidence(user_text: str, *, turn_id: str | None = None, session_id: str | None = None):
    return FactEvidence(
        turn_id or str(uuid4()),
        session_id or str(uuid4()),
        user_text,
    )


def proposal(
    *,
    category: str = "preference",
    content: str = "用户喜欢猫",
    fact_key: str = "user.pet",
    value: str = "猫",
    quote: str = "我喜欢猫",
    confidence: float = 0.9,
    stability: str = "stable",
    sensitivity: str = "normal",
    explicit_update: bool = False,
    keywords: tuple[str, ...] = (),
):
    return FactProposal(
        category,
        content,
        fact_key,
        value,
        quote,
        confidence,
        stability,
        sensitivity,
        explicit_update,
        keywords,
    )


def test_fact_contracts_are_frozen_and_memory_schema_uses_component_version(tmp_path):
    fact = proposal()
    with pytest.raises(FrozenInstanceError):
        fact.value = "狗"

    database = tmp_path / "memory.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=73")
    connection.close()

    memory_store = MemoryStore(database, clock=lambda: NOW)
    assert memory_store.diagnostics() == {"schema_version": 2, "fts5": True}
    memory_store.close()

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 73
    connection.close()


def test_automatic_low_risk_fact_is_confirmed_with_evidence_and_fts(store):
    source = evidence("我喜欢猫，也经常照顾流浪猫")

    change = store.save_fact(
        proposal(keywords=("宠物", "猫咪")),
        source,
    )

    assert isinstance(change, MemoryChange)
    record = store.get(change.memory_id)
    assert record.status is MemoryStatus.CONFIRMED
    assert record.fact_key == "user.pet"
    assert record.value == "猫"
    assert record.cardinality == "multi"
    assert record.origin == "automatic"
    assert record.keywords == ("宠物", "猫咪")
    assert record.session_id == source.session_id
    assert store.search("流浪猫") == ()
    assert [item.id for item in store.search("用户喜欢猫")] == [record.id]


def test_automatic_fact_rejects_forged_quote_and_hallucinated_value(store):
    source = evidence("我喜欢猫")

    with pytest.raises(MemoryConfigurationError, match="引用"):
        store.save_fact(proposal(quote="我喜欢狗"), source)
    with pytest.raises(MemoryConfigurationError, match="事实值"):
        store.save_fact(proposal(value="狗"), source)

    assert store.list_all() == ()


@pytest.mark.parametrize(
    ("user_text", "stability", "sensitivity", "expected_status"),
    [
        ("我今天喜欢猫", "stable", "normal", None),
        ("我喜欢猫", "temporary", "normal", None),
        ("我喜欢猫", "stable", "personal", None),
        ("假如我喜欢猫呢", "stable", "normal", MemoryStatus.CANDIDATE),
        ("他说我喜欢猫", "stable", "normal", MemoryStatus.CANDIDATE),
        ("小说角色设定是我喜欢猫", "stable", "normal", MemoryStatus.CANDIDATE),
        ("我可能喜欢猫", "uncertain", "normal", MemoryStatus.CANDIDATE),
    ],
)
def test_automatic_policy_handles_temporary_sensitive_and_uncertain_contexts(
    store,
    user_text,
    stability,
    sensitivity,
    expected_status,
):
    change = store.save_fact(
        proposal(
            quote=user_text,
            stability=stability,
            sensitivity=sensitivity,
        ),
        evidence(user_text),
    )

    if expected_status is None:
        assert change is None
        return
    assert store.get(change.memory_id).status is expected_status


@pytest.mark.parametrize(
    "field",
    ["content", "value", "quote", "keywords"],
)
def test_secrets_are_rejected_from_every_automatic_proposal_field(store, field):
    secret = "我的密码是 synthetic-secret-123"
    values = {
        "content": "用户喜欢猫",
        "value": "猫",
        "quote": "我喜欢猫",
        "keywords": ("宠物",),
    }
    values[field] = (secret,) if field == "keywords" else secret
    source_text = f"我喜欢猫；{secret}"

    with pytest.raises(MemoryConfigurationError, match="敏感"):
        store.save_fact(proposal(**values), evidence(source_text))

    assert store.list_all() == ()


def test_fact_identity_reuses_duplicates_and_keeps_multi_values(store):
    first_evidence = evidence("我喜欢猫")
    first = store.save_fact(proposal(), first_evidence)
    assert first is not None

    assert store.save_fact(proposal(), first_evidence) is None
    assert store.save_fact(proposal(quote="我一直喜欢猫"), evidence("我一直喜欢猫")) is None
    dog = store.save_fact(
        proposal(content="用户喜欢狗", value="狗", quote="我也喜欢狗"),
        evidence("我也喜欢狗"),
    )

    assert dog is not None
    assert {record.value for record in store.list_all()} == {"猫", "狗"}


def test_single_value_explicit_update_reuses_id_but_ambiguous_change_conflicts(store):
    original = store.save_fact(
        proposal(
            content="称呼用户为小王",
            fact_key="user.name",
            value="小王",
            quote="我叫小王",
        ),
        evidence("我叫小王"),
    )
    assert original is not None

    updated = store.save_fact(
        proposal(
            content="称呼用户为小李",
            fact_key="user.name",
            value="小李",
            quote="我改名叫小李了",
            explicit_update=True,
        ),
        evidence("我改名叫小李了"),
    )

    assert updated is not None
    assert updated.kind == "update"
    assert updated.memory_id == original.memory_id
    assert store.get(original.memory_id).value == "小李"
    assert store.get(original.memory_id).version == 2

    conflict = store.save_fact(
        proposal(
            content="称呼用户为小张",
            fact_key="user.name",
            value="小张",
            quote="我叫小张",
        ),
        evidence("我叫小张"),
    )
    assert conflict is not None
    conflicted = store.get(conflict.memory_id)
    assert conflicted.status is MemoryStatus.CONFLICTED
    assert conflicted.conflict_id == original.memory_id
    assert store.search("小李") == ()

    unrelated = store.create_confirmed(
        category="profile",
        content="用户住在杭州",
        source_turn_id=str(uuid4()),
        fact_key="user.city",
        value="杭州",
    )
    resolved = store.resolve_conflict(conflicted.id)

    assert resolved.status is MemoryStatus.CONFIRMED
    assert store.get(original.memory_id).status is MemoryStatus.DELETED
    assert store.get(unrelated.id).status is MemoryStatus.CONFIRMED


def test_manual_api_allows_personal_data_but_never_prohibited_secret(store):
    personal = store.create_confirmed(
        category="profile",
        content="用户住在杭州",
        source_turn_id=str(uuid4()),
        sensitivity=MemorySensitivity.PERSONAL,
        fact_key="user.city",
        value="杭州",
    )

    assert personal.sensitivity is MemorySensitivity.PERSONAL
    with pytest.raises(MemoryConfigurationError, match="敏感"):
        store.create_confirmed(
            category="secret",
            content="我的密码是 synthetic-secret-123",
            source_turn_id=str(uuid4()),
        )


def test_undo_add_is_idempotent_and_suppresses_only_old_evidence(store):
    source = evidence("我喜欢猫")
    change = store.save_fact(proposal(), source)
    assert store.undo(change.id) is True
    assert store.undo(change.id) is False
    assert store.search("猫") == ()
    assert store.get(change.memory_id).content == ""
    assert store.save_fact(proposal(), source) is None
    assert store.save_fact(proposal(), evidence("我喜欢猫")) is not None


def name_proposal(name, *, update=False):
    text = f"我改名叫{name}" if update else f"我叫{name}"
    return proposal(content=text, fact_key="user.name", value=name, quote=text,
                    explicit_update=update)


def test_undo_update_restores_value_with_increasing_version(store):
    first = store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    source = evidence("我改名叫小李")
    update = store.save_fact(name_proposal("小李", update=True), source)
    assert store.undo(update.id)
    restored = store.get(first.memory_id)
    assert restored.value == "小王"
    assert restored.version == 3
    assert store.save_fact(name_proposal("小李", update=True), source) is None
    assert store.search("小李") == ()


def test_edit_blocks_stale_undo_and_updates_search_value(store):
    change = store.save_fact(proposal(), evidence("我喜欢猫"))
    edited = store.edit(change.memory_id, "用户喜欢狗", expected_version=1)
    assert edited.value == "用户喜欢狗"
    assert edited.origin == "manual"
    assert edited.version == 2
    assert store.search("猫") == ()
    assert store.search("狗")[0].id == edited.id
    with pytest.raises(MemoryConfigurationError, match="版本|修改"):
        store.undo(change.id)


def test_two_connections_cannot_edit_same_version(tmp_path):
    path = tmp_path / "race.db"
    first = MemoryStore(path, clock=lambda: NOW)
    second = MemoryStore(path, clock=lambda: NOW)
    try:
        record = first.create_confirmed(category="preference", content="猫",
                                        source_turn_id=str(uuid4()))
        first.edit(record.id, "狗", 1)
        with pytest.raises(MemoryConfigurationError, match="版本"):
            second.edit(record.id, "鸟", 1)
        assert second.get(record.id).value == "狗"
    finally:
        first.close()
        second.close()


def test_delete_clears_evidence_and_all_version_bodies(store):
    first_source = evidence("我叫小王")
    first = store.save_fact(name_proposal("小王"), first_source)
    update_source = evidence("我改名叫小李")
    store.save_fact(name_proposal("小李", update=True), update_source)
    assert store.delete(first.memory_id)
    record = store.get(first.memory_id)
    assert (record.content, record.value, record.keywords) == ("", "", ())
    assert store._connection.execute("SELECT quote FROM memory_evidence").fetchall() == []
    assert all(row[0] is None for row in store._connection.execute(
        "SELECT before_json FROM memory_changes"))
    assert store.save_fact(name_proposal("小王"), first_source) is None
    assert store.save_fact(name_proposal("小李", update=True), update_source) is None


def test_expired_memories_are_not_listed(store):
    store.create_confirmed(category="preference", content="猫", source_turn_id=str(uuid4()),
                           expires_at=NOW - timedelta(seconds=1))
    assert store.list_all() == ()


def test_background_write_checks_source_generation_and_body_inside_transaction(store):
    source = evidence("我喜欢猫")
    with pytest.raises(MemoryConfigurationError, match="来源|会话"):
        store.save_fact(proposal(), source, expected_generation=0)
    connection = store._connection
    connection.execute("INSERT INTO memory_sessions(id,title,created_at,updated_at) VALUES (?,?,?,?)",
                       (source.session_id, "测试", NOW.isoformat(), NOW.isoformat()))
    connection.execute(
        "INSERT INTO session_turns VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid4()), source.turn_id, source.session_id, source.user_text, "好", "2026-09-02",
         NOW.isoformat(), (NOW + timedelta(days=1)).isoformat()))
    assert store.save_fact(proposal(), source, expected_generation=0) is not None
    connection.execute("UPDATE memory_sessions SET generation=1, deleted=1")
    with pytest.raises(MemoryConfigurationError, match="来源|会话"):
        store.save_fact(proposal(), source, expected_generation=0)


def test_confirm_candidate_uses_fact_identity_and_blocks_conflicting_recall(store):
    old = store.create_confirmed(category="profile", content="我叫小王", value="小王",
                                 fact_key="user.name", source_turn_id=str(uuid4()))
    new = store.create_candidate("profile", "我叫小李", str(uuid4()), .5,
                                 fact_key="user.name", value="小李")
    assert store.confirm(new.id).status is MemoryStatus.CONFLICTED
    assert store.search("小王") == ()
    assert store.search("小王", statuses=frozenset({MemoryStatus.CONFIRMED,
                                                  MemoryStatus.CONFLICTED}))[0].id == old.id
    store.delete(new.id)
    assert store.search("小王")[0].id == old.id


def test_write_failure_rolls_back_fact_evidence_and_fts(store):
    from core.memory import MemoryStorageError

    store._connection.execute(
        "CREATE TRIGGER fail_change BEFORE INSERT ON memory_changes "
        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(MemoryStorageError):
        store.save_fact(proposal(), evidence("我喜欢猫"))
    assert store.list_all() == ()
    assert store.list_changes() == ()
    assert store._connection.execute("SELECT * FROM memory_evidence").fetchall() == []
    assert store.search("用户喜欢猫") == ()


def test_closed_store_mutation_reports_memory_error(tmp_path):
    closed = MemoryStore(tmp_path / "closed.db")
    closed.close()
    with pytest.raises(MemoryConfigurationError, match="关闭"):
        closed.save_fact(proposal(), evidence("我喜欢猫"))


def test_transaction_rolls_back_when_commit_fails(tmp_path):
    from core.memory_transactions import immediate_transaction

    connection = sqlite3.connect(tmp_path / "transaction.db", isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child(id INTEGER REFERENCES parent(id) "
                           "DEFERRABLE INITIALLY DEFERRED)")
        with pytest.raises(sqlite3.IntegrityError), immediate_transaction(connection):
            connection.execute("INSERT INTO child VALUES (1)")
        assert not connection.in_transaction
        assert connection.execute("SELECT * FROM child").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize("keyword", ["%", "_", '" OR "'])
def test_search_treats_punctuation_as_literal(store, keyword):
    store.create_confirmed(category="notes", content="用户喜欢猫", source_turn_id=str(uuid4()))
    assert store.search(keyword) == ()


def test_edit_then_delete_does_not_allow_original_source_replay(store):
    source = evidence("我喜欢猫")
    change = store.save_fact(proposal(), source)
    store.edit(change.memory_id, "用户喜欢狗", 1)
    store.delete(change.memory_id)
    assert store.save_fact(proposal(), source) is None


def test_explicit_confirmation_of_duplicate_candidate_reuses_and_confirms(store):
    candidate = store.create_candidate("preference", "我喜欢猫", str(uuid4()), .5,
                                       fact_key="user.pet", value="猫")
    confirmed = store.create_confirmed(category="preference", content="我喜欢猫",
                                       source_turn_id=str(uuid4()), fact_key="user.pet", value="猫")
    assert confirmed.id == candidate.id
    assert confirmed.status is MemoryStatus.CONFIRMED
    assert confirmed.version == 2


def test_second_clear_evidence_can_confirm_uncertain_fact(store):
    candidate = store.save_fact(proposal(confidence=.5), evidence("我喜欢猫"))
    saved = store.save_fact(proposal(), evidence("我喜欢猫"))
    assert saved.memory_id == candidate.memory_id
    assert store.get(saved.memory_id).status is MemoryStatus.CONFIRMED


def test_confirm_duplicate_candidate_must_not_return_another_candidate(store):
    first = store.create_candidate("preference", "我喜欢猫", str(uuid4()), .5,
                                   fact_key="user.pet", value="猫")
    second = store.create_candidate("preference", "我喜欢狗", str(uuid4()), .5,
                                    fact_key="user.pet", value="狗")
    store.edit(second.id, "猫", 1)
    result = store.confirm(second.id)
    assert result.status is MemoryStatus.CONFIRMED
    assert result.id in {first.id, second.id}
    assert len(store.list_all()) == 1


def test_explicit_inference_requires_response_context_and_recognizes_future_name():
    from core.memory_facts import infer_explicit_fact

    assert infer_explicit_fact("以后叫我 Tenko") == ("user.name", "Tenko")
    assert infer_explicit_fact("我喜欢简短的回答") == ("response.length", "concise")
    assert infer_explicit_fact("我在学英文歌曲") is None
    assert infer_explicit_fact("我喜欢详细的地图") is None


def test_resolve_conflict_replaces_only_linked_fact_and_relinks_alternatives(store):
    original = store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    selected = store.save_fact(name_proposal("小李"), evidence("我叫小李"))
    alternative = store.save_fact(name_proposal("小张"), evidence("我叫小张"))

    resolved = store.resolve_conflict(selected.memory_id)

    assert resolved.status is MemoryStatus.CONFIRMED
    assert store.get(original.memory_id).status is MemoryStatus.DELETED
    remaining = store.get(alternative.memory_id)
    assert remaining.status is MemoryStatus.CONFLICTED
    assert remaining.conflict_id == resolved.id


def test_explicit_correction_consolidates_matching_candidate_into_confirmed_id(store):
    original = store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    candidate = store.save_fact(
        proposal(
            content="我可能改名叫小李",
            fact_key="user.name",
            value="小李",
            quote="我可能改名叫小李",
            confidence=.5,
        ),
        evidence("我可能改名叫小李"),
    )

    corrected = store.save_fact(
        name_proposal("小李", update=True),
        evidence("我改名叫小李"),
    )

    assert corrected.kind == "update"
    assert corrected.memory_id == original.memory_id
    assert store.get(original.memory_id).value == "小李"
    assert store.get(original.memory_id).status is MemoryStatus.CONFIRMED
    assert store.get(candidate.memory_id).status is MemoryStatus.DELETED
    assert len(store.list_all()) == 1


def test_explicit_correction_preserves_existing_pending_alternative(store):
    original = store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    pending = store.save_fact(name_proposal("小张"), evidence("我叫小张"))

    corrected = store.save_fact(
        name_proposal("小李", update=True),
        evidence("我改名叫小李"),
    )

    assert corrected.kind == "update"
    assert corrected.memory_id == original.memory_id
    assert store.get(original.memory_id).value == "小李"
    alternative = store.get(pending.memory_id)
    assert alternative.status is MemoryStatus.CONFLICTED
    assert alternative.conflict_id == original.memory_id


def test_historical_source_replay_is_suppressed_but_new_evidence_can_conflict(store):
    old_source = evidence("我叫小王")
    original = store.save_fact(name_proposal("小王"), old_source)
    store.save_fact(name_proposal("小李", update=True), evidence("我改名叫小李"))

    assert store.save_fact(name_proposal("小王"), old_source) is None
    assert len(store.list_all()) == 1

    fresh = store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    assert fresh is not None
    assert fresh.memory_id != original.memory_id
    assert store.get(fresh.memory_id).status is MemoryStatus.CONFLICTED


def test_confirm_duplicate_of_conflicted_peer_records_manual_confirmation(store):
    original = store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    conflicted_change = store.save_fact(name_proposal("小李"), evidence("我叫小李"))
    conflicted = store.get(conflicted_change.memory_id)
    candidate = store.create_candidate(
        "profile",
        "我叫小张",
        str(uuid4()),
        .5,
        fact_key="user.name",
        value="小张",
    )
    duplicate_candidate = store.edit(candidate.id, "小李", candidate.version)

    confirmed = store.confirm(duplicate_candidate.id)

    assert confirmed.id == conflicted.id
    assert confirmed.status is MemoryStatus.CONFLICTED
    assert confirmed.origin == "manual"
    assert confirmed.version == conflicted.version + 1
    assert store.get(duplicate_candidate.id).status is MemoryStatus.DELETED
    assert store.get(original.memory_id).status is MemoryStatus.CONFIRMED
    assert store.search("小王") == ()


def test_edit_accepts_long_content_and_derives_a_bounded_current_value(store):
    record = store.create_confirmed(
        category="notes",
        content="旧正文",
        source_turn_id=str(uuid4()),
    )
    content = "新" * 1500 + "尾部关键词"

    edited = store.edit(record.id, content, record.version)

    assert edited.content == content
    assert edited.value == content[:1000]
    assert len(edited.value) == 1000
    assert store.search("旧正文") == ()
    assert store.search("尾部关键词")[0].id == record.id


def test_short_search_includes_keywords_with_literal_escaping(store):
    change = store.save_fact(
        proposal(keywords=("短%",)),
        evidence("我喜欢猫"),
    )

    assert store.search("短")[0].id == change.memory_id
    assert store.search("%")[0].id == change.memory_id
    assert store.search("_") == ()


@pytest.mark.parametrize(
    "user_text",
    ["我不喜欢猫", "我朋友喜欢猫", "我喜欢猫吗？"],
)
def test_obviously_non_assertive_evidence_is_not_auto_confirmed(store, user_text):
    change = store.save_fact(
        proposal(quote=user_text),
        evidence(user_text),
    )

    assert store.get(change.memory_id).status is MemoryStatus.CANDIDATE


def test_controlled_name_key_requires_naming_evidence_for_auto_confirmation(store):
    change = store.save_fact(
        proposal(
            content="称呼用户为猫",
            fact_key="user.name",
            value="猫",
            quote="我喜欢猫",
        ),
        evidence("我喜欢猫"),
    )

    assert store.get(change.memory_id).status is MemoryStatus.CANDIDATE


def test_non_text_memory_id_reports_configuration_error(store):
    with pytest.raises(MemoryConfigurationError, match="UUID"):
        store.get(b"not-a-uuid")


@pytest.mark.parametrize(
    ("fact_key", "value", "content"),
    [
        ("response.language", "english", "我不想用英文回答"),
        ("response.length", "concise", "我不喜欢简短回答"),
    ],
)
def test_controlled_alias_negation_is_not_auto_confirmed(
    store,
    fact_key,
    value,
    content,
):
    change = store.save_fact(
        proposal(
            content=content,
            fact_key=fact_key,
            value=value,
            quote=content,
        ),
        evidence(content),
    )

    assert store.get(change.memory_id).status is MemoryStatus.CANDIDATE


def test_resolve_relink_increments_alternative_version_and_rejects_stale_edit(store):
    store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    selected = store.save_fact(name_proposal("小李"), evidence("我叫小李"))
    alternative = store.save_fact(name_proposal("小张"), evidence("我叫小张"))
    stale_version = store.get(alternative.memory_id).version

    store.resolve_conflict(selected.memory_id)

    relinked = store.get(alternative.memory_id)
    assert relinked.conflict_id == selected.memory_id
    assert relinked.version == stale_version + 1
    with pytest.raises(MemoryConfigurationError, match="版本"):
        store.edit(alternative.memory_id, "我叫小赵", stale_version)


def test_duplicate_consolidation_relink_increments_dependent_version(store):
    current = store.save_fact(name_proposal("小王"), evidence("我叫小王"))
    duplicate = store.save_fact(
        proposal(
            content="我可能改名叫小李",
            fact_key="user.name",
            value="小李",
            quote="我可能改名叫小李",
            confidence=.5,
        ),
        evidence("我可能改名叫小李"),
    )
    dependent = store.save_fact(name_proposal("小张"), evidence("我叫小张"))
    store._connection.execute(
        "UPDATE memories SET conflict_id=? WHERE id=?",
        (duplicate.memory_id, dependent.memory_id),
    )
    stale_version = store.get(dependent.memory_id).version

    store.save_fact(name_proposal("小李", update=True), evidence("我改名叫小李"))

    relinked = store.get(dependent.memory_id)
    assert relinked.conflict_id == current.memory_id
    assert relinked.version == stale_version + 1
    with pytest.raises(MemoryConfigurationError, match="版本"):
        store.edit(dependent.memory_id, "我叫小赵", stale_version)
