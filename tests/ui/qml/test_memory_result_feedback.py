import asyncio

import pytest
from PySide6.QtCore import QObject
from test_qml_application import build_controller

from core.coordinator import Coordinator
from core.event_bus import EventBus
from core.events import (
    ApprovalRequested,
    ConversationPhase,
    CorrelationId,
    MemoryResultReady,
    StateChanged,
    TurnId,
)
from core.memory import MemoryStore
from core.memory_intents import MemoryOperationService


@pytest.mark.parametrize("status,message", [("denied", "用户取消了记忆操作"), ("success", "记忆已保存")])
def test_memory_confirmation_result_replaces_prompt_and_finishes_chat(qapp, status, message):
    controller, _, tray, qml, _, chat, dialogs, *_ = build_controller(qapp)
    controller.start()
    try:
        turn, correlation = TurnId.new(), CorrelationId.new()
        controller.handle_runtime_event(StateChanged(turn, correlation, ConversationPhase.IDLE, ConversationPhase.THINKING))
        chat.append_user_message("记住：我喜欢喝茶")
        controller.handle_runtime_event(StateChanged(turn, correlation, ConversationPhase.THINKING, ConversationPhase.AWAITING_APPROVAL))
        controller.handle_runtime_event(ApprovalRequested(turn, correlation, "memory:remember", "保存记忆：我喜欢喝茶", "隐私"))
        dialogs.resolve_confirmation("memory:remember", status == "success")
        controller.handle_runtime_event(MemoryResultReady(turn, correlation, status, message, 0))
        controller.handle_runtime_event(StateChanged(turn, correlation, ConversationPhase.AWAITING_APPROVAL, ConversationPhase.IDLE))
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        assert pet.property("speech") == message
        assert chat.message_model.last["markdown"] == message
        assert not chat.processing and not tray.listening
        assert controller._phase is ConversationPhase.IDLE
    finally:
        controller.close()


def test_background_memory_tool_result_does_not_pollute_assistant_reply(qapp):
    controller, _, _, qml, _, chat, *_ = build_controller(qapp)
    controller.start()
    try:
        turn, correlation = TurnId.new(), CorrelationId.new()
        controller.handle_runtime_event(StateChanged(turn, correlation, ConversationPhase.IDLE, ConversationPhase.THINKING))
        controller.handle_runtime_event(MemoryResultReady(turn, correlation, "candidate", "已创建记忆候选", 1))
        assert chat.message_model.rowCount() == 0
        assert qml.root_objects[0].findChild(QObject, "petWindow").property("speech") == ""
    finally:
        controller.close()


def test_old_memory_result_cannot_replace_new_confirmation(qapp):
    controller, _, _, qml, _, chat, *_ = build_controller(qapp)
    controller.start()
    try:
        current, correlation = TurnId.new(), CorrelationId.new()
        controller.handle_runtime_event(StateChanged(current, correlation, ConversationPhase.IDLE, ConversationPhase.AWAITING_APPROVAL))
        controller.handle_runtime_event(ApprovalRequested(current, correlation, "memory:remember", "保存新记忆", "隐私"))
        controller.handle_runtime_event(MemoryResultReady(TurnId.new(), correlation, "denied", "旧操作取消", 0))
        assert chat.message_model.rowCount() == 0
        assert "保存新记忆" in qml.root_objects[0].findChild(QObject, "petWindow").property("speech")
    finally:
        controller.close()


@pytest.mark.parametrize("main_visible", [False, True])
def test_cancel_dialog_drives_real_memory_runtime_back_to_idle(qapp, tmp_path, main_visible):
    controller, runtime, tray, qml, shell, chat, dialogs, *_ = build_controller(qapp)
    controller.start()
    store = MemoryStore(tmp_path / "memory.db")
    bus = EventBus()
    for event_type in (StateChanged, ApprovalRequested, MemoryResultReady):
        bus.subscribe(event_type, controller.handle_runtime_event)
    coordinator = Coordinator(object(), object(), bus, memory_operations=MemoryOperationService(store))

    async def scenario():
        approved, finished = asyncio.Event(), asyncio.Event()
        bus.subscribe(ApprovalRequested, lambda event: approved.set())
        bus.subscribe(StateChanged, lambda event: finished.set() if event.current is ConversationPhase.IDLE else None)
        tasks = []
        runtime.reject = lambda: tasks.append(asyncio.create_task(coordinator.reject_pending()))
        try:
            if main_visible:
                shell.show_main("chat")
            await coordinator.submit_text("记住：我喜欢喝茶")
            await asyncio.wait_for(approved.wait(), 1)
            qapp.processEvents()
            window_name = "mainConfirmationSheet" if main_visible else "toolConfirmationWindow"
            assert qml.root_objects[0].findChild(QObject, window_name).property("visible")
            dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], False)
            await asyncio.wait_for(finished.wait(), 1)
            await asyncio.gather(*tasks)
            qapp.processEvents()
            assert coordinator.phase is ConversationPhase.IDLE
            assert controller._phase is ConversationPhase.IDLE
            assert not chat.processing and not tray.listening
            assert store.list_all() == ()
            assert "取消" in qml.root_objects[0].findChild(QObject, "petWindow").property("speech")
            assert not dialogs.currentConfirmation
            assert not qml.root_objects[0].findChild(QObject, window_name).property("visible")
        finally:
            await coordinator.stop()

    try:
        asyncio.run(scenario())
    finally:
        store.close()
        controller.close()
