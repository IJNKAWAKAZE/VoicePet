"""组件诊断状态、重试和导出 ViewModel"""

from __future__ import annotations

from concurrent.futures import Future
from enum import Enum
from typing import Any, ClassVar, Protocol

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

_INVALID_INDEX = QModelIndex()


class DiagnosticsRuntimeProtocol(Protocol):
    def run_diagnostics(self) -> Future[Any]: ...

    def test_llm_connection(self) -> Future[Any]: ...

    def export_diagnostics(self, destination: str) -> Future[None]: ...


class DiagnosticModel(QAbstractListModel):
    """向 QML 发布脱敏后的组件检查结果"""

    _ROLES: ClassVar[dict[int, bytes]] = {
        Qt.UserRole + 1: b"component",
        Qt.UserRole + 2: b"status",
        Qt.UserRole + 3: b"message",
        Qt.UserRole + 4: b"durationMs",
        Qt.UserRole + 5: b"retryable",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, object]] = []

    def roleNames(self) -> dict[int, bytes]:
        return self._ROLES

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        name = self._ROLES.get(role)
        return None if name is None else self._items[index.row()].get(name.decode())

    def replace_items(self, items: list[dict[str, object]]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()


class DiagnosticsViewModel(QObject):
    """诊断失败时保留上一份可用结果"""

    runningChanged = Signal()
    errorChanged = Signal()
    errorOccurred = Signal(str)
    statusMessageChanged = Signal()
    connectionStatusChanged = Signal()
    _futureFinished = Signal(str, object, object)

    def __init__(
        self,
        runtime: DiagnosticsRuntimeProtocol,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._model = DiagnosticModel(self)
        self._running = False
        self._error = ""
        self._status_message = ""
        self._connection_running = False
        self._connection_status = ""
        self._futureFinished.connect(self._handle_future)

    @Property(QObject, constant=True)
    def diagnostic_model(self) -> DiagnosticModel:
        return self._model

    @Property(QObject, constant=True)
    def diagnosticModel(self) -> DiagnosticModel:
        return self._model

    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    @Property(str, notify=errorChanged)
    def error(self) -> str:
        return self._error

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @Property(bool, notify=connectionStatusChanged)
    def connectionRunning(self) -> bool:
        return self._connection_running

    @Property(str, notify=connectionStatusChanged)
    def connectionStatus(self) -> str:
        return self._connection_status

    @Slot()
    def test_connection(self) -> None:
        if self._connection_running:
            return
        self._set_connection_status(True, "正在测试当前运行配置的 LLM 连接…")
        try:
            self._watch("connection", self._runtime.test_llm_connection())
        except Exception:  # noqa: BLE001 连接入口失败必须恢复按钮并隐藏敏感异常
            self._set_connection_status(False, "连接测试当前不可用，请稍后重试")

    @Slot(QUrl)
    def export_url(self, destination: QUrl) -> None:
        if destination.isLocalFile():
            self.export_bundle(destination.toLocalFile())

    @Slot()
    def run_all(self) -> None:
        if self._running:
            return
        self._set_running(True)
        try:
            self._watch("run", self._runtime.run_diagnostics())
        except RuntimeError:
            self._fail("诊断当前不可用")

    @Slot(str)
    def retry(self, component: str) -> None:
        if isinstance(component, str) and component:
            self.run_all()

    @Slot(str)
    def export_bundle(self, destination: str) -> None:
        if not isinstance(destination, str) or not destination:
            return
        try:
            self._status_message = "正在导出诊断包…"
            self.statusMessageChanged.emit()
            self._watch("export", self._runtime.export_diagnostics(destination))
        except RuntimeError:
            self._fail("诊断包导出当前不可用")

    def _watch(self, operation: str, future: Future[Any]) -> None:
        def done(completed: Future[Any]) -> None:
            try:
                self._futureFinished.emit(operation, completed.result(), None)
            except Exception as error:  # noqa: BLE001
                self._futureFinished.emit(operation, None, error)

        future.add_done_callback(done)

    @Slot(str, object, object)
    def _handle_future(self, operation: str, result: object, error: object) -> None:
        if operation == "connection":
            self._set_connection_status(False, _connection_message(result, error))
            return
        if operation == "run":
            self._set_running(False)
        if error is not None:
            self._fail("诊断包导出失败，请重试" if operation == "export" else "诊断运行失败，请重试")
            return
        self._set_error("")
        if operation != "run":
            self._status_message = "诊断包已导出"
            self.statusMessageChanged.emit()
            return
        results = tuple(getattr(result, "results", ()))
        self._model.replace_items([_diagnostic_item(item) for item in results])

    def _set_connection_status(self, running: bool, message: str) -> None:
        self._connection_running = running
        self._connection_status = message
        self.connectionStatusChanged.emit()

    def _set_running(self, value: bool) -> None:
        if value != self._running:
            self._running = value
            self.runningChanged.emit()

    def _set_error(self, value: str) -> None:
        if value != self._error:
            self._error = value
            self.errorChanged.emit()

    def _fail(self, message: str) -> None:
        self._status_message = message
        self.statusMessageChanged.emit()
        self._set_running(False)
        self._set_error(message)
        self.errorOccurred.emit(message)


def _diagnostic_item(result: object) -> dict[str, object]:
    raw_status = getattr(result, "status", "unavailable")
    status = raw_status.value if isinstance(raw_status, Enum) else str(raw_status)
    ui_status = "failed" if status == "unavailable" else status
    return {
        "component": str(getattr(result, "component", "unknown")),
        "status": ui_status,
        "message": str(getattr(result, "safe_message", "检查失败"))[:512],
        "durationMs": int(getattr(result, "duration_ms", 0)),
        "retryable": ui_status != "healthy",
    }


def _connection_message(result: object, error: object) -> str:
    """连接提示使用固定文案，避免展示探针上下文或异常正文"""
    if error is not None:
        return "连接测试失败，请检查当前运行配置和网络后重试"
    raw_status = getattr(result, "status", "unavailable")
    status = raw_status.value if isinstance(raw_status, Enum) else raw_status
    if status == "healthy":
        duration = max(0, int(getattr(result, "duration_ms", 0)))
        return f"当前运行配置连接测试成功（{duration} ms）"
    if status == "degraded":
        return "当前运行配置的 LLM 未就绪或已降级，请检查配置后重试"
    return "连接测试失败或服务不可用，请检查当前运行配置和网络后重试"
