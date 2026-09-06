from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtGui import QCursor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QSystemTrayIcon
from test_qml_application import build_controller

from ui.tray import TrayController


def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)


def test_custom_tray_mode_routes_context_activation_at_cursor(qapp, monkeypatch):
    tray = TrayController()
    requested = QSignalSpy(tray.context_menu_requested)
    monkeypatch.setattr(QCursor, "pos", lambda: QPoint(-180, 650))
    tray.set_custom_menu_enabled(True)
    tray._tray.activated.emit(QSystemTrayIcon.ActivationReason.Context)
    assert requested.count() == 1
    assert requested.at(0) == [QPoint(-180, 650)]
    assert tray._tray.contextMenu() is None
    tray.set_custom_menu_enabled(False)
    assert tray._tray.contextMenu() is not None
    tray.close()


def test_tray_menu_actions_and_escape_work_without_showing_main_window(qapp):
    controller, _service, tray, qml, shell, *_ = build_controller(qapp)
    controller.start()
    try:
        root = qml.root_objects[0]
        menu = root.findChild(QObject, "trayMenuWindow")
        assert menu is not None
        tray.context_menu_requested.emit(QPoint(790, 590))
        QTest.qWait(30)
        assert menu.property("visible") is True
        assert root.property("visible") is False
        assert menu.property("height") > 200
        available = qapp.primaryScreen().availableGeometry()
        assert available.contains(menu.geometry())
        action_names = {item.objectName() for item in visual_items(menu.contentItem())}
        assert "quickMenuAction_settings" not in action_names
        assert "quickMenuAction_manual" not in action_names
        settings_button = next((item for item in visual_items(menu.contentItem())
                                if item.objectName() == "quickMenuAction_open"), None)
        assert settings_button is not None
        QTest.mouseClick(menu, Qt.LeftButton,
                        pos=settings_button.mapToScene(QPoint(30, 18)).toPoint())
        qapp.processEvents()
        assert shell.current_section == "chat"
        assert root.property("visible") is True
        assert menu.property("visible") is False
        tray.context_menu_requested.emit(QPoint(790, 590))
        QTest.keyClick(menu, Qt.Key_Escape)
        qapp.processEvents()
        assert menu.property("visible") is False
        tray.context_menu_requested.emit(QPoint(790, 590))
        QTest.qWait(30)
        root.requestActivate()
        QTest.mouseClick(root, Qt.LeftButton, pos=QPoint(250, 40))
        QTest.qWait(30)
        assert menu.property("visible") is False
    finally:
        controller.close()


def test_tray_menu_listen_visibility_click_through_and_exit_are_routed(qapp):
    controller, service, _tray, qml, shell, *_ = build_controller(qapp)
    controller.start()
    root = qml.root_objects[0]
    try:
        menu = root.findChild(QObject, "trayMenuWindow")
        assert menu is not None
        pet = root.findChild(QObject, "petWindow")
        settings = root.findChild(QObject, "settingsPage").property("settings")
        menu.actionRequested.emit("clickThrough")
        assert settings.config.ui.pet_click_through is True
        assert pet.flags() & Qt.WindowTransparentForInput
        menu.actionRequested.emit("clickThrough")
        assert settings.config.ui.pet_click_through is False
        assert not pet.flags() & Qt.WindowTransparentForInput
        menu.actionRequested.emit("listen")
        assert service.activations == ["click"]
        menu.actionRequested.emit("visibility")
        assert pet.property("visible") is False
        menu.actionRequested.emit("visibility")
        assert pet.property("visible") is True
        menu.actionRequested.emit("manual")
        assert shell.current_section == "chat"
        menu.actionRequested.emit("quit")
        assert controller.is_closed
        assert service.closed == 1
    finally:
        controller.close()
