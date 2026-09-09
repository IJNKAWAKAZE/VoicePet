from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from test_qml_application import build_controller

from core.events import MemoryChanged, MemoryMaintenanceChanged
from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.dialogs import DialogCoordinator
from ui.viewmodels.memories import MemoryViewModel


def completed(value=None):
    future = Future()
    future.set_result(value)
    return future


@dataclass(frozen=True)
class Record:
    id: str
    content: str
    status: str = "confirmed"
    category: str = "偏好"
    source_turn_id: str = "turn-1"
    created_at: datetime = datetime(2026, 9, 6, tzinfo=UTC)
    updated_at: datetime = datetime(2026, 9, 6, tzinfo=UTC)
    origin: str = "automatic"
    version: int = 2
    conflict_id: str | None = None
    session_id: str = "session-1"
    source_title: str = "周末计划"
    source_state: str = "available"
    source_time: datetime | None = datetime(2026, 9, 6, tzinfo=UTC)


@dataclass(frozen=True)
class Change:
    id: str
    memory_id: str
    session_id: str
    source_turn_id: str
    kind: str
    after_version: int
    created_at: datetime
    undone: bool = False


@dataclass(frozen=True)
class Summary:
    id: str
    source_turn_id: str
    topic: str
    unfinished_items: tuple[str, ...]
    session_date: object
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    session_id: str = "session-1"
    source_turn_ids: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    source_title: str = "周末计划"


class MemoryRuntime:
    def __init__(self):
        self.list_futures = []
        self.edits = []
        self.undos = []
        self.summary_deleted = []
        self.records = (Record("memory-1", "喜欢晴天"),)
        self.changes = (
            Change(
                "change-1",
                "memory-1",
                "session-1",
                "turn-1",
                "create",
                2,
                datetime.now(UTC),
            ),
        )

    def list_memories(self):
        if self.list_futures:
            return self.list_futures.pop(0)
        return completed(self.records)

    def list_memory_changes(self, session_id=None):
        return completed(
            tuple(
                change
                for change in self.changes
                if session_id is None or change.session_id == session_id
            )
        )

    def list_summaries(self, session_id=None):
        now = datetime.now(UTC)
        return completed(
            (
                Summary(
                    "summary-1",
                    "turn-1",
                    "出游",
                    ("订票",),
                    now.date(),
                    now,
                    now,
                    now + timedelta(days=7),
                    decisions=("周六出发",),
                ),
            )
        )

    def edit_memory(self, memory_id, content, expected_version):
        self.edits.append((memory_id, content, expected_version))
        return completed(self.records[0])

    def undo_memory(self, change_id):
        self.undos.append(change_id)
        return completed(True)

    def delete_summary(self, summary_id):
        self.summary_deleted.append(summary_id)
        return completed(True)


def test_refresh_generation_prevents_older_result_overwriting_newer(qapp):
    runtime = MemoryRuntime()
    older, newer = Future(), Future()
    runtime.list_futures = [older, newer]
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())

    viewmodel.refresh()
    viewmodel.refresh()
    newer.set_result((Record("new", "新结果"),))
    qapp.processEvents()
    older.set_result((Record("old", "旧结果"),))
    qapp.processEvents()

    assert (
        viewmodel.memoryModel.data(viewmodel.memoryModel.index(0), Qt.UserRole + 1)
        == "new"
    )


def test_edit_uses_version_captured_when_editor_opened(qapp):
    runtime = MemoryRuntime()
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())
    viewmodel.refresh()
    viewmodel.select("memory-1")

    viewmodel.begin_edit_selected()
    runtime.records = (Record("memory-1", "后台更新", version=3),)
    viewmodel.refresh()
    viewmodel.save_edit("用户编辑")

    assert runtime.edits == [("memory-1", "用户编辑", 2)]


def test_changes_join_live_memory_for_real_undo_reasons(qapp):
    runtime = MemoryRuntime()
    runtime.changes = (
        Change("ok", "memory-1", "session-1", "turn-1", "create", 2, datetime.now(UTC)),
        Change(
            "newer", "memory-1", "session-1", "turn-1", "update", 1, datetime.now(UTC)
        ),
        Change(
            "missing", "gone", "session-1", "turn-1", "create", 1, datetime.now(UTC)
        ),
    )
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())

    viewmodel.refresh()
    viewmodel.refresh_changes()

    model = viewmodel.changeModel
    assert model.data(model.index(0), Qt.UserRole + 8) is True
    assert "后续版本" in model.data(model.index(1), Qt.UserRole + 9)
    assert "已删除" in model.data(model.index(2), Qt.UserRole + 9)


def test_changes_waiting_for_memories_are_rejoined_after_records_arrive(qapp):
    runtime = MemoryRuntime()
    pending_records = Future()
    runtime.list_futures = [pending_records]
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())

    viewmodel.refresh()
    viewmodel.refresh_changes()
    pending_records.set_result(runtime.records)
    qapp.processEvents()

    assert (
        viewmodel.changeModel.data(viewmodel.changeModel.index(0), Qt.UserRole + 8)
        is True
    )


@pytest.mark.parametrize("operation", ["undo", "delete-summary"])
def test_false_mutation_result_is_reported_as_no_op(qapp, operation):
    runtime = MemoryRuntime()
    runtime.undo_memory = lambda change_id: completed(False)
    runtime.delete_summary = lambda summary_id: completed(False)
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())

    if operation == "undo":
        viewmodel.undo_change("change-1")
    else:
        viewmodel.delete_summary("summary-1")

    assert "无法" in viewmodel.actionResult or "没有" in viewmodel.actionResult


def test_summary_rows_expose_native_qml_values_and_delete_refreshes(qapp):
    runtime = MemoryRuntime()
    viewmodel = MemoryViewModel(runtime, DialogCoordinator())

    viewmodel.refresh_summaries()
    index = viewmodel.summaryModel.index(0)
    assert viewmodel.summaryModel.data(index, Qt.UserRole + 4) == ["周六出发"]
    assert viewmodel.summaryModel.data(index, Qt.UserRole + 5) == ["订票"]
    viewmodel.delete_summary("summary-1")
    assert runtime.summary_deleted == ["summary-1"]


class ChatRuntime:
    def __init__(self):
        self.change_futures = {}
        self.undos = []

    def list_memory_changes(self, session_id=None):
        future = Future()
        self.change_futures[session_id] = future
        return future

    def undo_memory(self, change_id):
        self.undos.append(change_id)
        return completed(True)


def test_chat_open_memory_acknowledges_prompt_without_deleting_record(qapp):
    runtime = ChatRuntime()
    chat = ChatViewModel(runtime)
    chat.set_active_session_for_memory("session-1")
    runtime.change_futures["session-1"].set_result(
        (Change("change-1", "m1", "session-1", "t1", "create", 1, datetime.now(UTC)),)
    )
    qapp.processEvents()

    chat.open_memory()

    assert chat.memoryChangeCount == 0
    assert chat.memoryChangesModel.rowCount() == 0


def test_chat_discards_late_other_session_changes_without_touching_stream(qapp):
    runtime = ChatRuntime()
    chat = ChatViewModel(runtime)
    chat.set_active_session_for_memory("session-1")
    chat.append_assistant_delta("答")
    chat.set_active_session_for_memory("session-2")
    runtime.change_futures["session-1"].set_result(
        (Change("old", "m1", "session-1", "t1", "create", 1, datetime.now(UTC)),)
    )
    qapp.processEvents()
    runtime.change_futures["session-2"].set_result(
        (Change("current", "m2", "session-2", "t2", "create", 1, datetime.now(UTC)),)
    )
    qapp.processEvents()

    assert chat.memoryChangesModel.rowCount() == 1
    assert (
        chat.memoryChangesModel.data(chat.memoryChangesModel.index(0), Qt.UserRole + 1)
        == "current"
    )
    assert chat.messageModel.rowCount() == 1
    assert chat.messageModel.data(chat.messageModel.index(0), Qt.UserRole + 3) == "答"


def test_chat_undo_reports_backend_idempotent_result_locally(qapp):
    runtime = ChatRuntime()
    chat = ChatViewModel(runtime)
    chat.set_active_session_for_memory("session-1")
    runtime.change_futures["session-1"].set_result(())
    qapp.processEvents()
    runtime.undo_memory = lambda change_id: completed(False)

    chat.undo_memory_change("real-change")

    assert chat.memoryActionResult == "这项变更已无法撤销"


def test_chat_same_session_refresh_uses_latest_request_epoch(qapp):
    runtime = ChatRuntime()
    pending = []
    runtime.list_memory_changes = lambda session_id=None: (
        pending.append(Future()) or pending[-1]
    )
    chat = ChatViewModel(runtime)
    chat.set_active_session_for_memory("session-1")
    chat.refresh_memory_changes()

    pending[1].set_result(
        (Change("new", "m2", "session-1", "t2", "create", 1, datetime.now(UTC)),)
    )
    qapp.processEvents()
    pending[0].set_result(
        (Change("old", "m1", "session-1", "t1", "create", 1, datetime.now(UTC)),)
    )
    qapp.processEvents()

    assert (
        chat.memoryChangesModel.data(chat.memoryChangesModel.index(0), Qt.UserRole + 1)
        == "new"
    )


def test_chat_memory_failures_and_cross_session_undo_are_local(qapp):
    runtime = ChatRuntime()
    chat = ChatViewModel(runtime)
    chat.set_active_session_for_memory("session-1")
    runtime.change_futures["session-1"].set_exception(RuntimeError("query"))
    qapp.processEvents()
    assert "失败" in chat.memoryActionResult

    undo = Future()
    runtime.undo_memory = lambda change_id: undo
    chat.undo_memory_change("change-1")
    assert chat.memoryActionBusy is True
    chat.set_active_session_for_memory("session-2")
    undo.set_result(True)
    qapp.processEvents()
    assert chat.memoryActionResult == ""


def test_runtime_memory_events_refresh_models_without_global_toast(qapp):
    controller, _runtime, _tray, _qml, _shell, chat, dialogs, *_ = build_controller(
        qapp
    )
    chat.set_active_session_for_memory("session-1")
    refreshes = []
    controller._memories.refresh = lambda: refreshes.append("memories")
    controller._memories.refresh_changes = lambda: refreshes.append("changes")
    controller._memories.refresh_summaries = lambda: refreshes.append("summaries")
    chat_refreshes = []
    chat.refresh_memory_changes = lambda: chat_refreshes.append("chat")

    controller.handle_runtime_event(MemoryChanged("other-session", ()))
    controller.handle_runtime_event(MemoryChanged("session-1", ("real-change",)))
    controller.handle_runtime_event(MemoryMaintenanceChanged("failed", "后台整理失败"))

    assert refreshes == ["memories", "changes", "memories", "changes", "summaries"]
    assert chat_refreshes == ["chat"]
    assert controller._memories.maintenanceStatus == "后台整理失败"
    assert dialogs.toastModel.rowCount() == 0


@pytest.mark.parametrize("theme_id", ["sunny_sea", "deep_night", "sakura_coral"])
def test_memory_and_chat_pages_have_no_native_qml_errors_in_all_themes(qapp, theme_id):
    from dataclasses import replace

    from core.config import UiConfig

    controller, _runtime, _tray, qml, shell, _chat, _dialogs, *_ = build_controller(
        qapp
    )
    warnings = []
    controller.start()
    qml._engine.warnings.connect(lambda items: warnings.extend(items))
    theme = qml._engine.rootContext().contextProperty("themeViewModel")
    theme._config = replace(UiConfig(), theme_id=theme_id)
    theme.themeChanged.emit()
    shell.navigate("memories")
    QTest.qWait(30)
    shell.navigate("chat")
    QTest.qWait(30)

    assert not warnings, [item.toString() for item in warnings]
    controller.close()
