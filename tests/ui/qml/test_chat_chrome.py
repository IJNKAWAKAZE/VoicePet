import pytest
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtTest import QTest
from test_qml_application import build_controller, completed


@pytest.fixture
def chrome_app(qapp):
    controller, runtime, _tray, qml, shell, chat, *_ = build_controller(qapp)
    controller.start()
    shell.show_main("chat")
    QTest.qWait(60)
    yield qml.root_objects[0], runtime, chat
    controller.close()


def test_new_session_is_full_width_primary_button(chrome_app):
    root, _, _ = chrome_app
    button = root.findChild(QObject, "newSessionButton")
    sessions = root.findChild(QObject, "sessionList")
    assert button.property("kind") == "primary"
    assert button.property("text") == "＋ 新建会话"
    assert button.width() == pytest.approx(sessions.width())
    assert button.height() >= 40
    assert button.mapToScene(QPointF()).y() + button.height() < sessions.mapToScene(QPointF()).y()


def test_new_session_button_creates_session_with_mouse(chrome_app):
    root, runtime, chat = chrome_app
    runtime.new_session = lambda: completed("created-session")
    button = root.findChild(QObject, "newSessionButton")
    QTest.mouseClick(root, Qt.LeftButton, pos=button.mapToScene(
        QPointF(button.width() / 2, button.height() / 2),
    ).toPoint())
    assert chat.activeSessionId == "created-session"
    assert chat.messageModel.rowCount() == 0


def window_buttons(root):
    return [item for item in root.findChildren(QObject) if item.property("flat") is True]


def test_title_button_hover_background_stays_inside_window(chrome_app):
    root, _, _ = chrome_app
    close = window_buttons(root)[-1]
    QTest.mouseMove(root, close.mapToScene(QPointF(close.width() / 2, close.height() / 2)).toPoint())
    QTest.qWait(30)
    assert close.property("hovered")
    background = close.property("background")
    position = background.mapToScene(QPointF())
    assert position.y() >= 4
    assert position.x() + background.width() <= root.width() - 4
    assert background.property("radius") >= 6
    assert background.property("color").alpha() > 0


def test_title_glyphs_are_fixed_size_and_centered(chrome_app):
    root, _, _ = chrome_app
    for button in window_buttons(root):
        glyph = button.findChild(QObject, "windowGlyph")
        assert glyph is not None
        assert glyph.width() == glyph.height() == 24
        glyph_center = glyph.mapToScene(QPointF(12, 12))
        button_center = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        assert glyph_center.x() == pytest.approx(button_center.x())
        assert glyph_center.y() == pytest.approx(button_center.y())


def test_close_only_hides_main_panel(chrome_app):
    root, _, _ = chrome_app
    pet = root.findChild(QObject, "petWindow")
    close = window_buttons(root)[-1]
    QTest.mouseClick(root, Qt.LeftButton, pos=close.mapToScene(
        QPointF(close.width() / 2, close.height() / 2),
    ).toPoint())
    assert not root.isVisible()
    assert pet.property("visible")
