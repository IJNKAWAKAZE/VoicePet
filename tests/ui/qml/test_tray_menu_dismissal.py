from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QWindow

from ui.tray_menu import TrayMenuDismissal


def test_shell_focus_changes_do_not_close_tool_menu(qapp):
    window = QWindow()
    window.setFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    window.setGeometry(100, 100, 188, 286)
    guard = TrayMenuDismissal(window)
    try:
        window.show()
        window.activeChanged.emit()
        assert window.isVisible()
        # The opening right-click is held outside; only a fresh press dismisses.
        guard._previous = {2}
        guard._handle_keys({2}, QPoint(0, 0))
        assert window.isVisible()
        guard._handle_keys(set(), QPoint(0, 0))
        guard._handle_keys({1}, QPoint(120, 120))
        assert window.isVisible()
        guard._handle_keys(set(), QPoint(0, 0))
        guard._handle_keys({1}, QPoint(0, 0))
        assert not window.isVisible()
        for keys in ({27}, {91}, {92}, {18, 9}):
            window.show()
            guard._previous = set()
            guard._handle_keys(keys, QPoint(120, 120))
            assert not window.isVisible()
    finally:
        guard.close()
        window.close()
