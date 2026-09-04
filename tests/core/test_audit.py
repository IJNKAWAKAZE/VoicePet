import concurrent.futures
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.audit import (
    AuditConfigurationError,
    AuditContext,
    AuditStore,
    AuditUndoState,
)
from core.policy import (
    ConcurrencyPolicy,
    ConfirmationMode,
    PolicyDecision,
    RiskLevel,
    ToolManifest,
)
from core.tool_types import ToolExecutionResult, ToolExecutionStatus


def move_manifest():
    return ToolManifest(
        name="move_file",
        description="移动文件",
        input_schema={
            "type": "object",
            "maxProperties": 3,
            "properties": {
                "source": {"type": "string", "maxLength": 32767},
                "destination": {"type": "string", "maxLength": 32767},
                "options": {
                    "type": "object",
                    "maxProperties": 1,
                    "properties": {
                        "token": {"type": "string", "maxLength": 128},
                    },
                    "required": ["token"],
                    "additionalProperties": False,
                },
            },
            "required": ["source", "destination", "options"],
            "additionalProperties": False,
        },
        base_risk=RiskLevel.R2,
        timeout=30,
        concurrency_policy=ConcurrencyPolicy.SERIAL,
        supports_undo=True,
        sensitive_fields=("source", "destination", "options.token"),
    )


def context():
    return AuditContext(str(uuid4()), str(uuid4()), "call-1")


def decision(call_id="call-1"):
    return PolicyDecision(
        call_id,
        "move_file",
        True,
        RiskLevel.R2,
        ConfirmationMode.VOICE,
        "移动文件",
        "a" * 64,
        1,
        "允许执行",
    )


def successful_result():
    return ToolExecutionResult(
        ToolExecutionStatus.SUCCESS,
        {"moved": True, "stdout": "不应写入审计"},
        "文件移动完成",
        {"source": "C:/allowed/a.txt", "destination": "C:/allowed/b.txt"},
    )


def write_record(store, **overrides):
    values = {
        "context": context(),
        "manifest": move_manifest(),
        "arguments": {
            "source": "C:/private/source.txt",
            "destination": "C:/private/destination.txt",
            "options": {"token": "secret-token"},
        },
        "decision": decision(),
        "result": successful_result(),
        "duration_ms": 25,
        "occurred_at_utc": datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return store.write(**values)


def test_audit_migration_is_idempotent_and_enables_sqlite_safety_pragmas(tmp_path):
    database = tmp_path / "audit.db"
    first = AuditStore(database)

    assert first.diagnostics() == {
        "schema_version": 1,
        "journal_mode": "wal",
        "foreign_keys": True,
        "busy_timeout_ms": 5000,
    }
    first.close()

    second = AuditStore(database)
    assert second.diagnostics()["schema_version"] == 1
    second.close()


def test_audit_record_round_trip_is_immutable_and_excludes_result_payload(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    record = write_record(store)
    loaded = store.get(record.audit_id)

    assert loaded == record
    assert record.risk is RiskLevel.R2
    assert record.confirmation is ConfirmationMode.VOICE
    assert record.status is ToolExecutionStatus.SUCCESS
    assert record.undo_state is AuditUndoState.AVAILABLE
    assert record.undo_data == successful_result().undo_data
    assert "stdout" not in record.arguments
    with pytest.raises(TypeError):
        record.arguments["source"] = "changed"
    store.close()


def test_audit_redacts_nested_fields_without_mutating_original_arguments(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    arguments = {
        "source": "C:/private/source.txt",
        "destination": "C:/private/destination.txt",
        "options": {"token": "secret-token"},
    }

    record = write_record(store, arguments=arguments)

    assert record.arguments == {
        "source": "[REDACTED]",
        "destination": "[REDACTED]",
        "options": {"token": "[REDACTED]"},
    }
    assert arguments["options"]["token"] == "secret-token"
    store.close()


def test_audit_database_does_not_store_sensitive_arguments_or_result_output(tmp_path):
    database = tmp_path / "audit.db"
    store = AuditStore(database)
    write_record(store)
    store.close()

    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT arguments_json, safe_message FROM audit_records"
    ).fetchone()
    connection.close()

    combined = " ".join(row)
    assert "secret-token" not in combined
    assert "private/source" not in combined
    assert "不应写入审计" not in combined


def test_audit_caps_safe_message_and_lists_recent_records(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    base = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    first = write_record(
        store,
        result=ToolExecutionResult(
            ToolExecutionStatus.FAILED,
            {},
            "x" * 600,
        ),
        occurred_at_utc=base,
    )
    second = write_record(
        store,
        context=AuditContext(str(uuid4()), str(uuid4()), "call-2"),
        decision=decision("call-2"),
        occurred_at_utc=base + timedelta(seconds=1),
    )

    recent = store.list_recent(limit=2)

    assert [item.audit_id for item in recent] == [second.audit_id, first.audit_id]
    assert len(first.safe_message) == 512
    with pytest.raises(AuditConfigurationError):
        store.list_recent(limit=1001)
    store.close()


def test_audit_store_rejects_use_after_close_and_invalid_context(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    with pytest.raises(AuditConfigurationError):
        write_record(
            store,
            context=AuditContext("not-a-uuid", str(uuid4()), "call-1"),
        )
    store.close()
    store.close()
    with pytest.raises(AuditConfigurationError, match="关闭"):
        store.get(str(uuid4()))


def test_undo_state_reserve_complete_and_release_are_conditional(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    first = write_record(store)
    operation = str(uuid4())

    assert store.reserve_undo(first.audit_id, operation) is True
    assert store.reserve_undo(first.audit_id, str(uuid4())) is False
    assert store.release_undo(first.audit_id, str(uuid4())) is False
    assert store.release_undo(first.audit_id, operation) is True
    assert store.reserve_undo(first.audit_id, operation) is True
    assert store.complete_undo(first.audit_id, operation) is True
    assert store.get(first.audit_id).undo_state is AuditUndoState.COMPLETED
    assert store.get(first.audit_id).undone_by == operation
    assert store.reserve_undo(first.audit_id, str(uuid4())) is False
    store.close()


def test_concurrent_undo_reservation_has_exactly_one_winner(tmp_path):
    database = tmp_path / "audit.db"
    creator = AuditStore(database)
    record = write_record(creator)
    creator.close()

    def reserve():
        store = AuditStore(database)
        try:
            return store.reserve_undo(record.audit_id, str(uuid4()))
        finally:
            store.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reserve(), range(2)))

    assert sorted(results) == [False, True]
