"""全局 Toast、后台任务和确认请求协调器"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)

_INVALID_INDEX = QModelIndex()

# 阻塞确认按界面分流：主面板内嵌确认与独立确认窗口各自排队
MAIN_CHANNEL = "main"
WINDOW_CHANNEL = "window"
CONFIRMATION_CHANNELS = (MAIN_CHANNEL, WINDOW_CHANNEL)


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    request_id: str
    title: str
    summary: str
    impact: str
    reversible: bool
    risk: str
    timeout_ms: int = 30_000
    show_details: bool = True
    confirm_label: str = "确认"
    channel: str = MAIN_CHANNEL

    def __post_init__(self) -> None:
        texts = (self.request_id, self.title, self.summary, self.impact, self.risk, self.confirm_label)
        if any(not isinstance(value, str) or not value.strip() for value in texts):
            raise ValueError("确认请求文本不能为空")
        if (
            type(self.reversible) is not bool
            or type(self.show_details) is not bool
            or self.timeout_ms <= 0
            or self.channel not in CONFIRMATION_CHANNELS
        ):
            raise ValueError("确认请求参数无效")

    def to_map(self) -> dict[str, object]:
        return {
            "requestId": self.request_id,
            "title": self.title,
            "summary": self.summary,
            "impact": self.impact,
            "reversible": self.reversible,
            "risk": self.risk,
            "timeoutMs": self.timeout_ms,
            "showDetails": self.show_details,
            "confirmLabel": self.confirm_label,
            "channel": self.channel,
        }


@dataclass(frozen=True, slots=True)
class AgentInteractionRequest:
    """Codex Agent 的审批或用户输入请求"""

    request_id: str
    title: str
    message: str
    kind: str = "approval"
    options: tuple[dict[str, str], ...] = ()

    def to_map(self) -> dict[str, object]:
        return {
            "requestId": self.request_id,
            "title": self.title,
            "message": self.message,
            "kind": self.kind,
            "options": [dict(item) for item in self.options],
        }


class _DictionaryListModel(QAbstractListModel):
    def __init__(self, roles: tuple[str, ...], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._roles = {
            Qt.UserRole + offset: role.encode("utf-8")
            for offset, role in enumerate(roles, start=1)
        }
        self._items: list[dict[str, object]] = []

    def roleNames(self) -> dict[int, bytes]:
        return self._roles

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        role_name = self._roles.get(role)
        if role_name is None:
            return None
        return self._items[index.row()].get(role_name.decode("utf-8"))

    def append(self, item: dict[str, object]) -> None:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(item)
        self.endInsertRows()

    def remove(self, row: int) -> None:
        if not 0 <= row < len(self._items):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._items.pop(row)
        self.endRemoveRows()

    def upsert(self, key: str, value: str, item: dict[str, object]) -> None:
        for row, current in enumerate(self._items):
            if current.get(key) == value:
                self._items[row] = item
                index = self.index(row)
                self.dataChanged.emit(index, index, list(self._roles))
                return
        self.append(item)


class DialogCoordinator(QObject):
    """把短反馈、长任务与阻塞确认分成独立通道"""

    confirmationChanged = Signal()
    windowConfirmationChanged = Signal()
    confirmationResolved = Signal(str, bool)
    agentInteractionChanged = Signal()
    agentInteractionResolved = Signal(str, object)
    taskCancelRequested = Signal(str)
    pageErrorsChanged = Signal()

    _TOAST_KINDS = frozenset({"info", "success", "warning", "danger"})
    _TOAST_TIMEOUT_MS = 5000

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._toast_model = _DictionaryListModel(("message", "kind", "toastId"), self)
        self._toast_timers: dict[str, QTimer] = {}
        self._task_model = _DictionaryListModel(
            ("taskId", "title", "progress", "cancellable"), self
        )
        # 会话删除等主面板确认与工具审批确认分开排队，避免独立窗口误点危险动作
        self._confirmations: dict[str, list[ConfirmationRequest]] = {
            channel: [] for channel in CONFIRMATION_CHANNELS
        }
        self._agent_interaction: AgentInteractionRequest | None = None
        self._confirmation_callbacks: dict[
            str, tuple[Callable[[], None] | None, Callable[[], None] | None]
        ] = {}
        self._task_cancel_actions: dict[str, Callable[[], None]] = {}
        self._page_errors: dict[str, str] = {}
        self._timers: dict[str, QTimer] = {}
        for channel in CONFIRMATION_CHANNELS:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda channel=channel: self._timeout_current(channel))
            self._timers[channel] = timer

    @Property(QObject, constant=True)
    def toast_model(self) -> _DictionaryListModel:
        return self._toast_model

    @Property(QObject, constant=True)
    def toastModel(self) -> _DictionaryListModel:
        return self._toast_model

    @Property(QObject, constant=True)
    def task_model(self) -> _DictionaryListModel:
        return self._task_model

    @Property(QObject, constant=True)
    def taskModel(self) -> _DictionaryListModel:
        return self._task_model

    @Property("QVariantMap", notify=confirmationChanged)
    def current_confirmation(self) -> dict[str, object]:
        """独立窗口确认优先，保证工具审批在任意界面都能立即处理"""

        for channel in (WINDOW_CHANNEL, MAIN_CHANNEL):
            pending = self._confirmations[channel]
            if pending:
                return pending[0].to_map()
        return {}

    @Property("QVariantMap", notify=confirmationChanged)
    def currentConfirmation(self) -> dict[str, object]:
        return self.current_confirmation

    @Property("QVariantMap", notify=windowConfirmationChanged)
    def current_window_confirmation(self) -> dict[str, object]:
        """独立确认窗口只显示工具审批，不显示会话删除等主面板确认"""

        pending = self._confirmations[WINDOW_CHANNEL]
        return {} if not pending else pending[0].to_map()

    @Property("QVariantMap", notify=windowConfirmationChanged)
    def currentWindowConfirmation(self) -> dict[str, object]:
        return self.current_window_confirmation

    @Property("QVariantMap", notify=agentInteractionChanged)
    def current_agent_interaction(self) -> dict[str, object]:
        return self._agent_interaction.to_map() if self._agent_interaction else {}

    @Property("QVariantMap", notify=agentInteractionChanged)
    def currentAgentInteraction(self) -> dict[str, object]:
        return self.current_agent_interaction

    def request_agent_interaction(self, request: AgentInteractionRequest) -> None:
        self._agent_interaction = request
        self.agentInteractionChanged.emit()

    @Slot(str, str)
    def resolve_agent_interaction(self, request_id: str, result: object) -> None:
        if self._agent_interaction is None or self._agent_interaction.request_id != request_id:
            return
        self._agent_interaction = None
        self.agentInteractionResolved.emit(request_id, result)
        self.agentInteractionChanged.emit()

    @Property("QVariantMap", notify=pageErrorsChanged)
    def page_errors(self) -> dict[str, str]:
        return dict(self._page_errors)

    @Property("QVariantMap", notify=pageErrorsChanged)
    def pageErrors(self) -> dict[str, str]:
        return self.page_errors

    @Slot(str, str)
    def toast(self, message: str, kind: str = "info") -> None:
        if not isinstance(message, str) or not message.strip():
            return
        safe_kind = kind if kind in self._TOAST_KINDS else "info"
        toast_id = str(uuid4())
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setProperty("toastId", toast_id)
        timer.timeout.connect(self._expire_toast)
        self._toast_timers[toast_id] = timer
        self._toast_model.append({"message": message[:240], "kind": safe_kind, "toastId": toast_id})
        timer.start(self._TOAST_TIMEOUT_MS)

    @Slot(int)
    def dismiss_toast(self, row: int) -> None:
        toast_id = self._toast_model.data(self._toast_model.index(row), Qt.UserRole + 3)
        timer = self._toast_timers.pop(toast_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._toast_model.remove(row)

    @Slot()
    def clear_toasts(self) -> None:
        # 导航时仅清理短提示，保留独立的确认请求、任务和页面错误
        while self._toast_model.rowCount():
            self.dismiss_toast(self._toast_model.rowCount() - 1)

    @Slot()
    def _expire_toast(self) -> None:
        # 到期按稳定标识查找，避免前一条关闭后行号变化误删后续提示
        timer = self.sender()
        if timer is None:
            return
        toast_id = timer.property("toastId")
        for row in range(self._toast_model.rowCount()):
            if self._toast_model.data(self._toast_model.index(row), Qt.UserRole + 3) == toast_id:
                self.dismiss_toast(row)
                return

    @Slot(str, str, float, bool)
    def upsert_task(
        self,
        task_id: str,
        title: str,
        progress: float,
        cancellable: bool,
        cancel_action: Callable[[], None] | None = None,
    ) -> None:
        if not task_id or not title or type(cancellable) is not bool:
            return
        normalized = -1.0 if progress < 0 else min(float(progress), 1.0)
        self._task_model.upsert(
            "taskId",
            task_id,
            {
                "taskId": task_id,
                "title": title[:120],
                "progress": normalized,
                "cancellable": cancellable,
            },
        )
        if cancel_action is not None:
            self._task_cancel_actions[task_id] = cancel_action

    @Slot(int)
    def dismiss_task(self, row: int) -> None:
        self._task_model.remove(row)

    def confirm(
        self,
        request: ConfirmationRequest,
        approve: Callable[[], None] | None = None,
        reject: Callable[[], None] | None = None,
    ) -> None:
        pending = self._confirmations[request.channel]
        if any(item.request_id == request.request_id for item in pending):
            return
        was_empty = not pending
        pending.append(request)
        self._confirmation_callbacks[request.request_id] = (approve, reject)
        if was_empty:
            self._activate_current()

    @Slot(str, bool)
    def resolve_confirmation(self, request_id: str, approved: bool) -> None:
        for channel in CONFIRMATION_CHANNELS:
            pending = self._confirmations[channel]
            if not pending or pending[0].request_id != request_id:
                continue
            self._timers[channel].stop()
            pending.pop(0)
            callbacks = self._confirmation_callbacks.pop(request_id, (None, None))
            self.confirmationResolved.emit(request_id, approved)
            callback = callbacks[0] if approved else callbacks[1]
            if callback is not None:
                callback()
            self._activate_current()
            return

    @Slot(str)
    def cancel_task(self, task_id: str) -> None:
        action = self._task_cancel_actions.get(task_id)
        if action is None:
            return
        action()
        self.taskCancelRequested.emit(task_id)

    @Slot(str, str)
    def set_page_error(self, page: str, message: str) -> None:
        if not page or not message:
            return
        self._page_errors[page] = message[:512]
        self.pageErrorsChanged.emit()

    @Slot(str)
    def clear_page_error(self, page: str) -> None:
        if page in self._page_errors:
            self._page_errors.pop(page)
            self.pageErrorsChanged.emit()

    def _activate_current(self) -> None:
        self.confirmationChanged.emit()
        self.windowConfirmationChanged.emit()
        for channel in CONFIRMATION_CHANNELS:
            pending = self._confirmations[channel]
            if pending:
                self._timers[channel].start(pending[0].timeout_ms)

    def _timeout_current(self, channel: str) -> None:
        pending = self._confirmations.get(channel)
        if pending:
            self.resolve_confirmation(pending[0].request_id, False)
