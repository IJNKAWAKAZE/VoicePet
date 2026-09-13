from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from ui.viewmodels.dialogs import (
    WINDOW_CHANNEL,
    ConfirmationRequest,
    DialogCoordinator,
)


def test_toast_queue_exposes_safe_rows_and_can_be_consumed():
    dialogs = DialogCoordinator()

    dialogs.toast("已保存", "success")
    dialogs.toast("未知类型", "other")

    assert dialogs.toast_model.rowCount() == 2
    assert dialogs.toast_model.data(dialogs.toast_model.index(0), Qt.UserRole + 1) == "已保存"
    assert dialogs.toast_model.data(dialogs.toast_model.index(1), Qt.UserRole + 2) == "info"
    dialogs.dismiss_toast(0)
    assert dialogs.toast_model.rowCount() == 1


def test_task_with_same_id_updates_existing_row():
    dialogs = DialogCoordinator()

    dialogs.upsert_task("download", "下载模型", 0.2, True)
    dialogs.upsert_task("download", "下载模型", 0.8, False)

    assert dialogs.task_model.rowCount() == 1
    index = dialogs.task_model.index(0)
    assert dialogs.task_model.data(index, Qt.UserRole + 3) == 0.8
    assert dialogs.task_model.data(index, Qt.UserRole + 4) is False


def test_confirmation_requests_are_fifo_and_resolve_once():
    dialogs = DialogCoordinator()
    resolved = QSignalSpy(dialogs.confirmationResolved)
    first = ConfirmationRequest("one", "第一项", "摘要", "影响", False, "高", 30_000)
    second = ConfirmationRequest("two", "第二项", "摘要", "影响", True, "中", 30_000)

    dialogs.confirm(first)
    dialogs.confirm(second)
    dialogs.resolve_confirmation("one", False)
    dialogs.resolve_confirmation("one", True)

    assert resolved.count() == 1
    assert resolved.at(0) == ["one", False]
    assert dialogs.current_confirmation["requestId"] == "two"


def test_window_approval_cannot_resolve_pending_session_deletion():
    dialogs = DialogCoordinator()
    deleted: list[bool] = []
    approved: list[bool] = []
    dialogs.confirm(
        ConfirmationRequest(
            "delete-session:one",
            "删除这段会话？",
            "删除后无法恢复",
            "仅删除会话历史",
            False,
            "本地数据",
            show_details=False,
            confirm_label="删除",
        ),
        lambda: deleted.append(True),
    )
    dialogs.confirm(
        ConfirmationRequest(
            "call-1",
            "允许这项操作？",
            "打开记事本",
            "操作将由本机工具执行",
            False,
            "低",
            channel=WINDOW_CHANNEL,
        ),
        lambda: approved.append(True),
    )

    # 独立窗口只看到工具审批，主面板确认仍然排队等待
    assert dialogs.currentWindowConfirmation["requestId"] == "call-1"
    assert dialogs.currentConfirmation["requestId"] == "call-1"

    dialogs.resolve_confirmation("call-1", True)

    assert approved == [True]
    assert deleted == []
    assert dialogs.currentWindowConfirmation == {}
    assert dialogs.currentConfirmation["requestId"] == "delete-session:one"
    dialogs.resolve_confirmation("delete-session:one", True)
    assert deleted == [True]


def test_confirmation_timeout_rejects_by_default(qapp):
    dialogs = DialogCoordinator()
    resolved = QSignalSpy(dialogs.confirmationResolved)
    dialogs.confirm(ConfirmationRequest("short", "超时", "摘要", "影响", False, "高", 1))

    QSignalSpy(dialogs.confirmationChanged).wait(20)
    qapp.processEvents()

    assert resolved.count() == 1
    assert resolved.at(0) == ["short", False]


def test_confirmation_callback_is_consumed_only_once():
    dialogs = DialogCoordinator()
    approved = []
    rejected = []
    request = ConfirmationRequest("callback", "确认", "摘要", "影响", True, "中")
    dialogs.confirm(request, lambda: approved.append(True), lambda: rejected.append(True))

    dialogs.resolve_confirmation("callback", True)
    dialogs.resolve_confirmation("callback", False)

    assert approved == [True]
    assert rejected == []


def test_task_cancellation_calls_only_matching_action():
    dialogs = DialogCoordinator()
    calls = []
    dialogs.upsert_task("one", "任务一", 0.2, True, lambda: calls.append("one"))
    dialogs.upsert_task("two", "任务二", 0.2, True, lambda: calls.append("two"))

    dialogs.cancel_task("two")

    assert calls == ["two"]


def test_page_error_is_persistent_and_not_added_to_toast_queue():
    dialogs = DialogCoordinator()

    dialogs.set_page_error("settings", "凭据需要重新配置")

    assert dialogs.page_errors == {"settings": "凭据需要重新配置"}
    assert dialogs.toast_model.rowCount() == 0
