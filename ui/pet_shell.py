"""透明桌宠容器、声呐状态与安全文本气泡"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QTextDocument
from PySide6.QtWidgets import QSizePolicy, QTextBrowser, QVBoxLayout, QWidget

from core.events import ConversationPhase

from .pet_animation import PetAnimationWidget, PetAssetError, PetSpriteAtlas

_PET_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MAX_MANIFEST_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class PetChoice:
    """设置页可选择的已验证桌宠形象"""

    pet_id: str
    display_name: str
    directory: Path
    built_in: bool

    @property
    def label(self) -> str:
        return f"{self.display_name}（{self.pet_id}）"


def discover_pet_choices(
    data_root: str | Path,
    *,
    built_in_root: str | Path | None = None,
) -> tuple[PetChoice, ...]:
    """从内置与用户目录读取安全的 v2 桌宠候选"""

    source_roots = (
        (
            Path(built_in_root).expanduser().resolve()
            if built_in_root is not None
            else default_pet_directory().resolve().parent
        ),
        Path(data_root).expanduser().resolve() / "pets",
    )
    choices: list[PetChoice] = []
    seen_ids: set[str] = set()
    for built_in, source_root in ((True, source_roots[0]), (False, source_roots[1])):
        discovered: list[PetChoice] = []
        try:
            directories = tuple(path for path in source_root.iterdir() if path.is_dir())
        except OSError:
            continue
        for directory in directories:
            choice = _read_pet_choice(directory, built_in=built_in)
            if choice is None or choice.pet_id in seen_ids:
                continue
            seen_ids.add(choice.pet_id)
            discovered.append(choice)
        choices.extend(
            sorted(
                discovered,
                key=lambda choice: (
                    choice.display_name.casefold(),
                    choice.pet_id,
                ),
            )
        )
    return tuple(choices)


def _read_pet_choice(directory: Path, *, built_in: bool) -> PetChoice | None:
    root = directory.resolve()
    manifest_path = root / "pet.json"
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            return None
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pet_id = data.get("id")
    display_name = data.get("displayName")
    sprite_path = data.get("spritesheetPath")
    if (
        not isinstance(pet_id, str)
        or _PET_ID_PATTERN.fullmatch(pet_id) is None
        or root.name != pet_id
        or not isinstance(display_name, str)
        or not display_name.strip()
        or not isinstance(sprite_path, str)
        or not sprite_path.strip()
        or data.get("spriteVersionNumber") != 2
    ):
        return None
    sheet = (root / sprite_path).resolve()
    try:
        sheet.relative_to(root)
    except ValueError:
        return None
    if not sheet.is_file():
        return None
    return PetChoice(pet_id, display_name.strip(), root, built_in)


def clamp_window_top_left(
    proposed: QPoint,
    window_size: QSize,
    screen_geometries: Sequence[QRect],
) -> QPoint:
    """把窗口左上角限制到最近屏幕的设备无关可用区域"""

    if not screen_geometries:
        return QPoint(proposed)

    def distance_squared(geometry: QRect) -> int:
        nearest_x = min(max(proposed.x(), geometry.left()), geometry.right())
        nearest_y = min(max(proposed.y(), geometry.top()), geometry.bottom())
        return (proposed.x() - nearest_x) ** 2 + (proposed.y() - nearest_y) ** 2

    target = min(screen_geometries, key=distance_squared)
    maximum_x = target.left() + max(0, target.width() - window_size.width())
    maximum_y = target.top() + max(0, target.height() - window_size.height())
    return QPoint(
        min(max(proposed.x(), target.left()), maximum_x),
        min(max(proposed.y(), target.top()), maximum_y),
    )


def default_pet_directory() -> Path:
    """返回源码或冻结包中的内置桌宠目录"""

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parents[1]))
    return bundle_root / "assets" / "pet" / "dpsk-girl"


def resolve_active_pet_directory(
    active_skin: str,
    data_root: str | Path,
) -> Path:
    """优先选择用户形象并在名称异常或缺失时回退内置形象"""

    fallback = default_pet_directory().resolve()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", active_skin) is None:
        return fallback
    user_root = Path(data_root).expanduser().resolve() / "pets"
    user_pet = (user_root / active_skin).resolve()
    if user_pet.parent == user_root and user_pet.is_dir():
        return user_pet
    built_in = (fallback.parent / active_skin).resolve()
    if built_in.parent == fallback.parent and built_in.is_dir():
        return built_in
    return fallback


class ConversationBubble(QTextBrowser):
    """安全渲染 Markdown 并限制可视高度的对话气泡"""

    hover_changed = Signal(bool)

    _WIDTH = 240
    _MINIMUM_HEIGHT = 44
    _MAXIMUM_HEIGHT = 260

    def __init__(self, *, max_chars: int = 400, parent: QWidget | None = None) -> None:
        if max_chars <= 0:
            raise ValueError("气泡文本上限必须大于零")
        super().__init__(parent)
        self._max_chars = max_chars
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFixedWidth(self._WIDTH)
        self.setMinimumHeight(self._MINIMUM_HEIGHT)
        self.setMaximumHeight(self._MAXIMUM_HEIGHT)
        self.document().setDocumentMargin(2)
        self.setStyleSheet(
            "QTextBrowser{background:#102B3F;color:#EAF6F5;"
            "border:1px solid #65D6D0;border-radius:10px;padding:10px;"
            "font-family:'Microsoft YaHei UI';}"
            "QScrollBar:vertical{background:transparent;width:7px;margin:6px 1px;}"
            "QScrollBar::handle:vertical{background:#3B687C;border:0;"
            "border-radius:3px;min-height:28px;}"
            "QScrollBar::handle:vertical:hover{background:#65D6D0;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{"
            "background:transparent;}"
        )

    def set_message(self, text: str) -> None:
        normalized = text.strip()
        if len(normalized) > self._max_chars:
            normalized = normalized[: self._max_chars] + "…"
        features = (
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML
        )
        self.document().setMarkdown(normalized, features)
        self.document().setTextWidth(max(1, self.viewport().width()))
        desired_height = ceil(self.document().size().height()) + 22
        self.setFixedHeight(
            min(
                self._MAXIMUM_HEIGHT,
                max(self._MINIMUM_HEIGHT, desired_height),
            )
        )
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def text(self) -> str:
        """返回去除 Markdown 标记后的可读文本"""

        return self.toPlainText()

    def loadResource(self, resource_type, name):
        """阻止 Markdown 内容读取本地或远程图片"""

        if resource_type == QTextDocument.ResourceType.ImageResource:
            return None
        return super().loadResource(resource_type, name)

    def enterEvent(self, event) -> None:
        self.hover_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_changed.emit(False)
        super().leaveEvent(event)


class SonarStatusWidget(QWidget):
    """以克制的同心声呐环表示桌宠当前活动状态"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(180, 180)

    def paintEvent(self, event) -> None:
        _ = event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#65D6D0"), 2))
        center = self.rect().center()
        for radius, alpha in ((72, 55), (52, 95), (30, 180)):
            color = QColor("#65D6D0")
            color.setAlpha(alpha)
            painter.setPen(QPen(color, 2))
            painter.drawEllipse(center, radius, radius)


class PetShellWindow(QWidget):
    """承载桌宠渲染器和气泡的透明置顶窗口"""

    activation_requested = Signal()

    def __init__(
        self,
        *,
        always_on_top: bool = True,
        pet_directory: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._drag_offset: QPoint | None = None
        self._press_global_position: QPoint | None = None
        self._dragged = False
        self._observed_screens: set[int] = set()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.bubble = ConversationBubble(parent=self)
        self._message_hide_timer = QTimer(self)
        self._message_hide_timer.setSingleShot(True)
        self._message_hide_timer.timeout.connect(self._hide_message)
        self._message_hide_remaining_ms: int | None = None
        self.bubble.hover_changed.connect(self._bubble_hover_changed)
        self.pet_animation: PetAnimationWidget | None = None
        self.sonar: SonarStatusWidget | None = None
        try:
            atlas = PetSpriteAtlas.load(pet_directory or default_pet_directory())
        except PetAssetError:
            self.sonar = SonarStatusWidget(self)
            renderer: QWidget = self.sonar
        else:
            self.pet_animation = PetAnimationWidget(atlas, self)
            renderer = self.pet_animation
        renderer.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self._renderer = renderer
        self._layout = QVBoxLayout(self)
        self._layout.addWidget(self.bubble)
        self._layout.addWidget(renderer, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.bubble.hide()
        application = QGuiApplication.instance()
        if application is not None:
            application.screenAdded.connect(self._screen_added)
            application.screenRemoved.connect(self._screen_removed)
            application.primaryScreenChanged.connect(
                self._screen_configuration_changed
            )
            for screen in QGuiApplication.screens():
                self._observe_screen(screen)

    @property
    def using_pet_asset(self) -> bool:
        return self.pet_animation is not None

    def set_phase(self, phase: ConversationPhase) -> None:
        if self.pet_animation is not None:
            self.pet_animation.set_phase(phase)

    def show_message(
        self,
        text: str,
        screen_geometries: Sequence[QRect] | None = None,
    ) -> None:
        """显示消息并向上扩展窗口以保持桌宠底部位置"""

        self.cancel_message_hide()
        was_visible = self.isVisible()
        previous_bottom = self.frameGeometry().bottom()
        self.bubble.set_message(text)
        self.bubble.show()
        self._layout.invalidate()
        self._layout.activate()
        self.adjustSize()
        if was_visible:
            self.move(self.x(), previous_bottom - self.height() + 1)
        self.ensure_visible(screen_geometries)

    def schedule_message_hide(self, delay_ms: int = 8000) -> None:
        """安排当前气泡在指定延迟后隐藏"""

        if delay_ms <= 0:
            raise ValueError("气泡隐藏延迟必须大于零")
        self._message_hide_remaining_ms = delay_ms
        self._message_hide_timer.start(delay_ms)

    def cancel_message_hide(self) -> None:
        """取消当前气泡隐藏计划"""

        self._message_hide_timer.stop()
        self._message_hide_remaining_ms = None

    def _bubble_hover_changed(self, hovered: bool) -> None:
        if hovered:
            if self._message_hide_timer.isActive():
                self._message_hide_remaining_ms = max(
                    1,
                    self._message_hide_timer.remainingTime(),
                )
                self._message_hide_timer.stop()
            return
        if (
            self._message_hide_remaining_ms is not None
            and self.bubble.isVisible()
            and not self._message_hide_timer.isActive()
        ):
            self._message_hide_timer.start(self._message_hide_remaining_ms)

    def _hide_message(self) -> None:
        self._message_hide_remaining_ms = None
        if not self.bubble.isVisible():
            return
        was_visible = self.isVisible()
        previous_bottom = self.frameGeometry().bottom()
        self.bubble.hide()
        self._layout.invalidate()
        self._layout.activate()
        self.adjustSize()
        if was_visible:
            self.move(self.x(), previous_bottom - self.height() + 1)

    def load_pet_directory(self, directory: str | Path) -> bool:
        """完整加载新图集成功后才替换当前渲染器"""

        try:
            atlas = PetSpriteAtlas.load(directory)
        except PetAssetError:
            return False
        replacement = PetAnimationWidget(atlas, self)
        replacement.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self._layout.replaceWidget(self._renderer, replacement)
        self._renderer.hide()
        self._renderer.deleteLater()
        self._renderer = replacement
        self.pet_animation = replacement
        self.sonar = None
        replacement.show()
        return True

    def set_always_on_top(self, enabled: bool) -> None:
        """应用置顶设置并保持窗口原有可见状态"""

        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        if visible:
            self.show()

    def move_within_screens(
        self,
        proposed: QPoint,
        screen_geometries: Sequence[QRect] | None = None,
    ) -> None:
        """在多屏可用区域内移动且不手动换算设备像素比"""

        geometries = (
            tuple(screen.availableGeometry() for screen in QGuiApplication.screens())
            if screen_geometries is None
            else tuple(screen_geometries)
        )
        self.move(clamp_window_top_left(proposed, self.size(), geometries))

    def ensure_visible(
        self,
        screen_geometries: Sequence[QRect] | None = None,
    ) -> None:
        """在屏幕布局变化后把窗口重新放回可用区域"""

        self.move_within_screens(self.pos(), screen_geometries)

    def _observe_screen(self, screen) -> None:
        identity = id(screen)
        if identity in self._observed_screens:
            return
        self._observed_screens.add(identity)
        screen.availableGeometryChanged.connect(self._screen_configuration_changed)
        screen.geometryChanged.connect(self._screen_configuration_changed)
        screen.logicalDotsPerInchChanged.connect(self._screen_configuration_changed)

    def _screen_added(self, screen) -> None:
        self._observe_screen(screen)
        self._screen_configuration_changed()

    def _screen_removed(self, screen) -> None:
        self._observed_screens.discard(id(screen))
        self._screen_configuration_changed()

    def _screen_configuration_changed(self, value=None) -> None:
        del value
        QTimer.singleShot(0, self.ensure_visible)

    def mousePressEvent(self, event) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            self._press_global_position = event.globalPosition().toPoint()
            self._dragged = False
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            current = event.globalPosition().toPoint()
            if self._press_global_position is not None:
                distance = (current - self._press_global_position).manhattanLength()
                if distance >= QGuiApplication.styleHints().startDragDistance():
                    self._dragged = True
            proposed = event.globalPosition().toPoint() - self._drag_offset
            self.move_within_screens(proposed)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            activate = not self._dragged
            self._drag_offset = None
            self._press_global_position = None
            self._dragged = False
            event.accept()
            if activate:
                self.activation_requested.emit()
            return
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:
        self.cancel_message_hide()
        super().closeEvent(event)
