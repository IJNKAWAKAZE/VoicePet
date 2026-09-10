import asyncio
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.agent_types import AgentEventType
from core.config import PrivacyConfig
from core.coordinator import Coordinator
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    MemoryChanged,
    MemoryResultReady,
)
from core.memory import MemoryConfigurationError, MemoryStore
from core.memory_data import MemoryDataManager
from core.memory_facts import MemoryChange
from core.memory_intents import MemoryOperationService
from core.runtime import RuntimeHost, RuntimeServices
from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext


class Audio:
    async def start_recording(self, token):
        return None

    async def stop_recording(self, token):
        return b""

    async def abort(self):
        return None


class Transcript:
    async def transcribe(self, audio, token):
        return ""


class Lifecycle:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    async def start(self):
        self.events.append(f"start:{self.name}")

    async def stop(self):
        self.events.append(f"stop:{self.name}")


class Activation:
    async def activate(self, source):
        raise AssertionError("测试不应激活语音")


class RuntimeCoordinator:
    phase = ConversationPhase.IDLE

    async def stop(self):
        return None


def _agent_text(text):
    return SimpleNamespace(type=AgentEventType.TEXT_DELTA, payload={"text": text})


def _agent_completed():
    return SimpleNamespace(
        type=AgentEventType.TURN_COMPLETED, payload={"status": "completed"}
    )


class ImmediateGateway:
    async def run_turn(self, session, text, **kwargs):
        yield _agent_text("完整回复")
        yield _agent_completed()

    async def cancel(self):
        return None


class DelayedGateway:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn(self, session, text, **kwargs):
        self.started.set()
        await self.release.wait()
        yield _agent_text("完整回复")
        yield _agent_completed()

    async def cancel(self):
        return None


async def wait_idle(coordinator):
    for _ in range(200):
        if coordinator.phase is ConversationPhase.IDLE:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Coordinator 未恢复空闲")


def test_coordinator_enqueues_completed_archive_for_captured_session(tmp_path):
    async def scenario():
        context = SessionContext()
        captured_session_id = context.session_id
        archive = SessionArchiveStore(tmp_path / "assistant.db")
        queued = []
        llm = DelayedGateway()
        coordinator = Coordinator(
            Audio(),
            Transcript(),
            EventBus(),
            agent_gateway=llm,
            session_context=context,
            session_archive=archive,
            archive_completion=lambda session_id, turn_id: queued.append(
                (session_id, turn_id)
            ),
        )

        turn_id = await coordinator.submit_text("原会话问题")
        await llm.started.wait()
        context.clear()
        llm.release.set()
        await wait_idle(coordinator)

        assert queued == [(captured_session_id, str(turn_id))]
        assert [item.turn_id for item in archive.list_session_turns(captured_session_id)] == [
            str(turn_id)
        ]
        assert archive.list_session_turns(context.session_id) == ()
        await coordinator.stop()
        archive.close()

    asyncio.run(scenario())


def test_coordinator_does_not_enqueue_cancelled_completion(tmp_path):
    async def scenario():
        context = SessionContext()
        archive = SessionArchiveStore(tmp_path / "assistant.db")
        queued = []
        llm = DelayedGateway()
        coordinator = Coordinator(
            Audio(),
            Transcript(),
            EventBus(),
            agent_gateway=llm,
            session_context=context,
            session_archive=archive,
            archive_completion=lambda session_id, turn_id: queued.append(
                (session_id, turn_id)
            ),
        )

        await coordinator.submit_text("稍后取消")
        await llm.started.wait()
        await coordinator.cancel_active_turn()
        llm.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert queued == []
        assert archive.list_turns() == ()
        await coordinator.stop()
        archive.close()

    asyncio.run(scenario())


def test_archive_failure_does_not_prevent_ordinary_reply_completion():
    class FailingArchive:
        def archive_turn(self, *args):
            raise RuntimeError("database unavailable")

        def discard_turn(self, turn_id):
            return False

    async def scenario():
        coordinator = Coordinator(
            Audio(),
            Transcript(),
            EventBus(),
            agent_gateway=ImmediateGateway(),
            session_context=SessionContext(),
            session_archive=FailingArchive(),
            archive_completion=lambda session_id, turn_id: (_ for _ in ()).throw(
                AssertionError("归档失败不应通知调度器")
            ),
        )

        await coordinator.submit_text("普通问题")
        await wait_idle(coordinator)

        assert coordinator.response_text == "完整回复"
        await coordinator.stop()

    asyncio.run(scenario())


def test_runtime_memory_configuration_and_invalidation_are_immediate():
    events = []

    class Scheduler(Lifecycle):
        enabled = True

        def configure(self, enabled):
            self.enabled = enabled
            events.append(("scheduler:configure", enabled))

        def invalidate(self):
            events.append("scheduler:invalidate")

        def status(self):
            return {"status": "idle", "message": ""}

        def enqueue(self):
            if self.enabled:
                events.append("scheduler:old-provider-request")

    class ContextPolicy:
        def configure(self, enabled):
            events.append(("context:configure", enabled))

    class StorePolicy:
        def set_enabled(self, enabled):
            events.append(("store:configure", enabled))

    class ArchivePolicy:
        def configure(self, *, enabled, retention_days, summary_retention_days):
            events.append(
                ("archive:configure", enabled, retention_days, summary_retention_days)
            )

    scheduler = Scheduler("scheduler", events)
    services = RuntimeServices(
        EventBus(),
        RuntimeCoordinator(),
        Activation(),
        Lifecycle("capture", events),
        closers=(lambda: events.append("close:database"),),
        scheduler=scheduler,
        memory_context=ContextPolicy(),
        memory_store=StorePolicy(),
        session_archive=ArchivePolicy(),
    )
    host = RuntimeHost(services)
    privacy = PrivacyConfig(
        memory_enabled=True,
        chat_history_enabled=False,
        auto_memory_enabled=True,
        chat_retention_days=12,
        summary_retention_days=30,
    )

    host.start()
    try:
        host.configure_memory(privacy).result(timeout=1)
        assert host.memory_status().result(timeout=1) == {
            "status": "idle",
            "message": "",
        }
        host.invalidate_memory().result(timeout=1)
        host.configure_memory(PrivacyConfig()).result(timeout=1)
        scheduler.enqueue()
    finally:
        host.close()

    assert ("context:configure", True) in events
    assert ("store:configure", True) in events
    assert ("archive:configure", False, 12, 30) in events
    assert ("scheduler:configure", False) in events
    assert [item for item in events if isinstance(item, tuple) and item[0] == "scheduler:configure"][-1] == (
        "scheduler:configure",
        False,
    )
    assert "scheduler:old-provider-request" not in events
    assert events.index("stop:scheduler") < events.index("stop:capture")
    assert events.index("stop:scheduler") < events.index("close:database")


def test_runtime_future_memory_apis_publish_real_change_identity():
    events = []
    bus = EventBus()
    changed = []
    bus.subscribe(MemoryChanged, changed.append)
    session_id = str(uuid4())
    change_id = str(uuid4())
    record = SimpleNamespace(session_id=session_id)
    change = SimpleNamespace(id=change_id, session_id=session_id)
    summary = SimpleNamespace(id="summary-1", session_id=session_id)

    class Memory:
        def edit(self, memory_id, content, expected_version):
            events.append(("edit", threading.get_ident()))
            return record

        def undo(self, requested_change_id):
            assert requested_change_id == change_id
            return True

        def list_changes(self, requested_session_id=None):
            if requested_session_id is None:
                return (change,)
            assert requested_session_id == session_id
            return (change,)

        def list_records(self):
            return ()

        def delete(self, memory_id):
            return False

        def confirm(self, memory_id):
            return record

        def resolve_conflict(self, memory_id):
            return record

        def export_json(self, destination):
            return 0

    class Sessions:
        def list_summaries(self, requested_session_id=None):
            assert requested_session_id in {None, session_id}
            return (summary,)

        def delete_summary(self, summary_id):
            return summary_id == "summary-1"

    host = RuntimeHost(
        RuntimeServices(
            bus,
            RuntimeCoordinator(),
            Activation(),
            Lifecycle("capture", events),
            memory=Memory(),
            sessions=Sessions(),
        )
    )
    caller = threading.get_ident()

    host.start()
    try:
        assert host.edit_memory("memory-1", "新正文", 1).result(timeout=1) is record
        assert host.undo_memory(change_id).result(timeout=1) is True
        assert host.list_memory_changes(session_id).result(timeout=1) == (change,)
        assert host.list_summaries(session_id).result(timeout=1) == (summary,)
        assert host.delete_summary("summary-1").result(timeout=1) is True
    finally:
        host.close()

    assert next(item for item in events if isinstance(item, tuple) and item[0] == "edit")[1] != caller
    assert changed == [
        MemoryChanged(session_id, ()),
        MemoryChanged(session_id, (change_id,)),
        MemoryChanged(session_id, ()),
    ]


def test_manual_edit_does_not_claim_concurrent_background_change():
    bus = EventBus()
    changed = []
    bus.subscribe(MemoryChanged, changed.append)
    session_id = str(uuid4())
    unrelated_change = MemoryChange(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        "add",
        1,
        datetime.now(UTC),
        False,
    )
    started = threading.Event()
    release = threading.Event()

    class ConcurrentMemory:
        def __init__(self):
            self.changes = []

        def edit(self, memory_id, content, expected_version):
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("测试未释放手动编辑")
            return SimpleNamespace(session_id=session_id)

        def list_changes(self, requested_session_id=None):
            return tuple(self.changes)

    memory = ConcurrentMemory()
    host = RuntimeHost(
        RuntimeServices(
            bus,
            RuntimeCoordinator(),
            Activation(),
            Lifecycle("capture", []),
            memory=memory,
        )
    )
    host.start()
    try:
        future = host.edit_memory("memory-1", "新正文", 1)
        assert started.wait(timeout=1)
        memory.changes.append(unrelated_change)
        release.set()
        future.result(timeout=1)
    finally:
        release.set()
        host.close()

    assert changed == [MemoryChanged(session_id, ())]


def test_memory_data_adds_non_sensitive_source_metadata(tmp_path):
    path = tmp_path / "assistant.db"
    archive = SessionArchiveStore(path)
    store = MemoryStore(path)
    turn_id = str(uuid4())
    archived = archive.archive_turn(turn_id, "不能向 UI 泄露的正文", "回复")
    record = store.create_confirmed(
        category="preference",
        content="喜欢猫",
        source_turn_id=turn_id,
        session_id=archived.session_id,
    )
    manager = MemoryDataManager(store, archive=archive)

    available = manager.list_records()[0]
    assert available.source_title == "不能向 UI 泄露的正文"
    assert available.source_state == "available"
    assert available.source_time == archived.created_at
    assert available.session_id == archived.session_id

    archive.discard_session(archived.session_id)
    deleted = manager.list_records()[0]
    assert deleted.source_title == ""
    assert deleted.source_state == "deleted"
    assert deleted.source_time == archived.created_at
    assert deleted.session_id == archived.session_id
    assert record.content == deleted.content
    archive.close()
    store.close()


def test_disabled_memory_keeps_explicit_local_management_available(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    record = store.create_confirmed(
        category="preference",
        content="喜欢猫",
        source_turn_id=str(uuid4()),
    )
    manager = MemoryDataManager(store)
    store.set_enabled(False)

    assert manager.list_records()[0].source_state == "manual"
    edited = manager.edit(record.id, "喜欢猫和狗", record.version)
    assert edited.content == "喜欢猫和狗"
    assert manager.delete(record.id) is True
    with pytest.raises(MemoryConfigurationError, match="记忆写入已关闭"):
        store.create_confirmed(
            category="preference",
            content="后台或聊天写入",
            source_turn_id=str(uuid4()),
        )
    store.close()


def test_privacy_only_config_change_is_immediate():
    from core.config import AppConfig
    from core.config_application import ConfigApplicationMode, classify_config_change

    previous = AppConfig()
    current = replace(
        previous,
        privacy=replace(
            previous.privacy,
            memory_enabled=False,
            chat_history_enabled=False,
            auto_memory_enabled=False,
            chat_retention_days=21,
            summary_retention_days=60,
        ),
    )

    assert (
        classify_config_change(previous, current)
        is ConfigApplicationMode.IMMEDIATE_ONLY
    )


def test_disabled_memory_explicit_remember_reports_not_enabled(tmp_path):
    async def scenario():
        store = MemoryStore(tmp_path / "assistant.db", enabled=False)
        bus = EventBus()
        approvals = []
        results = []
        bus.subscribe(ApprovalRequested, approvals.append)
        bus.subscribe(MemoryResultReady, results.append)
        coordinator = Coordinator(
            Audio(),
            Transcript(),
            bus,
            memory_operations=MemoryOperationService(store),
        )

        await coordinator.submit_text("记住我喜欢猫")
        for _ in range(100):
            if approvals:
                break
            await asyncio.sleep(0)
        await coordinator.approve_pending()

        assert results[-1].status == "failed"
        assert results[-1].message == "记忆功能未启用，未保存这条内容"
        assert store.list_all() == ()
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())


def test_factory_wires_background_memory_pipeline(tmp_path):
    from core.config import AppConfig
    from core.runtime_factory import build_default_runtime

    services = build_default_runtime(
        AppConfig(),
        EventBus(),
        tmp_path / "VoicePet",
        api_key="isolated-test-key",
    )
    try:
        assert services.scheduler is not None
        assert services.scheduler._extractor is not None
        assert services.memory_context is services.coordinator._memory_context
        assert services.session_archive._retention_days == 7
        assert services.session_archive._summary_retention_days == 7
    finally:
        RuntimeHost(services).close()


def test_factory_keeps_cleanup_scheduler_when_llm_is_not_configured(tmp_path):
    from core.config import AppConfig
    from core.runtime_factory import build_default_runtime

    services = build_default_runtime(AppConfig(), EventBus(), tmp_path / "VoicePet")
    try:
        assert services.scheduler is not None
        assert services.scheduler._extractor is None
        services.scheduler.configure(True)
        assert services.scheduler.status()["status"] == "disabled"
    finally:
        RuntimeHost(services).close()


def test_factory_validates_legacy_memory_schema_before_opening_other_resources(
    tmp_path,
    monkeypatch,
):
    from core.config import AppConfig
    from core.memory_schema import MemorySchemaError
    from core.runtime_factory import build_default_runtime

    data_root = tmp_path / "VoicePet"
    data_directory = data_root / "data"
    data_directory.mkdir(parents=True)
    connection = sqlite3.connect(data_directory / "assistant.db")
    connection.execute("CREATE TABLE memories(id TEXT PRIMARY KEY)")
    connection.close()

    def unexpected_resource(*args, **kwargs):
        raise AssertionError("旧记忆结构校验前不应打开其他持久资源")

    monkeypatch.setattr("core.runtime_factory.StructuredLogStore", unexpected_resource)

    with pytest.raises(MemorySchemaError, match="旧记忆结构"):
        build_default_runtime(AppConfig(), EventBus(), data_root)
