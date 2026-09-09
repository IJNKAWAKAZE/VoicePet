"""通过真实 QML 按钮验证 Agent 审批回传"""

import pytest
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtTest import QTest

from core.events import AgentApprovalRequested, CorrelationId, TurnId
from tests.ui.qml.test_qml_application import build_controller, completed


def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)


@pytest.mark.parametrize(("label", "decision"), [("允许", "accept"), ("取消", "cancel")])
def test_click_resolves_agent_approval_and_closes_window(qapp, label, decision):
    controller, runtime, _, qml, _, _, dialogs, *_ = build_controller(qapp)
    replies = []

    def resolve(approval_id, result):
        replies.append((approval_id, result))
        return completed()

    runtime.resolve_agent_approval = resolve
    controller.start()
    try:
        window = qml.root_objects[0].findChild(QObject, "agentInteractionWindow")
        controller.handle_runtime_event(AgentApprovalRequested(TurnId.new(), CorrelationId.new(), "approval-1", "是否创建 test.txt？"))
        QTest.qWait(50)
        assert window.isVisible()
        button = next(item for item in visual_items(window.contentItem())
                      if item.property("text") == label and item.metaObject().indexOfSignal("clicked()") >= 0)
        point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
        QTest.qWait(20)
        assert replies == [("approval-1", decision)], [w.toString() for w in qml._warnings]
        assert not dialogs.currentAgentInteraction
        assert not window.isVisible()
    finally:
        controller.close()
