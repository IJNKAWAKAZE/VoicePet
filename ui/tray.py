"""VoicePet 系统托盘动作与用户意图信号"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtGui import QAction, QCursor, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .qml_resources import ui_assets_root


class TrayController(QObject):
    """提供聆听、手动输入、设置和显式退出动作"""

    wake_requested = Signal()
    manual_input_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()
    open_requested = Signal()
    listen_toggle_requested = Signal()
    pet_visibility_requested = Signal()
    restore_interaction_requested = Signal()
    context_menu_requested = Signal(QPoint)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(self._icon(), self)
        menu = QMenu()
        self._menu = menu
        self._custom_menu_enabled = False
        self._tray.activated.connect(self._activated)
        self.open_action = QAction("打开 VoicePet", menu)
        self.wake_action = QAction("开始聆听", menu)
        self.manual_input_action = QAction("手动输入", menu)
        self.pet_visibility_action = QAction("显示或隐藏桌宠", menu)
        self.restore_interaction_action = QAction("恢复鼠标交互", menu)
        self.settings_action = QAction("打开设置", menu)
        self.quit_action = QAction("退出 VoicePet", menu)
        menu.addAction(self.open_action)
        menu.addAction(self.wake_action)
        menu.addAction(self.pet_visibility_action)
        menu.addAction(self.restore_interaction_action)
        menu.addAction(self.manual_input_action)
        menu.addAction(self.settings_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.wake_action.triggered.connect(self.wake_requested.emit)
        self.wake_action.triggered.connect(self.listen_toggle_requested.emit)
        self.open_action.triggered.connect(self.open_requested.emit)
        self.pet_visibility_action.triggered.connect(
            self.pet_visibility_requested.emit
        )
        self.restore_interaction_action.triggered.connect(
            self.restore_interaction_requested.emit
        )
        self.manual_input_action.triggered.connect(
            self.manual_input_requested.emit
        )
        self.settings_action.triggered.connect(self.settings_requested.emit)
        self.quit_action.triggered.connect(self.quit_requested.emit)
        self._tray.setContextMenu(menu)
        self._tray.setToolTip("VoicePet")

    def show(self) -> None:
        self._tray.show()

    def close(self) -> None:
        self._tray.hide()
        self._menu.close()

    def set_custom_menu_enabled(self, enabled: bool) -> None:
        """QML 菜单就绪后接管右键，旧窗口仍可使用原生菜单"""

        self._custom_menu_enabled = enabled
        self._tray.setContextMenu(None if enabled else self._menu)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Context and self._custom_menu_enabled:
            self.context_menu_requested.emit(QCursor.pos())

    @property
    def is_visible(self) -> bool:
        return self._tray.isVisible()

    def set_listening(self, listening: bool) -> None:
        self.wake_action.setText("停止聆听" if listening else "开始聆听")

    @staticmethod
    def _icon() -> QIcon:
        return QIcon(str(ui_assets_root() / "brand" / "voicepet.ico"))
