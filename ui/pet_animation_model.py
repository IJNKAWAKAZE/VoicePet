"""向 QML 发布 Codex v2 图集帧和桌宠鼠标交互状态"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtWidgets import QApplication

from core.events import ConversationPhase

from .pet_animation import (
    CELL_HEIGHT,
    CELL_WIDTH,
    STANDARD_ANIMATION_DURATIONS,
    PetSpriteAtlas,
    animation_row_for_phase,
)
from .qml_resources import validated_asset_url


class PetAnimationModel(QObject):
    """保持帧推进规则在 Python 中并只向 QML 暴露已验证 URL"""

    frameChanged = Signal()
    phaseChanged = Signal()
    sheetChanged = Signal()

    def __init__(
        self,
        directory: str | Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        root = Path(directory).expanduser().resolve()
        self._atlas = PetSpriteAtlas.load(root)
        self._sheet_url = validated_asset_url(self._atlas.sheet_path, (root,))
        self._phase = ConversationPhase.IDLE
        self._row = 0
        self._column = 0
        self._followup_row: int | None = None

    @Property(QUrl, notify=sheetChanged)
    def sheet_url(self) -> QUrl:
        return self._sheet_url

    @Property(QUrl, notify=sheetChanged)
    def sheetUrl(self) -> QUrl:
        return self._sheet_url

    @Property(int, constant=True)
    def cell_width(self) -> int:
        return CELL_WIDTH

    @Property(int, constant=True)
    def cellWidth(self) -> int:
        return CELL_WIDTH

    @Property(int, constant=True)
    def cell_height(self) -> int:
        return CELL_HEIGHT

    @Property(int, constant=True)
    def cellHeight(self) -> int:
        return CELL_HEIGHT

    @Property(int, notify=frameChanged)
    def row(self) -> int:
        return self._row

    @Property(int, notify=frameChanged)
    def column(self) -> int:
        return self._column

    @Property(int, notify=frameChanged)
    def frame_duration(self) -> int:
        return STANDARD_ANIMATION_DURATIONS[self._row][self._column]

    @Property(int, notify=frameChanged)
    def frameDuration(self) -> int:
        return self.frame_duration

    @Property(str, notify=phaseChanged)
    def phase(self) -> str:
        return self._phase.value

    @Property(bool, constant=True)
    def using_asset(self) -> bool:
        return True

    @Property(bool, constant=True)
    def usingAsset(self) -> bool:
        return True

    def set_phase(self, phase: ConversationPhase | str) -> None:
        try:
            normalized = phase if isinstance(phase, ConversationPhase) else ConversationPhase(phase)
        except (TypeError, ValueError):
            return
        if normalized is self._phase:
            return
        self._phase = normalized
        self._row = animation_row_for_phase(normalized)
        self._column = 0
        self._followup_row = 7 if normalized is ConversationPhase.EXECUTING_TOOL else None
        self.phaseChanged.emit()
        self.frameChanged.emit()

    def load_directory(self, directory: str | Path) -> None:
        """验证新形象后原子替换当前图集"""

        root = Path(directory).expanduser().resolve()
        atlas = PetSpriteAtlas.load(root)
        sheet_url = validated_asset_url(atlas.sheet_path, (root,))
        self._atlas = atlas
        self._sheet_url = sheet_url
        self._row = animation_row_for_phase(self._phase)
        self._column = 0
        self._followup_row = (
            7 if self._phase is ConversationPhase.EXECUTING_TOOL else None
        )
        self.sheetChanged.emit()
        self.frameChanged.emit()

    @Slot()
    def advance_frame(self) -> None:
        durations = STANDARD_ANIMATION_DURATIONS[self._row]
        self._column += 1
        if self._column >= len(durations):
            self._column = 0
            if self._followup_row is not None:
                self._row = self._followup_row
                self._followup_row = None
        self.frameChanged.emit()


class PetInteractionController(QObject):
    """延迟确认单击并隔离双击、右键与拖动"""

    listenToggleRequested = Signal()
    showMainRequested = Signal()
    quickMenuRequested = Signal(float, float)
    positionChanged = Signal(float, float)
    dragFinished = Signal()

    def __init__(
        self,
        *,
        double_click_interval: int | None = None,
        drag_distance: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        hints = QApplication.styleHints()
        self._double_click_interval = (
            hints.mouseDoubleClickInterval()
            if double_click_interval is None
            else double_click_interval
        )
        self._drag_distance = (
            hints.startDragDistance() if drag_distance is None else drag_distance
        )
        self._press_x = 0.0
        self._press_y = 0.0
        self._button = 0
        self._dragging = False
        self._suppress_left_release = False
        self._single_click = QTimer(self)
        self._single_click.setSingleShot(True)
        self._single_click.timeout.connect(self.listenToggleRequested.emit)

    @Slot(float, float, int)
    def pointer_press(self, x: float, y: float, button: int) -> None:
        self._suppress_left_release = False
        self._press_x = x
        self._press_y = y
        self._button = button
        self._dragging = False

    @Slot(float, float)
    def pointer_move(self, x: float, y: float) -> None:
        if self._button != 1:
            return
        delta_x = x - self._press_x
        delta_y = y - self._press_y
        if not self._dragging and max(abs(delta_x), abs(delta_y)) < self._drag_distance:
            return
        self._dragging = True
        self._single_click.stop()
        self._press_x = x
        self._press_y = y
        self.positionChanged.emit(delta_x, delta_y)

    @Slot(float, float, int)
    def pointer_release(self, x: float, y: float, button: int) -> None:
        if button == 2:
            self._single_click.stop()
            self.quickMenuRequested.emit(x, y)
        elif button == 1 and self._suppress_left_release:
            self._suppress_left_release = False
            self._single_click.stop()
        elif button == 1 and not self._dragging:
            self._single_click.start(self._double_click_interval)
        elif button == 1 and self._dragging:
            self.dragFinished.emit()
        self._button = 0
        self._dragging = False

    @Slot()
    def pointer_cancel(self) -> None:
        self._single_click.stop()
        if self._dragging:
            self.dragFinished.emit()
        self._button = 0
        self._dragging = False

    @Slot(float, float, int)
    def double_click(self, x: float, y: float, button: int) -> None:
        del x, y
        if button != 1:
            return
        self._single_click.stop()
        self._button = 0
        self._dragging = False
        self._suppress_left_release = True
        self.showMainRequested.emit()

    @Slot(float, float)
    def context_menu(self, x: float, y: float) -> None:
        self._single_click.stop()
        self.quickMenuRequested.emit(x, y)
