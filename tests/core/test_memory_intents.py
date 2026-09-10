import asyncio
import threading
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.cancellation import CancellationToken
from core.coordinator import Coordinator
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    MemoryResultReady,
    RuntimeErrorEvent,
)
from core.memory import MemoryStore
from core.memory_intents import (
    MemoryIntentAction,
    MemoryIntentParser,
    MemoryOperationService,
)
from core.session_archive import SessionArchiveStore


def test_memory_intent_parser_recognizes_bounded_chinese_commands():
    parser = MemoryIntentParser()

    remember = parser.parse("请记住：我喜欢低音量播报")
    forget = parser.parse("忘掉低音量播报")
    listing = parser.parse("你记得什么")
    clear = parser.parse("清空今天对话")

    assert (remember.action, remember.content) == (
        MemoryIntentAction.REMEMBER,
        "我喜欢低音量播报",
    )
    assert (forget.action, forget.content) == (
        MemoryIntentAction.FORGET,
        "低音量播报",
    )
    assert listing.action is MemoryIntentAction.LIST
    assert clear.action is MemoryIntentAction.CLEAR_TODAY
    assert parser.parse("帮我写一份报告") is None


def test_memory_operation_service_executes_only_explicit_plan(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    service = MemoryOperationService(store, today=lambda: date(2026, 9, 2))
    source_turn_id = str(uuid4())

    remember = service.plan("记住我喜欢低音量播报", source_turn_id)
    assert remember.requires_confirmation is True
    created = service.execute(remember)
    assert created.records[0].content == "我喜欢低音量播报"

    listing = service.execute(service.plan("你记得什么", source_turn_id))
    assert [record.id for record in listing.records] == [created.records[0].id]

    forgotten = service.execute(
        service.plan("忘掉低音量播报", source_turn_id)
    )
    assert forgotten.affected == 1
    assert store.list_all() == ()
    store.close()


@pytest.mark.parametrize(
    ("command", "fact_key", "value", "explicit_update"),
    [
        ("记住我叫小王", "user.name", "小王", False),
        ("记住我改名叫小李", "user.name", "小李", True),
        ("记住回答简短一点", "response.length", "concise", False),
        ("记住从现在起请用英文回答", "response.language", "english", True),
        ("记住语气正式", "response.style", "formal", False),
    ],
)
def test_explicit_remember_forwards_controlled_fact_identity(
    command,
    fact_key,
    value,
    explicit_update,
):
    class RecordingStore:
        def __init__(self):
            self.kwargs = None

        def create_confirmed(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(content=kwargs["content"])

    store = RecordingStore()
    service = MemoryOperationService(store)
    source_turn_id = str(uuid4())

    result = service.execute(service.plan(command, source_turn_id))

    assert result.records[0].content == command.removeprefix("记住")
    assert store.kwargs == {
        "category": "user_requested",
        "content": command.removeprefix("记住"),
        "source_turn_id": source_turn_id,
        "fact_key": fact_key,
        "value": value,
        "explicit_update": explicit_update,
    }


def test_clear_today_removes_session_layer_without_deleting_long_term_memory(
    tmp_path,
):
    database = tmp_path / "assistant.db"
    store = MemoryStore(database)
    archive = SessionArchiveStore(database)
    long_term = store.create_confirmed(
        category="preference",
        content="长期偏好",
        source_turn_id=str(uuid4()),
    )
    turn_id = str(uuid4())
    archived = archive.archive_turn(turn_id, "今天的问题", "今天的回复")
    archive.save_summary(turn_id, "今天的话题", ())
    service = MemoryOperationService(
        store,
        session_archive=archive,
        today=lambda: archived.session_date,
    )

    result = service.execute(service.plan("清空今天对话", str(uuid4())))

    assert result.affected == 2
    assert archive.list_turns() == ()
    assert archive.list_summaries() == ()
    assert store.get(long_term.id).content == "长期偏好"
    archive.close()
    store.close()


def test_coordinator_requires_confirmation_before_explicit_memory_write(tmp_path):
    class Audio:
        async def record_until_silence(self, token: CancellationToken):
            return b"audio"

    class Transcript:
        async def transcribe(self, audio, token):
            return "记住我喜欢低音量播报"

    async def scenario():
        store = MemoryStore(tmp_path / "assistant.db")
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

        await coordinator.start_listening()
        for _ in range(100):
            if approvals:
                break
            await asyncio.sleep(0)
        assert store.list_all() == ()
        assert approvals[0].risk == "隐私"

        await coordinator.approve_pending()
        assert store.list_all()[0].content == "我喜欢低音量播报"
        assert results[-1].status == "success"
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())


def test_expired_turn_rejects_approved_memory_write(tmp_path):
    class Audio:
        async def record_until_silence(self, token: CancellationToken):
            return b"audio"

    class Transcript:
        async def transcribe(self, audio, token):
            return "记住我喜欢低音量播报"

    async def scenario():
        current = [0.0]
        store = MemoryStore(tmp_path / "assistant.db")
        bus = EventBus()
        approvals = []
        errors = []
        bus.subscribe(ApprovalRequested, approvals.append)
        bus.subscribe(RuntimeErrorEvent, errors.append)
        coordinator = Coordinator(
            Audio(),
            Transcript(),
            bus,
            memory_operations=MemoryOperationService(store),
            max_turn_duration=0.01,
            clock=lambda: current[0],
        )

        await coordinator.start_listening()
        for _ in range(100):
            if approvals:
                break
            await asyncio.sleep(0)
        current[0] = 0.02
        await coordinator.approve_pending()

        assert store.list_all() == ()
        assert errors[-1].error_code == "runtime.budget"
        assert coordinator.phase is ConversationPhase.IDLE
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())


def test_interrupt_discards_completed_explicit_memory_result(tmp_path):
    class Audio:
        async def record_until_silence(self, token: CancellationToken):
            return b"audio"

    class Transcript:
        async def transcribe(self, audio, token):
            return "记住我喜欢低音量播报"

    async def scenario():
        started = threading.Event()
        release = threading.Event()
        store = MemoryStore(tmp_path / "assistant.db")
        delegate = MemoryOperationService(store)

        class BlockingOperations:
            def plan(self, text, source_turn_id):
                return delegate.plan(text, source_turn_id)

            def execute(self, operation):
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("测试未释放记忆写入")
                return delegate.execute(operation)

        bus = EventBus()
        approvals = []
        results = []
        first_approval = asyncio.Event()
        second_approval = asyncio.Event()

        def record_approval(event):
            approvals.append(event)
            if len(approvals) == 1:
                first_approval.set()
            elif len(approvals) == 2:
                second_approval.set()

        bus.subscribe(ApprovalRequested, record_approval)
        bus.subscribe(MemoryResultReady, results.append)
        coordinator = Coordinator(
            Audio(),
            Transcript(),
            bus,
            memory_operations=BlockingOperations(),
        )

        old_turn = await coordinator.start_listening()
        await asyncio.wait_for(first_approval.wait(), timeout=1)
        approval_task = asyncio.create_task(
            coordinator.approve_pending()
        )
        assert await asyncio.to_thread(started.wait, 1)

        new_turn = await coordinator.interrupt()
        await asyncio.wait_for(second_approval.wait(), timeout=1)
        assert new_turn != old_turn
        assert coordinator.phase is ConversationPhase.AWAITING_APPROVAL

        release.set()
        await approval_task

        assert coordinator.phase is ConversationPhase.AWAITING_APPROVAL
        assert all(result.turn_id != old_turn for result in results)
        assert len(store.list_all()) == 1
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())


def test_expired_started_memory_write_reports_result_then_recovers(tmp_path):
    class Audio:
        async def record_until_silence(self, token: CancellationToken):
            return b"audio"

    class Transcript:
        async def transcribe(self, audio, token):
            return "记住我喜欢低音量播报"

    async def scenario():
        current = [0.0]
        store = MemoryStore(tmp_path / "assistant.db")
        delegate = MemoryOperationService(store)

        class ExpiringOperations:
            def plan(self, text, source_turn_id):
                return delegate.plan(text, source_turn_id)

            def execute(self, operation):
                result = delegate.execute(operation)
                current[0] = 1.0
                return result

        bus = EventBus()
        approvals = []
        results = []
        errors = []
        bus.subscribe(ApprovalRequested, approvals.append)
        bus.subscribe(MemoryResultReady, results.append)
        bus.subscribe(RuntimeErrorEvent, errors.append)
        coordinator = Coordinator(
            Audio(),
            Transcript(),
            bus,
            memory_operations=ExpiringOperations(),
            max_turn_duration=0.5,
            clock=lambda: current[0],
        )

        await coordinator.start_listening()
        for _ in range(100):
            if approvals:
                break
            await asyncio.sleep(0)
        await coordinator.approve_pending()

        assert len(store.list_all()) == 1
        assert results[-1].status == "success"
        assert errors[-1].error_code == "runtime.budget"
        assert coordinator.phase is ConversationPhase.IDLE
        await coordinator.stop()
        store.close()

    asyncio.run(scenario())
