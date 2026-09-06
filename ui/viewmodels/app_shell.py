"""统一主窗口的顶层导航状态"""

from __future__ import annotations

from PySide6.QtCore import Property, QObject, Signal, Slot


class AppShellViewModel(QObject):
    """维护页面选择并通过信号请求窗口动作"""

    currentSectionChanged = Signal()
    showMainRequested = Signal(str)
    hideMainRequested = Signal()
    errorOccurred = Signal(str)

    _SECTIONS = frozenset({"chat", "memories", "settings"})

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current_section = "chat"

    @Property(str, notify=currentSectionChanged)
    def current_section(self) -> str:
        return self._current_section

    @Property(str, notify=currentSectionChanged)
    def currentSection(self) -> str:
        return self._current_section

    @Slot(str)
    def navigate(self, section: str) -> None:
        if section not in self._SECTIONS:
            self.errorOccurred.emit("无法打开未知页面")
            return
        if section == self._current_section:
            return
        self._current_section = section
        self.currentSectionChanged.emit()

    @Slot()
    @Slot(str)
    def show_main(self, section: str = "chat") -> None:
        if section not in self._SECTIONS:
            self.errorOccurred.emit("无法打开未知页面")
            return
        self.navigate(section)
        self.showMainRequested.emit(section)

    @Slot()
    def hide_main(self) -> None:
        self.hideMainRequested.emit()
