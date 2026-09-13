import pytest
from PySide6.QtCore import QObject
from test_qml_application import Runtime, build_controller

from core.events import ApprovalRequested, CorrelationId, TurnId
from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.dialogs import (
    WINDOW_CHANNEL,
    ConfirmationRequest,
    DialogCoordinator,
)


def session_removal_request(request_id: str = "delete-session:one", label: str = "删除"):
    return ConfirmationRequest(
        request_id,
        "删除这段会话？" if label == "删除" else "清空全部会话？",
        "删除后无法恢复，会同时删除会话相关的近期摘要，长期记忆不受影响",
        "仅删除会话历史，保留长期记忆、设置和桌宠形象",
        False,
        "本地数据",
        show_details=False,
        confirm_label=label,
    )


@pytest.mark.parametrize("clear_all,label", [(False, "删除"), (True, "清空")])
def test_session_removal_uses_one_explanation_and_specific_action(qapp, clear_all, label):
    dialogs = DialogCoordinator()
    chat = ChatViewModel(Runtime(), dialogs=dialogs)
    if clear_all:
        chat.request_clear_sessions()
    else:
        chat.request_delete_session("session-one")
    request = dialogs.currentConfirmation
    assert request["summary"] == "删除后无法恢复，会同时删除会话相关的近期摘要，长期记忆不受影响"
    assert request["showDetails"] is False
    assert request["confirmLabel"] == label
    assert request["channel"] == "main"


def test_tool_approval_keeps_risk_details_on_both_surfaces(qapp):
    controller, _, _, qml, shell, _, dialogs, *_ = build_controller(qapp)
    controller.start()
    try:
        shell.show_main("chat")
        dialogs.confirm(
            ConfirmationRequest(
                "tool",
                "执行工具？",
                "将执行操作",
                "影响文件",
                False,
                "高",
                channel=WINDOW_CHANNEL,
            )
        )
        root = qml.root_objects[0]
        tool = root.findChild(QObject, "toolConfirmationWindow")
        tool.show()
        qapp.processEvents()
        for surface in (root.findChild(QObject, "mainConfirmationSheet"), tool):
            risk = surface.findChild(QObject, "confirmationRisk")
            impact = surface.findChild(QObject, "confirmationImpact")
            approve = surface.findChild(QObject, "confirmationApprove")
            assert risk is not None and impact is not None and approve is not None
            assert risk.property("visible") is True
            assert impact.property("visible") is True
            assert approve.property("text") == "确认"
    finally:
        controller.close()


def test_session_removal_is_never_presented_in_tool_window(qapp):
    controller, _, _, qml, _, _, dialogs, *_ = build_controller(qapp)
    controller.start()
    try:
        chat = ChatViewModel(Runtime(), dialogs=dialogs)
        chat.request_delete_session("session-one")
        root = qml.root_objects[0]
        tool = root.findChild(QObject, "toolConfirmationWindow")
        tool.show()
        qapp.processEvents()
        assert dialogs.currentConfirmation["confirmLabel"] == "删除"
        assert dialogs.currentWindowConfirmation == {}
        assert tool.findChild(QObject, "confirmationApprove").property("text") != "删除"
    finally:
        controller.close()


def test_hidden_main_window_tool_approval_cannot_delete_session(qapp):
    controller, runtime, _, qml, _, _, dialogs, *_ = build_controller(qapp)
    controller.start()
    try:
        deleted = []
        dialogs.confirm(session_removal_request(), lambda: deleted.append(True))
        controller.handle_runtime_event(
            ApprovalRequested(
                TurnId.new(), CorrelationId.new(), "call-1", "打开记事本", "低"
            )
        )
        qapp.processEvents()
        tool = qml.root_objects[0].findChild(QObject, "toolConfirmationWindow")
        assert tool.property("visible") is True
        assert tool.findChild(QObject, "confirmationApprove").property("text") == "确认"

        dialogs.resolve_confirmation("call-1", True)
        qapp.processEvents()

        assert deleted == []
        assert len(runtime.approvals) == 1
        assert dialogs.currentConfirmation["requestId"] == "delete-session:one"
        assert dialogs.currentWindowConfirmation == {}
    finally:
        controller.close()
