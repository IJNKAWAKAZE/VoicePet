import asyncio
import json
from datetime import datetime

import pytest

import core
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    CorrelationId,
    ErrorSeverity,
    MemoryResultReady,
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TranscriptReady,
    TurnId,
    WakeCommandPending,
)
from core.structured_logging import (
    RuntimeEventLogger,
    StructuredLogError,
    StructuredLogStore,
)


def test_structured_logging_types_are_publicly_exported():
    assert core.StructuredLogError is StructuredLogError
    assert core.StructuredLogStore is StructuredLogStore
    assert core.RuntimeEventLogger is RuntimeEventLogger


def test_store_writes_correlated_json_after_redacting_sensitive_data(tmp_path):
    store = StructuredLogStore(tmp_path / "logs")

    store.write(
        "info",
        "runtime",
        (
            "Bearer abcdefghijklmnopqrstuvwxyz "
            "email=a@example.com user=C:\\Users\\Alice\\Desktop "
            "ip=192.168.1.8"
        ),
        turn_id="turn-1",
        correlation_id="correlation-1",
        context={
            "phase": "THINKING",
            "api_key": "sk-secret-value",
            "password": "open-sesame",
            "prompt": "private prompt",
            "arguments": {"path": "C:\\private.txt"},
            "content": "private file content",
        },
    )

    raw = (tmp_path / "logs" / "voicepet.jsonl").read_text("utf-8")
    record = json.loads(raw)
    datetime.fromisoformat(record["timestamp_utc"])
    assert record["level"] == "INFO"
    assert record["component"] == "runtime"
    assert record["turn_id"] == "turn-1"
    assert record["correlation_id"] == "correlation-1"
    assert record["context"]["phase"] == "THINKING"
    assert "[REDACTED]" in raw
    for secret in (
        "abcdefghijklmnopqrstuvwxyz",
        "a@example.com",
        "Alice",
        "192.168.1.8",
        "sk-secret-value",
        "open-sesame",
        "private prompt",
        "private.txt",
        "private file content",
    ):
        assert secret not in raw


def test_store_rotates_with_bounded_backups_and_reads_latest_tail(tmp_path):
    store = StructuredLogStore(
        tmp_path / "logs",
        max_bytes=240,
        backup_count=2,
    )

    for index in range(12):
        store.write("info", "test", f"entry-{index}-" + "x" * 80)

    files = tuple((tmp_path / "logs").glob("voicepet.jsonl*"))
    tail = store.tail(max_bytes=10_000)

    assert len(files) <= 3
    assert "entry-11-" in tail
    assert "entry-0-" not in tail


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"backup_count": 0},
    ],
)
def test_store_rejects_invalid_rotation_limits(tmp_path, kwargs):
    with pytest.raises(StructuredLogError):
        StructuredLogStore(tmp_path / "logs", **kwargs)


def test_store_rejects_invalid_tail_limit(tmp_path):
    store = StructuredLogStore(tmp_path / "logs")

    with pytest.raises(StructuredLogError):
        store.tail(max_bytes=0)


def test_event_logger_records_ids_and_safe_metadata_without_event_bodies(tmp_path):
    async def scenario():
        bus = EventBus()
        store = StructuredLogStore(tmp_path / "logs")
        event_logger = RuntimeEventLogger(bus, store)
        turn_id = TurnId.new()
        correlation_id = CorrelationId.new()
        events = (
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.IDLE,
                ConversationPhase.LISTENING,
            ),
            TranscriptReady(turn_id, correlation_id, "private transcript"),
            WakeCommandPending(turn_id, correlation_id),
            ApprovalRequested(
                turn_id,
                correlation_id,
                "call-1",
                "private approval summary",
                "R2",
            ),
            MemoryResultReady(
                turn_id,
                correlation_id,
                "success",
                "private memory content",
                1,
            ),
            SpeakRequested(turn_id, correlation_id, "private spoken text"),
            RuntimeErrorEvent(
                turn_id,
                correlation_id,
                "llm.network",
                "llm",
                ErrorSeverity.WARNING,
                True,
                False,
                "Bearer abcdefghijklmnopqrstuvwxyz",
                {"exception_type": "NetworkError"},
            ),
        )
        for event in events:
            await bus.publish(event)

        before_close = store.path.read_text("utf-8")
        event_logger.close()
        await bus.publish(events[0])
        after_close = store.path.read_text("utf-8")
        return events, turn_id, correlation_id, before_close, after_close

    events, turn_id, correlation_id, raw, after_close = asyncio.run(scenario())
    records = [json.loads(line) for line in raw.splitlines()]

    assert len(records) == len(events)
    assert {record["event_type"] for record in records} == {
        type(event).__name__ for event in events
    }
    assert all(record["turn_id"] == str(turn_id) for record in records)
    assert all(
        record["correlation_id"] == str(correlation_id) for record in records
    )
    assert after_close == raw
    for private_text in (
        "private transcript",
        "private approval summary",
        "private memory content",
        "private spoken text",
        "abcdefghijklmnopqrstuvwxyz",
    ):
        assert private_text not in raw
    error_record = next(
        record for record in records
        if record["event_type"] == "RuntimeErrorEvent"
    )
    assert error_record["level"] == "WARNING"
    assert error_record["component"] == "llm"
    assert error_record["context"] == {
        "error_code": "llm.network",
        "exception_type": "NetworkError",
        "retryable": True,
        "user_action_required": False,
    }


def test_event_logger_never_breaks_delivery_when_log_write_fails():
    class BrokenStore:
        def write(self, *args, **kwargs):
            raise StructuredLogError("disk unavailable")

    async def scenario():
        bus = EventBus()
        RuntimeEventLogger(bus, BrokenStore())
        seen = []
        bus.subscribe(StateChanged, seen.append)
        event = StateChanged(
            TurnId.new(),
            CorrelationId.new(),
            ConversationPhase.IDLE,
            ConversationPhase.LISTENING,
        )

        await bus.publish(event)

        assert seen == [event]

    asyncio.run(scenario())
