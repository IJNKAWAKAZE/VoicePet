from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from ui.viewmodels.dialogs import DialogCoordinator
from ui.viewmodels.memories import MemoryViewModel


def completed(value=None):
    future = Future()
    future.set_result(value)
    return future


@dataclass
class Memory:
    id: str
    category: str
    content: str
    status: str
    created_at: datetime
    source_turn_id: str


class FakeRuntime:
    def __init__(self):
        now = datetime.now(UTC)
        self.records = (
            Memory("confirmed", "偏好", "喜欢晴天", "confirmed", now, "turn-1"),
            Memory("candidate", "称呼", "希望叫小蓝", "candidate", now, "turn-2"),
            Memory("conflicted", "地点", "可能住在上海", "conflicted", now, "turn-3"),
        )
        self.confirmed = []
        self.resolved = []
        self.deleted = []
        self.exported = []

    def list_memories(self):
        return completed(self.records)

    def confirm_memory(self, memory_id):
        self.confirmed.append(memory_id)
        return completed()

    def resolve_memory_conflict(self, memory_id):
        self.resolved.append(memory_id)
        return completed()

    def delete_memory(self, memory_id):
        self.deleted.append(memory_id)
        return completed(True)

    def export_memories(self, destination):
        self.exported.append(destination)
        return completed(len(self.records))


def test_refresh_filter_and_selection_publish_safe_memory_rows():
    runtime = FakeRuntime()
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())

    viewmodel.refresh()
    viewmodel.set_filter_status("candidate")
    viewmodel.select("candidate")

    assert viewmodel.memory_model.rowCount() == 1
    index = viewmodel.memory_model.index(0)
    assert viewmodel.memory_model.data(index, Qt.UserRole + 1) == "candidate"
    assert viewmodel.selected_memory["content"] == "希望叫小蓝"
    assert viewmodel.can_confirm is True
    assert viewmodel.can_resolve is False


def test_unknown_filter_keeps_current_filter():
    viewmodel = MemoryViewModel(FakeRuntime(), DialogCoordinator())

    viewmodel.set_filter_status("unknown")

    assert viewmodel.filter_status == "all"


def test_actions_only_send_selected_memory_id():
    runtime = FakeRuntime()
    dialogs = DialogCoordinator()
    viewmodel = MemoryViewModel(runtime, dialogs)
    viewmodel.refresh()

    viewmodel.confirm_selected()
    viewmodel.select("candidate")
    viewmodel.confirm_selected()
    viewmodel.select("conflicted")
    viewmodel.resolve_selected()
    request_id = dialogs.current_confirmation["requestId"]
    assert "替换" in dialogs.current_confirmation["summary"]
    dialogs.resolve_confirmation(request_id, True)
    viewmodel.delete_selected()
    request_id = dialogs.current_confirmation["requestId"]
    dialogs.resolve_confirmation(request_id, True)

    assert runtime.confirmed == ["candidate"]
    assert runtime.resolved == ["conflicted"]
    assert runtime.deleted == ["conflicted"]


def test_export_delegates_explicit_destination():
    runtime = FakeRuntime()
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())

    viewmodel.export_all("D:/exports/memory.json")

    assert runtime.exported == ["D:/exports/memory.json"]


def test_refresh_notifies_selected_status_change(qapp):
    runtime = FakeRuntime()
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())
    viewmodel.refresh()
    viewmodel.select("candidate")
    changed = QSignalSpy(viewmodel.selectedMemoryChanged)

    runtime.records[1].status = "confirmed"
    viewmodel.refresh()

    assert changed.count() == 1
    assert viewmodel.canConfirm is False
    assert viewmodel.selectedMemory["status"] == "confirmed"


def test_refresh_clears_deleted_selection_and_notifies(qapp):
    runtime = FakeRuntime()
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())
    viewmodel.refresh()
    viewmodel.select("candidate")
    changed = QSignalSpy(viewmodel.selectedMemoryChanged)

    runtime.records = (runtime.records[0], runtime.records[2])
    viewmodel.refresh()

    assert changed.count() == 1
    assert viewmodel.selectedMemory == {}
    assert viewmodel.canConfirm is False


def test_filter_clears_selection_when_record_is_hidden(qapp):
    viewmodel = MemoryViewModel(FakeRuntime(), DialogCoordinator())
    viewmodel.refresh()
    viewmodel.select("candidate")

    viewmodel.set_filter_status("confirmed")

    assert viewmodel.selectedMemory == {}
