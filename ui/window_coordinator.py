"""QML 顶层窗口的原生行为与屏幕边界协调"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, Signal, Slot
from PySide6.QtGui import QWindow


class WindowCoordinator(QObject):
    """集中管理系统移动、最大化和可见屏幕约束"""

    errorOccurred = Signal(str)

    def __init__(
        self,
        *,
        can_recover_click_through: Callable[[], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._can_recover_click_through = can_recover_click_through or (lambda: False)

    @Slot(QWindow)
    def start_system_move(self, window: QWindow) -> None:
        if window is not None:
            window.startSystemMove()

    @Slot(QWindow)
    def toggle_maximized(self, window: QWindow) -> None:
        if window is None:
            return
        if window.visibility() == QWindow.Maximized:
            window.showNormal()
        else:
            window.showMaximized()

    @Slot(QWindow, bool, result=bool)
    def set_click_through(self, window: QWindow, enabled: bool) -> bool:
        """只在存在托盘恢复入口时启用鼠标穿透"""

        if window is None or type(enabled) is not bool:
            return False
        if enabled and not self._can_recover_click_through():
            self.errorOccurred.emit("请先启用系统托盘恢复入口")
            return False
        try:
            window.setFlag(Qt.WindowType.WindowTransparentForInput, enabled)
        except RuntimeError:
            self.errorOccurred.emit("鼠标穿透设置失败")
            return False
        return True

    @Slot(QWindow, result=bool)
    def raise_window(self, window: QWindow) -> bool:
        """把置顶窗口抬回置顶带最前面，重新显示或改标志都不会自动回到最前"""

        if window is None:
            return False
        raise_method = getattr(window, "raise_", None)
        if not callable(raise_method):
            return False
        try:
            raise_method()
        except RuntimeError:
            return False
        return True

    def clamp_rect(self, rect: QRect, screens: Sequence[QRect]) -> QRect:
        """把主窗口限制到最接近的可用屏幕内"""

        if not screens:
            return QRect(rect)
        resized = QRect(rect)
        resized.setWidth(max(820, resized.width()))
        resized.setHeight(max(560, resized.height()))
        target = max(
            screens,
            key=lambda screen: screen.intersected(resized).width()
            * screen.intersected(resized).height(),
        )
        resized.setWidth(min(resized.width(), target.width()))
        resized.setHeight(min(resized.height(), target.height()))
        maximum_x = target.right() - resized.width() + 1
        maximum_y = target.bottom() - resized.height() + 1
        resized.moveLeft(min(max(resized.left(), target.left()), maximum_x))
        resized.moveTop(min(max(resized.top(), target.top()), maximum_y))
        return resized

    def clamp_pet_position(
        self,
        position: QPoint,
        size: QSize,
        screens: Sequence[QRect],
    ) -> QPoint:
        """把桌宠完整限制在最接近的可用屏幕内"""

        if not screens or size.isEmpty():
            return QPoint(position)
        pet_rect = QRect(position, size)
        areas = [
            screen.intersected(pet_rect).width()
            * screen.intersected(pet_rect).height()
            for screen in screens
        ]
        if max(areas) > 0:
            target = screens[areas.index(max(areas))]
        else:
            target = min(
                screens,
                key=lambda screen: _distance_to_rect(position, screen),
            )
        maximum_x = max(target.left(), target.right() - size.width() + 1)
        maximum_y = max(target.top(), target.bottom() - size.height() + 1)
        return QPoint(
            min(max(position.x(), target.left()), maximum_x),
            min(max(position.y(), target.top()), maximum_y),
        )


def _distance_to_rect(point: QPoint, rect: QRect) -> int:
    delta_x = max(rect.left() - point.x(), 0, point.x() - rect.right())
    delta_y = max(rect.top() - point.y(), 0, point.y() - rect.bottom())
    return delta_x * delta_x + delta_y * delta_y
