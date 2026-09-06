from PySide6.QtCore import QObject
from PySide6.QtTest import QTest
from test_qml_application import build_controller
from test_tray_menu import visual_items

from ui.pet_shell import PetChoice


def test_pet_delete_button_is_anchored_to_card_bottom_right(qapp):
    controller, service, _tray, qml, shell, *_ = build_controller(qapp)
    controller.start()
    try:
        root = qml.root_objects[0]
        root.show()
        shell.navigate("settings")
        page = root.findChild(QObject, "settingsPage")
        page.setProperty("category", "pets")
        pets = page.property("pets")
        choice = service.load_pet_catalog()[0]
        pets._catalog_loader = lambda: (PetChoice("custom", "测试形象", choice.directory, False),)
        pets.refresh()
        QTest.qWait(40)
        assert not any("支持 Codex v2" in str(item.property("text"))
                       for item in visual_items(root.contentItem()))
        card = next(item for item in visual_items(root.contentItem()) if item.property("petId") == "custom")
        button = next(item for item in visual_items(card) if item.property("text") == "删除")
        point = button.mapToItem(card, 0, 0)
        assert abs(card.height() - (point.y() + button.height()) - 12) <= 1
        assert abs(card.width() - (point.x() + button.width()) - 12) <= 1
    finally:
        controller.close()


def test_session_delete_uses_compact_cross_with_accessible_label(qapp):
    controller, _service, _tray, qml, _shell, chat, *_ = build_controller(qapp)
    controller.start()
    try:
        root = qml.root_objects[0]
        root.show()
        chat._session_model.reset_items([{
            "sessionId": "one", "title": "测试会话", "turnCount": 1, "active": True,
        }])
        QTest.qWait(30)
        session_list = root.findChild(QObject, "sessionList")
        buttons = [item for item in visual_items(session_list)
                   if item.property("text") == "×" and item.property("accessibleLabel") is not None]
        assert len(buttons) == 1
        assert buttons[0].property("accessibleLabel") == "删除会话"
    finally:
        controller.close()
