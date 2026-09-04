"""VoicePet 系统托盘动作与用户意图信号"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayController(QObject):
    """提供开始聆听、设置和显式退出动作"""

    wake_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(self._icon(), self)
        menu = QMenu()
        self.wake_action = QAction("开始聆听", menu)
        self.settings_action = QAction("打开设置", menu)
        self.quit_action = QAction("退出 VoicePet", menu)
        menu.addAction(self.wake_action)
        menu.addAction(self.settings_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.wake_action.triggered.connect(self.wake_requested.emit)
        self.settings_action.triggered.connect(self.settings_requested.emit)
        self.quit_action.triggered.connect(self.quit_requested.emit)
        self._tray.setContextMenu(menu)
        self._tray.setToolTip("VoicePet")

    def show(self) -> None:
        self._tray.show()

    def close(self) -> None:
        self._tray.hide()

    @staticmethod
    def _icon() -> QIcon:
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor("#081827"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#65D6D0"))
        painter.drawEllipse(5, 5, 22, 22)
        painter.drawEllipse(10, 10, 12, 12)
        painter.end()
        return QIcon(pixmap)
