"""记忆、近期摘要和可撤销变更的 QML ViewModel"""

from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QUrl,
    Signal,
    Slot,
)

from .dialogs import ConfirmationRequest, DialogCoordinator

_INVALID_INDEX = QModelIndex()
_FILTERS = frozenset({"all", "confirmed", "candidate", "conflicted"})


class MemoryRuntimeProtocol(Protocol):
    def list_memories(self) -> Future[tuple[Any, ...]]: ...
    def confirm_memory(self, memory_id: str) -> Future[Any]: ...
    def resolve_memory_conflict(self, memory_id: str) -> Future[Any]: ...
    def edit_memory(
        self, memory_id: str, content: str, expected_version: int
    ) -> Future[Any]: ...
    def delete_memory(self, memory_id: str) -> Future[bool]: ...
    def list_memory_changes(
        self, session_id: str | None = None
    ) -> Future[tuple[Any, ...]]: ...
    def undo_memory(self, change_id: str) -> Future[bool]: ...
    def list_summaries(
        self, session_id: str | None = None
    ) -> Future[tuple[Any, ...]]: ...
    def delete_summary(self, summary_id: str) -> Future[bool]: ...
    def export_memories(self, destination: str) -> Future[int]: ...


class RoleListModel(QAbstractListModel):
    def __init__(self, roles: tuple[str, ...], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._roles = {
            Qt.UserRole + offset: role.encode() for offset, role in enumerate(roles, 1)
        }
        self._items: list[dict[str, object]] = []

    def roleNames(self) -> dict[int, bytes]:
        return self._roles

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        name = self._roles.get(role)
        return None if name is None else self._items[index.row()].get(name.decode())

    def replace_items(self, items: list[dict[str, object]]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()


class MemoryListModel(RoleListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            (
                "memoryId",
                "category",
                "content",
                "status",
                "createdAt",
                "source",
                "origin",
                "originLabel",
                "sourceTitle",
                "sourceState",
                "sourceTime",
                "version",
                "conflictId",
                "conflictContent",
            ),
            parent,
        )


class MemoryChangeListModel(RoleListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            (
                "changeId",
                "memoryId",
                "sessionId",
                "sourceTurnId",
                "kind",
                "afterVersion",
                "createdAt",
                "canUndo",
                "undoReason",
                "content",
            ),
            parent,
        )


class SummaryListModel(RoleListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            (
                "summaryId",
                "sourceTitle",
                "topic",
                "decisions",
                "unfinishedItems",
                "createdAt",
                "expiresAt",
                "sessionId",
            ),
            parent,
        )


class MemoryViewModel(QObject):
    filterStatusChanged = Signal()
    selectedMemoryChanged = Signal()
    loadingChanged = Signal()
    actionStateChanged = Signal()
    errorChanged = Signal()
    errorOccurred = Signal(str)
    maintenanceStatusChanged = Signal()
    _futureFinished = Signal(str, int, object, object)

    def __init__(
        self,
        runtime: MemoryRuntimeProtocol,
        dialogs: DialogCoordinator,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime, self._dialogs = runtime, dialogs
        self._model, self._change_model, self._summary_model = (
            MemoryListModel(self),
            MemoryChangeListModel(self),
            SummaryListModel(self),
        )
        self._records: list[dict[str, object]] = []
        self._raw_changes: tuple[object, ...] = ()
        self._filter_status, self._selected_id, self._error, self._action_result = (
            "all",
            "",
            "",
            "",
        )
        self._loading = self._busy = False
        self._maintenance_status = ""
        self._edit_id, self._edit_version = "", 0
        self._generations: dict[str, int] = {}
        self._pending_confirmations: dict[str, tuple[str, str]] = {}
        self._futureFinished.connect(self._handle_future)
        dialogs.confirmationResolved.connect(self._confirmation_resolved)

    @Property(QObject, constant=True)
    def memory_model(self):
        return self._model

    @Property(QObject, constant=True)
    def memoryModel(self):
        return self._model

    @Property(QObject, constant=True)
    def changeModel(self):
        return self._change_model

    @Property(QObject, constant=True)
    def summaryModel(self):
        return self._summary_model

    @Property(str, notify=filterStatusChanged)
    def filter_status(self):
        return self._filter_status

    @Property(str, notify=filterStatusChanged)
    def filterStatus(self):
        return self._filter_status

    @Property("QVariantMap", notify=selectedMemoryChanged)
    def selected_memory(self):
        return next(
            (
                dict(item)
                for item in self._records
                if item["memoryId"] == self._selected_id
            ),
            {},
        )

    @Property("QVariantMap", notify=selectedMemoryChanged)
    def selectedMemory(self):
        return self.selected_memory

    @Property(bool, notify=selectedMemoryChanged)
    def can_confirm(self):
        return self.selected_memory.get("status") == "candidate"

    @Property(bool, notify=selectedMemoryChanged)
    def canConfirm(self):
        return self.can_confirm

    @Property(bool, notify=selectedMemoryChanged)
    def can_resolve(self):
        return self.selected_memory.get("status") == "conflicted"

    @Property(bool, notify=selectedMemoryChanged)
    def canResolve(self):
        return self.can_resolve

    @Property(bool, notify=loadingChanged)
    def loading(self):
        return self._loading

    @Property(bool, notify=actionStateChanged)
    def actionBusy(self):
        return self._busy

    @Property(str, notify=actionStateChanged)
    def actionResult(self):
        return self._action_result

    @Property(str, notify=errorChanged)
    def error(self):
        return self._error

    @Property(str, notify=maintenanceStatusChanged)
    def maintenanceStatus(self):
        return self._maintenance_status

    @Slot(str, str)
    def set_maintenance_status(self, status, message):
        value = "" if status == "success" else str(message)
        if value != self._maintenance_status:
            self._maintenance_status = value
            self.maintenanceStatusChanged.emit()

    @Slot()
    def refresh(self):
        self._set_loading(True)
        self._start("memories", self._runtime.list_memories)

    @Slot()
    def refresh_changes(self):
        self._start("changes", lambda: self._runtime.list_memory_changes(None))

    @Slot()
    def refreshChanges(self):
        self.refresh_changes()

    @Slot()
    def refresh_summaries(self):
        self._start("summaries", lambda: self._runtime.list_summaries(None))

    @Slot()
    def refreshSummaries(self):
        self.refresh_summaries()

    @Slot(str)
    def set_filter_status(self, status):
        if status in _FILTERS and status != self._filter_status:
            self._filter_status = status
            self.filterStatusChanged.emit()
            self._apply_filter()

    @Slot(str)
    def select(self, memory_id):
        if memory_id != self._selected_id and any(
            item["memoryId"] == memory_id for item in self._records
        ):
            self._selected_id = memory_id
            self.selectedMemoryChanged.emit()

    @Slot()
    def confirm_selected(self):
        if self.can_confirm:
            self._run_action(
                "confirm", lambda: self._runtime.confirm_memory(self._selected_id)
            )

    @Slot()
    def resolve_selected(self):
        if not self.can_resolve:
            return
        selected = self.selected_memory
        request_id = f"resolve-memory:{uuid4()}"
        self._pending_confirmations[request_id] = ("resolve", self._selected_id)
        old = selected.get("conflictContent") or "关联旧记录"
        self._dialogs.confirm(
            ConfirmationRequest(
                request_id,
                "采用新内容？",
                f"将“{old}”替换为“{selected.get('content', '')}”",
                "只更新这条关联的旧记录",
                False,
                "记忆",
            )
        )

    @Slot()
    def begin_edit_selected(self):
        selected = self.selected_memory
        if selected:
            self._edit_id, self._edit_version = (
                str(selected["memoryId"]),
                int(selected["version"]),
            )

    @Slot(str)
    def save_edit(self, content):
        content = content.strip()
        if self._edit_id and content:
            memory_id, version = self._edit_id, self._edit_version
            self._run_action(
                "edit", lambda: self._runtime.edit_memory(memory_id, content, version)
            )

    @Slot(str)
    def undo_change(self, change_id):
        if change_id:
            self._run_action("undo", lambda: self._runtime.undo_memory(change_id))

    @Slot(str)
    def delete_summary(self, summary_id):
        if summary_id:
            self._run_action(
                "delete-summary", lambda: self._runtime.delete_summary(summary_id)
            )

    @Slot()
    def delete_selected(self):
        selected = self.selected_memory
        if not selected:
            return
        request_id = f"delete-memory:{uuid4()}"
        self._pending_confirmations[request_id] = ("delete", self._selected_id)
        self._dialogs.confirm(
            ConfirmationRequest(
                request_id,
                "删除这条记忆？",
                str(selected["content"])[:160],
                "该记忆会从本机长期记忆中移除",
                False,
                "隐私",
            )
        )

    @Slot(str)
    def export_all(self, destination):
        if destination:
            self._start("export", lambda: self._runtime.export_memories(destination))

    @Slot(QUrl)
    def export_url(self, destination):
        if destination.isLocalFile():
            self.export_all(destination.toLocalFile())

    @Slot(str, bool)
    def _confirmation_resolved(self, request_id, approved):
        pending = self._pending_confirmations.pop(request_id, None)
        if not approved or pending is None:
            return
        operation, memory_id = pending
        action = (
            self._runtime.delete_memory
            if operation == "delete"
            else self._runtime.resolve_memory_conflict
        )
        self._run_action(operation, lambda: action(memory_id))

    def _run_action(self, operation, starter):
        if self._busy:
            return
        self._busy = True
        self._action_result = ""
        self.actionStateChanged.emit()
        self._start(operation, starter)

    def _start(self, operation, starter):
        generation = self._generations.get(operation, 0) + 1
        self._generations[operation] = generation
        try:
            future = starter()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self._futureFinished.emit(operation, generation, None, RuntimeError())
            return

        def done(completed):
            try:
                self._futureFinished.emit(
                    operation, generation, completed.result(), None
                )
            except Exception as error:  # noqa: BLE001 后台异常只发布安全的局部状态
                self._futureFinished.emit(operation, generation, None, error)

        future.add_done_callback(done)

    @Slot(str, int, object, object)
    def _handle_future(self, operation, generation, result, error):
        if generation != self._generations.get(operation):
            return
        if operation == "memories":
            self._set_loading(False)
        if operation not in {"memories", "changes", "summaries", "export"}:
            self._busy = False
            self.actionStateChanged.emit()
        if error is not None:
            message = (
                "内容已发生变化，请刷新后重新编辑"
                if operation == "edit"
                else "记忆操作失败，请重试"
            )
            self._set_error(message)
            self._action_result = message
            self.actionStateChanged.emit()
            return
        self._set_error("")
        if operation == "memories":
            selected_before = self.selected_memory
            self._records = [_memory_item(record) for record in tuple(result or ())]
            by_id = {str(item["memoryId"]): item for item in self._records}
            for item in self._records:
                item["conflictContent"] = str(
                    by_id.get(str(item["conflictId"]), {}).get("content", "")
                )
            self._apply_filter(notify_selection=False)
            if self.selected_memory != selected_before:
                self.selectedMemoryChanged.emit()
            self._recompute_changes()
        elif operation == "changes":
            self._raw_changes = tuple(result or ())
            self._recompute_changes()
        elif operation == "summaries":
            self._summary_model.replace_items(
                [_summary_item(item) for item in tuple(result or ())]
            )
        elif operation == "export":
            self._dialogs.toast("记忆已导出", "success")
        else:
            self._action_result = (
                "操作已完成" if result is not False else "这项操作没有产生变更"
            )
            self.actionStateChanged.emit()
            if operation == "delete-summary":
                self.refresh_summaries()
            else:
                self.refresh()
                self.refresh_changes()

    def _recompute_changes(self):
        records = {str(item["memoryId"]): item for item in self._records}
        changes = sorted(
            self._raw_changes,
            key=lambda change: _iso(_read(change, "created_at", "")),
            reverse=True,
        )
        self._change_model.replace_items(
            [_change_item(change, records) for change in changes]
        )

    def _apply_filter(self, notify_selection=True):
        visible = (
            list(self._records)
            if self._filter_status == "all"
            else [
                item for item in self._records if item["status"] == self._filter_status
            ]
        )
        self._model.replace_items(visible)
        if self._selected_id and not any(
            item["memoryId"] == self._selected_id for item in visible
        ):
            self._selected_id = ""
            if notify_selection:
                self.selectedMemoryChanged.emit()

    def _set_loading(self, value):
        if value != self._loading:
            self._loading = value
            self.loadingChanged.emit()

    def _set_error(self, value):
        if value != self._error:
            self._error = value
            self.errorChanged.emit()


def _memory_item(record):
    origin = str(_read(record, "origin", "explicit"))
    return {
        "memoryId": str(_read(record, "id", "")),
        "category": str(_read(record, "category", "未分类")),
        "content": str(_read(record, "content", ""))[:4000],
        "status": _enum_value(_read(record, "status", "candidate")),
        "createdAt": _iso(_read(record, "created_at", "")),
        "source": str(_read(record, "source_turn_id", "")),
        "origin": origin,
        "originLabel": {
            "automatic": "自动记住",
            "explicit": "你要求保存",
            "manual": "手动确认或编辑",
        }.get(origin, "手动确认或编辑"),
        "sourceTitle": str(_read(record, "source_title", "")),
        "sourceState": str(_read(record, "source_state", "unavailable")),
        "sourceTime": _iso(_read(record, "source_time", "")),
        "version": int(_read(record, "version", 1)),
        "conflictId": str(_read(record, "conflict_id", "") or ""),
        "conflictContent": "",
    }


def _change_item(change, records):
    memory_id = str(_read(change, "memory_id", ""))
    record = records.get(memory_id)
    undone = bool(_read(change, "undone", False))
    after_version = int(_read(change, "after_version", 0))
    reason = (
        "已撤销"
        if undone
        else "已删除，无法撤销"
        if record is None or record.get("status") == "deleted"
        else "已有后续版本，无法撤销"
        if int(record.get("version", 0)) != after_version
        else ""
    )
    return {
        "changeId": str(_read(change, "id", "")),
        "memoryId": memory_id,
        "sessionId": str(_read(change, "session_id", "")),
        "sourceTurnId": str(_read(change, "source_turn_id", "")),
        "kind": str(_read(change, "kind", "")),
        "afterVersion": after_version,
        "createdAt": _iso(_read(change, "created_at", "")),
        "canUndo": not reason,
        "undoReason": reason,
        "content": str(record.get("content", "")) if record else "",
    }


def _summary_item(item):
    return {
        "summaryId": str(_read(item, "id", "")),
        "sourceTitle": str(_read(item, "source_title", "")) or "来源会话不可用",
        "topic": str(_read(item, "topic", "")),
        "decisions": list(_read(item, "decisions", ())),
        "unfinishedItems": list(_read(item, "unfinished_items", ())),
        "createdAt": _iso(_read(item, "created_at", "")),
        "expiresAt": _iso(_read(item, "expires_at", "")),
        "sessionId": str(_read(item, "session_id", "")),
    }


def _read(source, name, default):
    return (
        source.get(name, default)
        if isinstance(source, dict)
        else getattr(source, name, default)
    )


def _enum_value(value):
    return str(value.value) if isinstance(value, Enum) else str(value)


def _iso(value):
    return (
        value.astimezone().strftime("%Y-%m-%d %H:%M")
        if isinstance(value, datetime)
        else str(value or "")
    )
