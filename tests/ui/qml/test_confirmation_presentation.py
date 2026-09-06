import pytest
from PySide6.QtCore import QObject
from test_qml_application import Runtime, build_controller

from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.dialogs import ConfirmationRequest, DialogCoordinator


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


@pytest.mark.parametrize("detailed", [False, True])
def test_both_confirmation_surfaces_preserve_tool_risk_only(qapp, detailed):
    controller, _, _, qml, shell, _, dialogs, *_ = build_controller(qapp)
    controller.start()
    try:
        shell.show_main("chat")
        if detailed:
            dialogs.confirm(ConfirmationRequest("tool", "执行工具？", "将执行操作", "影响文件", False, "高"))
        else:
            chat = ChatViewModel(Runtime(), dialogs=dialogs)
            chat.request_delete_session("session-one")
        root = qml.root_objects[0]
        tool = root.findChild(QObject, "toolConfirmationWindow")
        tool.show()
        qapp.processEvents()
        for surface in (root.findChild(QObject, "mainConfirmationSheet"), tool):
            risk = surface.findChild(QObject, "confirmationRisk")
            impact = surface.findChild(QObject, "confirmationImpact")
            approve = surface.findChild(QObject, "confirmationApprove")
            assert risk is not None and impact is not None and approve is not None
            assert risk.property("visible") is detailed
            assert impact.property("visible") is detailed
            assert approve.property("text") == ("确认" if detailed else "删除")
    finally:
        controller.close()
