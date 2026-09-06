from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from threading import Thread

import pytest
from PySide6.QtCore import Qt

from ui.viewmodels.diagnostics import DiagnosticsViewModel


class Status(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class Result:
    component: str
    status: Status
    safe_message: str
    duration_ms: int


@dataclass
class Report:
    results: tuple[Result, ...]


def completed(value=None, error=None):
    future = Future()
    if error is None:
        future.set_result(value)
    else:
        future.set_exception(error)
    return future


class Runtime:
    def __init__(self):
        self.report = Report(
            (
                Result("audio", Status.HEALTHY, "正常", 12),
                Result("llm", Status.DEGRADED, "响应偏慢", 80),
                Result("wake", Status.UNAVAILABLE, "模型缺失", 2),
            )
        )
        self.fail = False
        self.exports = []

    def run_diagnostics(self):
        if self.fail:
            return completed(error=RuntimeError("private"))
        return completed(self.report)

    def export_diagnostics(self, destination):
        self.exports.append(destination)
        return completed()


def test_diagnostic_statuses_are_mapped_to_ui_roles():
    viewmodel = DiagnosticsViewModel(Runtime())

    viewmodel.run_all()

    model = viewmodel.diagnostic_model
    assert model.rowCount() == 3
    assert model.data(model.index(0), Qt.UserRole + 2) == "healthy"
    assert model.data(model.index(1), Qt.UserRole + 2) == "degraded"
    assert model.data(model.index(2), Qt.UserRole + 2) == "failed"
    assert model.data(model.index(2), Qt.UserRole + 5) is True


def test_failed_run_keeps_previous_results():
    runtime = Runtime()
    viewmodel = DiagnosticsViewModel(runtime)
    viewmodel.run_all()
    runtime.fail = True

    viewmodel.run_all()

    assert viewmodel.diagnostic_model.rowCount() == 3
    assert viewmodel.error != ""


def test_retry_and_export_delegate_to_runtime():
    runtime = Runtime()
    viewmodel = DiagnosticsViewModel(runtime)

    viewmodel.retry("wake")
    viewmodel.export_bundle("D:/exports/diagnostics.zip")

    assert viewmodel.diagnostic_model.rowCount() == 3
    assert runtime.exports == ["D:/exports/diagnostics.zip"]


@pytest.mark.parametrize("synchronous", [True, False])
def test_export_failure_replaces_pending_status(synchronous):
    class FailingRuntime(Runtime):
        def export_diagnostics(self, destination):
            if synchronous:
                raise RuntimeError("private")
            return completed(error=RuntimeError("private"))

    viewmodel = DiagnosticsViewModel(FailingRuntime())
    viewmodel.export_bundle("D:/exports/diagnostics.zip")
    assert "导出" in viewmodel.statusMessage
    assert "失败" in viewmodel.statusMessage or "不可用" in viewmodel.statusMessage
    assert "正在" not in viewmodel.statusMessage


class ConnectionRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.connection = Future()
        self.connection_calls = 0

    def test_llm_connection(self):
        self.connection_calls += 1
        return self.connection


def test_connection_has_immediate_feedback_prevents_duplicates_and_keeps_full_results():
    runtime = ConnectionRuntime()
    viewmodel = DiagnosticsViewModel(runtime)
    viewmodel.run_all()
    changes = []
    viewmodel.connectionStatusChanged.connect(lambda: changes.append(viewmodel.connectionStatus))

    viewmodel.test_connection()
    viewmodel.test_connection()

    assert viewmodel.connectionRunning is True
    assert viewmodel.running is False
    assert "正在" in viewmodel.connectionStatus
    assert "当前运行" in viewmodel.connectionStatus
    assert runtime.connection_calls == 1
    runtime.connection.set_result(Result("llm", Status.HEALTHY, "private-secret", 42))
    assert viewmodel.connectionRunning is False
    assert "成功" in viewmodel.connectionStatus
    assert "42 ms" in viewmodel.connectionStatus
    assert "private-secret" not in viewmodel.connectionStatus
    assert viewmodel.diagnostic_model.rowCount() == 3
    assert len(changes) == 2


@pytest.mark.parametrize("status, expected", [
    (Status.DEGRADED, "未就绪"), (Status.UNAVAILABLE, "失败"),
])
def test_connection_unhealthy_results_have_safe_visible_feedback(status, expected):
    runtime = ConnectionRuntime()
    viewmodel = DiagnosticsViewModel(runtime)
    viewmodel.test_connection()
    runtime.connection.set_result(Result("llm", status, "api_key=private-secret", 20))
    assert viewmodel.connectionRunning is False
    assert expected in viewmodel.connectionStatus
    assert "private-secret" not in viewmodel.connectionStatus


@pytest.mark.parametrize("synchronous", [True, False])
def test_connection_errors_end_pending_state_without_exposing_details(synchronous):
    class FailingRuntime(ConnectionRuntime):
        def test_llm_connection(self):
            if synchronous:
                raise RuntimeError("api_key=private-secret")
            return completed(error=RuntimeError("api_key=private-secret"))

    viewmodel = DiagnosticsViewModel(FailingRuntime())
    viewmodel.test_connection()
    assert viewmodel.connectionRunning is False
    assert "失败" in viewmodel.connectionStatus or "不可用" in viewmodel.connectionStatus
    assert "正在" not in viewmodel.connectionStatus
    assert "private-secret" not in viewmodel.connectionStatus


def test_connection_worker_completion_keeps_concurrent_full_diagnostic_busy(qapp):
    class PendingRuntime(ConnectionRuntime):
        def __init__(self):
            super().__init__()
            self.full_run = Future()

        def run_diagnostics(self):
            return self.full_run

    runtime = PendingRuntime()
    viewmodel = DiagnosticsViewModel(runtime)
    viewmodel.run_all()
    viewmodel.test_connection()
    worker = Thread(target=lambda: runtime.connection.set_result(
        Result("llm", Status.HEALTHY, "正常", 7),
    ))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert viewmodel.connectionRunning is True

    qapp.processEvents()

    assert viewmodel.connectionRunning is False
    assert "成功" in viewmodel.connectionStatus
    assert viewmodel.running is True
    runtime.full_run.set_result(runtime.report)
    assert viewmodel.running is False
    assert viewmodel.diagnostic_model.rowCount() == 3
    assert "成功" in viewmodel.connectionStatus
