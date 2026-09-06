import pytest
from PySide6.QtCore import QObject, QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from test_qml_application import build_controller


def test_drag_does_not_reverse_when_window_moves_under_cursor(qapp):
    controller, _service, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        pet.setPosition(100, 100)
        QTest.qWait(30)
        QTest.mousePress(pet, Qt.LeftButton, pos=QPoint(50, 50))
        QTest.mouseMove(pet, QPoint(90, 50))
        qapp.processEvents()
        assert pet.x() == 140
        # 窗口移动后相同全局位置对应新的局部坐标，不应被当成反向拖动
        QTest.mouseMove(pet, QPoint(50, 50))
        qapp.processEvents()
        assert pet.x() == 140
        QTest.mouseMove(pet, QPoint(70, 50))
        qapp.processEvents()
        assert pet.x() == 160
        QTest.mouseRelease(pet, Qt.LeftButton, pos=QPoint(50, 50))
    finally:
        controller.close()


@pytest.mark.parametrize("area", [QRect(48, 30, 752, 570), QRect(-1280, 40, 1280, 984)])
@pytest.mark.parametrize("corner", ["tl", "tr", "bl", "br"])
def test_speech_stays_inside_actual_screen_work_area(qapp, monkeypatch, area, corner):
    class Screen:
        def availableGeometry(self):
            return area

    monkeypatch.setattr(QGuiApplication, "screenAt", lambda point: Screen())
    controller, _service, _tray, qml, *_ = build_controller(qapp)
    controller.start()
    try:
        pet = qml.root_objects[0].findChild(QObject, "petWindow")
        x = area.left() if corner.endswith("l") else area.right() - pet.width() + 1
        y = area.top() if corner.startswith("t") else area.bottom() - pet.height() + 1
        pet.setPosition(x, y)
        pet.setProperty("speech", "边角气泡测试，检查长回复换行和窗口大小更新。" * 15)
        QTest.qWait(50)
        speech = pet.findChild(QObject, "petSpeechWindow")
        assert speech.isVisible()
        assert area.contains(speech.geometry()), (area, speech.geometry())
    finally:
        controller.close()
