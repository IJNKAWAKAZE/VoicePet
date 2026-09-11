"""自绘托盘工具窗口的关闭行为，不依赖 Popup 或 shell 焦点交接。"""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication


class TrayMenuDismissal(QObject):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self._previous = set()
        self._user32 = None
        if QGuiApplication.platformName() == "windows":
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            self._user32.GetAsyncKeyState.restype = wintypes.SHORT
            window.setFlag(Qt.WindowDoesNotAcceptFocus, True)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._poll)
        window.visibleChanged.connect(self._visibility_changed)
        QGuiApplication.instance().installEventFilter(self)

    def _keys(self):
        if self._user32 is None:
            return set()
        # 只读当前按下状态，不使用会被其他应用消耗的低位标志。
        return {key for key in (1, 2, 4, 5, 6, 27, 9, 18, 91, 92)
                if self._user32.GetAsyncKeyState(key) & 0x8000}

    def _visibility_changed(self, visible):
        self._timer.stop()
        if visible and self._user32 is not None:
            # 打开托盘菜单的那次右键/Win 键不能再被当作关闭操作。
            self._previous = self._keys()
            self._timer.start()

    def _poll(self):
        self._handle_keys(self._keys(), QCursor.pos())

    def _handle_keys(self, pressed, cursor):
        newly_pressed = pressed - self._previous
        self._previous = pressed
        outside_click = bool(newly_pressed & {1, 2, 4, 5, 6}) and not self.window.geometry().contains(cursor)
        switch_app = bool(newly_pressed & {91, 92}) or (9 in newly_pressed and 18 in pressed)
        if outside_click or switch_app or 27 in newly_pressed:
            self.window.hide()

    def eventFilter(self, watched, event):
        if self.window.isVisible():
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.window.hide()
                return True
            if (event.type() == QEvent.MouseButtonPress
                    and not self.window.geometry().contains(event.globalPosition().toPoint())):
                self.window.hide()
        return False

    def close(self):
        self._timer.stop()
        QGuiApplication.instance().removeEventFilter(self)
