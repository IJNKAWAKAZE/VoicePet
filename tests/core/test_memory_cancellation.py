import asyncio
from uuid import uuid4

import pytest

from core.coordinator import Coordinator
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    MemoryResultReady,
    RuntimeErrorEvent,
    StateChanged,
)
from core.memory import MemoryStore
from core.memory_intents import MemoryOperationService


@pytest.mark.parametrize("command", ["记住：我喜欢喝茶", "忘记：我喜欢喝茶", "清空今天对话"])
def test_cancel_memory_operation_keeps_data_and_returns_to_idle(tmp_path, command):
    async def scenario():
        store = MemoryStore(tmp_path / "memory.db")
        store.create_confirmed(category="preference", content="我喜欢喝茶", source_turn_id=str(uuid4()))
        original = store.list_all()
        bus = EventBus()
        approved = asyncio.Event()
        results, errors, phases = [], [], []
        bus.subscribe(ApprovalRequested, lambda event: approved.set())
        bus.subscribe(MemoryResultReady, results.append)
        bus.subscribe(RuntimeErrorEvent, errors.append)
        bus.subscribe(StateChanged, lambda event: phases.append(event.current))
        coordinator = Coordinator(object(), object(), bus, memory_operations=MemoryOperationService(store))
        try:
            await coordinator.submit_text(command)
            await asyncio.wait_for(approved.wait(), 1)
            assert coordinator.phase is ConversationPhase.AWAITING_APPROVAL
            await coordinator.reject_pending()
            assert store.list_all() == original
            assert results[-1].status == "denied"
            assert coordinator.phase is ConversationPhase.IDLE
            assert ConversationPhase.RECOVERING not in phases
            assert errors == []
            approved.clear()
            await coordinator.submit_text(command)
            await asyncio.wait_for(approved.wait(), 1)
            assert coordinator.phase is ConversationPhase.AWAITING_APPROVAL
        finally:
            await coordinator.stop()
            store.close()

    asyncio.run(scenario())


def test_late_cancel_result_does_not_end_a_new_turn(tmp_path):
    async def scenario():
        store = MemoryStore(tmp_path / "memory.db")
        bus = EventBus()
        ready = asyncio.Event()
        bus.subscribe(ApprovalRequested, lambda event: ready.set())
        coordinator = Coordinator(object(), object(), bus, memory_operations=MemoryOperationService(store))
        new_turns = []

        async def start_another_turn(event):
            if event.status == "denied":
                await coordinator.cancel_active_turn()
                new_turns.append(await coordinator.submit_text("记住：我喜欢清爽的配色"))

        bus.subscribe(MemoryResultReady, start_another_turn)
        try:
            await coordinator.submit_text("记住：我喜欢喝茶")
            await asyncio.wait_for(ready.wait(), 1)
            ready.clear()
            await coordinator.reject_pending()
            await asyncio.wait_for(ready.wait(), 1)
            assert coordinator.phase is ConversationPhase.AWAITING_APPROVAL
            assert coordinator.is_current(new_turns[0])
            assert store.list_all() == ()
        finally:
            await coordinator.stop()
            store.close()

    asyncio.run(scenario())
