import time
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from core.event_bus import EventBus
from core.events import MemoryChanged
from core.memory import MemoryStore
from core.memory_data import MemoryDataManager
from core.memory_facts import FactEvidence, FactProposal
from core.runtime import RuntimeHost, RuntimeServices
from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext
from core.session_data import SessionDataManager
from ui.event_bridge import QtEventBridge
from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.dialogs import DialogCoordinator
from ui.viewmodels.memories import MemoryViewModel


class InertLifecycle:
    async def start(self):
        pass

    async def stop(self):
        pass


def wait_for(qapp, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert predicate()


def test_real_runtime_futures_restore_and_undo_persisted_memory_in_qt(qapp, tmp_path):
    path = tmp_path / "assistant.db"
    archive = SessionArchiveStore(path)
    store = MemoryStore(path)
    context = SessionContext()
    source = archive.archive_turn(str(uuid4()), "我喜欢猫", "知道了", context.session_id)
    change = store.save_fact(FactProposal("pet", "用户喜欢猫", "user.pet", "猫", "我喜欢猫"),
                             FactEvidence(source.turn_id, source.session_id, source.user_text))
    archive.save_summary(source.turn_id, "宠物喜好", (), decisions=("喜欢猫",))
    bus = EventBus()
    host = RuntimeHost(RuntimeServices(bus, InertLifecycle(), None, InertLifecycle(),
                                      memory=MemoryDataManager(store, archive=archive),
                                      sessions=SessionDataManager(archive, context),
                                      closers=(store.close, archive.close)))
    dialogs = DialogCoordinator()
    memories = MemoryViewModel(host, dialogs)
    chat = ChatViewModel(host)
    bridge = QtEventBridge(bus, (MemoryChanged,))
    events = []
    bridge.event_received.connect(events.append)
    host.start()
    try:
        memories.refresh()
        memories.refresh_changes()
        memories.refresh_summaries()
        chat.set_active_session_for_memory(source.session_id)
        wait_for(qapp, lambda: memories.memoryModel.rowCount() == 1 and memories.summaryModel.rowCount() == 1
                 and chat.memoryChangesModel.rowCount() == 1 and memories.changeModel.rowCount() == 1)
        wait_for(qapp, lambda: memories.changeModel.data(memories.changeModel.index(0), Qt.UserRole + 8) is True)
        assert memories.memoryModel.data(memories.memoryModel.index(0), Qt.UserRole + 3) == "用户喜欢猫"
        assert chat.messageModel.rowCount() == 0
        chat.undo_memory_change(change.id)
        wait_for(qapp, lambda: bool(events) and not chat.memoryActionBusy)
        assert events[-1].change_ids == (change.id,)
        assert "已撤销" in chat.memoryActionResult
        memories.refresh()
        memories.refresh_changes()
        wait_for(qapp, lambda: memories.memoryModel.rowCount() == 0 and "已撤销" in str(
            memories.changeModel.data(memories.changeModel.index(0), Qt.UserRole + 9)))
        assert dialogs.toastModel.rowCount() == 0
        assert chat.messageModel.rowCount() == 0
    finally:
        bridge.close()
        host.close()
    reopened = MemoryStore(path)
    try:
        assert reopened.list_changes(source.session_id)[0].undone
        assert reopened.list_all() == ()
    finally:
        reopened.close()
