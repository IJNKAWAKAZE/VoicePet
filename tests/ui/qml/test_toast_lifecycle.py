import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QSignalSpy, QTest
from test_qml_application import build_controller

from ui.viewmodels.dialogs import ConfirmationRequest, DialogCoordinator


def toast_messages(dialogs):
    model = dialogs.toastModel
    return [model.data(model.index(row), Qt.UserRole + 1) for row in range(model.rowCount())]


def test_toast_expires_without_clicking(qapp, monkeypatch):
    monkeypatch.setattr(DialogCoordinator, "_TOAST_TIMEOUT_MS", 40, raising=False)
    dialogs = DialogCoordinator()
    removed = QSignalSpy(dialogs.toastModel.rowsRemoved)
    dialogs.toast("会话历史已更新", "success")
    assert toast_messages(dialogs) == ["会话历史已更新"]
    assert removed.wait(1000)
    assert toast_messages(dialogs) == []


def test_expiration_follows_toast_identity_after_earlier_row_is_dismissed(qapp, monkeypatch):
    dialogs = DialogCoordinator()
    monkeypatch.setattr(DialogCoordinator, "_TOAST_TIMEOUT_MS", 5000, raising=False)
    dialogs.toast("手动关闭的第一条")
    monkeypatch.setattr(DialogCoordinator, "_TOAST_TIMEOUT_MS", 40, raising=False)
    dialogs.toast("应当到期的第二条")
    monkeypatch.setattr(DialogCoordinator, "_TOAST_TIMEOUT_MS", 5000, raising=False)
    dialogs.toast("仍需显示的第三条")
    dialogs.dismiss_toast(0)
    removed = QSignalSpy(dialogs.toastModel.rowsRemoved)
    assert removed.wait(1000)
    assert toast_messages(dialogs) == ["仍需显示的第三条"]


@pytest.mark.parametrize("navigation", ["section", "category", "hide"])
def test_navigation_clears_toasts_but_not_confirmation(qapp, navigation):
    controller, _, _, qml, shell, _, dialogs, *_ = build_controller(qapp)
    controller.start()
    try:
        shell.show_main("settings" if navigation == "category" else "chat")
        QTest.qWait(30)
        dialogs.confirm(ConfirmationRequest("pending", "确认操作", "摘要", "影响", False, "高"))
        dialogs.toast("会话历史已更新", "success")
        if navigation == "section":
            shell.navigate("settings")
        elif navigation == "category":
            qml.root_objects[0].findChild(QObject, "settingsPage").setProperty("category", "voice")
        else:
            shell.hide_main()
        qapp.processEvents()
        assert toast_messages(dialogs) == []
        assert dialogs.currentConfirmation["requestId"] == "pending"
    finally:
        controller.close()
