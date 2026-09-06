from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from core.config import UiConfig
from ui.viewmodels.theme import ThemeViewModel


def test_combobox_uses_single_chevron_and_still_opens_with_mouse(qapp):
    QQuickStyle.setStyle("Basic")
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(
        Path("ui/qml/components/AppComboBox.qml").resolve(),
    )))
    root = component.createWithInitialProperties({"theme": theme, "model": ["small", "medium"]})
    window = QQuickWindow()
    try:
        assert root is not None, component.errors()
        root.setParentItem(window.contentItem())
        root.setWidth(240)
        window.resize(240, 200)
        window.show()
        QTest.qWait(30)
        assert root.findChild(QObject, "comboDownIndicator") is not None
        QTest.mouseClick(window, Qt.LeftButton, pos=QPoint(220, 20))
        qapp.processEvents()
        assert root.findChild(QObject, "comboDownIndicator").property("rotation") == 180
        QTest.keyClick(window, Qt.Key_Down)
        QTest.keyClick(window, Qt.Key_Return)
        assert root.property("currentIndex") == 1
    finally:
        window.close()
        root.deleteLater()
        engine.deleteLater()
        qapp.processEvents()
