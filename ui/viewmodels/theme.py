"""三套可持久化主题的语义令牌 ViewModel"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QColor

from core.config import SUPPORTED_THEME_IDS, UiConfig
from ui.qml_resources import ui_assets_root, validated_asset_url

INTERACTION_PRIMARY = {
    "sunny_sea": "#317FA8",
    "deep_night": "#69C7F2",
    "sakura_coral": "#C84E6C",
}


@dataclass(frozen=True, slots=True)
class ThemePalette:
    canvas: str
    shell_surface: str
    surface: str
    surface_alt: str
    primary: str
    accent: str
    accent_alt: str
    text: str
    text_muted: str
    border: str
    interaction_primary: str
    selected_text: str
    primary_hover: str
    primary_pressed: str
    focus: str
    danger: str
    danger_surface: str
    success: str
    warning: str
    info: str
    disabled_text: str
    inverse_text: str
    user_bubble: str
    assistant_bubble: str
    code_block: str


THEME_PALETTES = {
    "sunny_sea": ThemePalette(
        canvas="#F6FBFF",
        shell_surface="#EAF5FC",
        surface="#FFFFFF",
        surface_alt="#EDF7FC",
        primary="#4BA8D1",
        accent="#69D6C5",
        accent_alt="#A7A6E8",
        text="#24354A",
        text_muted="#687D91",
        border="#ADC8D9",
        interaction_primary=INTERACTION_PRIMARY["sunny_sea"],
        selected_text="#205B7C",
        primary_hover="#286E94",
        primary_pressed="#205B7C",
        focus="#266F98",
        danger="#B43A50",
        danger_surface="#FCECEF",
        success="#247A68",
        warning="#986515",
        info="#276F98",
        disabled_text="#91A1AF",
        inverse_text="#FFFFFF",
        user_bubble="#DDF3FA",
        assistant_bubble="#FFFFFF",
        code_block="#E7F1F6",
    ),
    "deep_night": ThemePalette(
        canvas="#111827",
        shell_surface="#172238",
        surface="#1C2940",
        surface_alt="#24334F",
        primary="#69C7F2",
        accent="#83DFCF",
        accent_alt="#A9A7F4",
        text="#F3F7FB",
        text_muted="#ABB9CA",
        border="#506887",
        interaction_primary=INTERACTION_PRIMARY["deep_night"],
        selected_text="#69C7F2",
        primary_hover="#8DD8F7",
        primary_pressed="#4EACD7",
        focus="#96E2FF",
        danger="#FF8799",
        danger_surface="#482635",
        success="#83DFCF",
        warning="#F4C66E",
        info="#8DD8F7",
        disabled_text="#718198",
        inverse_text="#102033",
        user_bubble="#244A62",
        assistant_bubble="#1C2940",
        code_block="#101A2A",
    ),
    "sakura_coral": ThemePalette(
        canvas="#FFF8F8",
        shell_surface="#FFF0F3",
        surface="#FFFFFF",
        surface_alt="#FFF0F2",
        primary="#D95E7B",
        accent="#F3A06B",
        accent_alt="#A993D6",
        text="#44313E",
        text_muted="#806B77",
        border="#D5A7B7",
        interaction_primary=INTERACTION_PRIMARY["sakura_coral"],
        selected_text="#9F3652",
        primary_hover="#B94361",
        primary_pressed="#9F3652",
        focus="#B94361",
        danger="#B62D49",
        danger_surface="#FDE9ED",
        success="#377B68",
        warning="#95601C",
        info="#6F5AA8",
        disabled_text="#A7939E",
        inverse_text="#FFFFFF",
        user_bubble="#FFE1E7",
        assistant_bubble="#FFFFFF",
        code_block="#F8EAEF",
    ),
}


class ThemeViewModel(QObject):
    """向 QML 发布主题令牌并原子保存主题偏好"""

    themeChanged = Signal()
    reduceMotionChanged = Signal()
    errorOccurred = Signal(str)

    def __init__(
        self,
        config: UiConfig,
        persist: Callable[[UiConfig], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._persist = persist

    @Property(str, notify=themeChanged)
    def current_theme_id(self) -> str:
        return self._config.theme_id

    @Property(str, notify=themeChanged)
    def currentThemeId(self) -> str:
        return self.current_theme_id

    @Property(bool, notify=reduceMotionChanged)
    def reduce_motion(self) -> bool:
        return self._config.reduce_motion

    @Property(bool, notify=reduceMotionChanged)
    def reduceMotion(self) -> bool:
        return self.reduce_motion

    @Property(int, constant=True)
    def motionFast(self) -> int:
        return 120

    @Property(int, constant=True)
    def motionStandard(self) -> int:
        return 180

    @Property(int, constant=True)
    def motionSlow(self) -> int:
        return 220

    @Slot(str)
    def set_theme(self, theme_id: str) -> None:
        if theme_id not in SUPPORTED_THEME_IDS:
            self.errorOccurred.emit("无法切换到未知主题")
            return
        if theme_id == self._config.theme_id:
            return
        candidate = replace(self._config, theme_id=theme_id)
        if not self._save(candidate):
            return
        self._config = candidate
        self.themeChanged.emit()

    @Slot(bool)
    def set_reduce_motion(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            self.errorOccurred.emit("减少动态效果设置无效")
            return
        if enabled == self._config.reduce_motion:
            return
        candidate = replace(self._config, reduce_motion=enabled)
        if not self._save(candidate):
            return
        self._config = candidate
        self.reduceMotionChanged.emit()

    def _save(self, candidate: UiConfig) -> bool:
        try:
            self._persist(candidate)
        except (OSError, RuntimeError, ValueError):
            self.errorOccurred.emit("设置保存失败，请稍后重试")
            return False
        return True

    @property
    def _palette(self) -> ThemePalette:
        return THEME_PALETTES[self._config.theme_id]

    def _color(self, name: str) -> QColor:
        return QColor(getattr(self._palette, name))

    def _asset_url(self, filename: str) -> QUrl:
        root = ui_assets_root()
        return validated_asset_url(
            root / "themes" / self.current_theme_id / filename,
            (root,),
        )

    @Property(QUrl, notify=themeChanged)
    def welcomeImage(self) -> QUrl:
        return self._asset_url("welcome.webp")

    @Property(QUrl, notify=themeChanged)
    def backgroundImage(self) -> QUrl:
        return self._asset_url("background.webp")

    @Property(QUrl, constant=True)
    def brandMark(self) -> QUrl:
        root = ui_assets_root()
        return validated_asset_url(
            root / "brand" / "voicepet-mark.svg",
            (root,),
        )

    @Property(QColor, notify=themeChanged)
    def canvas(self) -> QColor:
        return self._color("canvas")

    @Property(QColor, notify=themeChanged)
    def surface(self) -> QColor:
        return self._color("surface")

    @Property(QColor, notify=themeChanged)
    def shellSurface(self) -> QColor:
        return self._color("shell_surface")

    @Property(QColor, notify=themeChanged)
    def surface_alt(self) -> QColor:
        return self._color("surface_alt")

    @Property(QColor, notify=themeChanged)
    def surfaceAlt(self) -> QColor:
        return self.surface_alt

    @Property(QColor, notify=themeChanged)
    def primary(self) -> QColor:
        return self._color("primary")

    @Property(QColor, notify=themeChanged)
    def accent(self) -> QColor:
        return self._color("accent")

    @Property(QColor, notify=themeChanged)
    def accent_alt(self) -> QColor:
        return self._color("accent_alt")

    @Property(QColor, notify=themeChanged)
    def accentAlt(self) -> QColor:
        return self.accent_alt

    @Property(QColor, notify=themeChanged)
    def text(self) -> QColor:
        return self._color("text")

    @Property(QColor, notify=themeChanged)
    def text_muted(self) -> QColor:
        return self._color("text_muted")

    @Property(QColor, notify=themeChanged)
    def textMuted(self) -> QColor:
        return self.text_muted

    @Property(QColor, notify=themeChanged)
    def border(self) -> QColor:
        return self._color("border")

    @Property(QColor, notify=themeChanged)
    def interaction_primary(self) -> QColor:
        return self._color("interaction_primary")

    @Property(QColor, notify=themeChanged)
    def interactionPrimary(self) -> QColor:
        return self.interaction_primary

    @Property(QColor, notify=themeChanged)
    def selectedText(self) -> QColor:
        return self._color("selected_text")

    @Property(QColor, notify=themeChanged)
    def primaryHover(self) -> QColor:
        return self._color("primary_hover")

    @Property(QColor, notify=themeChanged)
    def primaryPressed(self) -> QColor:
        return self._color("primary_pressed")

    @Property(QColor, notify=themeChanged)
    def focus(self) -> QColor:
        return self._color("focus")

    @Property(QColor, notify=themeChanged)
    def danger(self) -> QColor:
        return self._color("danger")

    @Property(QColor, notify=themeChanged)
    def dangerSurface(self) -> QColor:
        return self._color("danger_surface")

    @Property(QColor, notify=themeChanged)
    def success(self) -> QColor:
        return self._color("success")

    @Property(QColor, notify=themeChanged)
    def warning(self) -> QColor:
        return self._color("warning")

    @Property(QColor, notify=themeChanged)
    def info(self) -> QColor:
        return self._color("info")

    @Property(QColor, notify=themeChanged)
    def disabledText(self) -> QColor:
        return self._color("disabled_text")

    @Property(QColor, notify=themeChanged)
    def inverseText(self) -> QColor:
        return self._color("inverse_text")

    @Property(QColor, notify=themeChanged)
    def userBubble(self) -> QColor:
        return self._color("user_bubble")

    @Property(QColor, notify=themeChanged)
    def assistantBubble(self) -> QColor:
        return self._color("assistant_bubble")

    @Property(QColor, notify=themeChanged)
    def codeBlock(self) -> QColor:
        return self._color("code_block")
