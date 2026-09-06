"""使用 Windows 原生全局快捷键提供唤醒降级入口"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import Property, QObject, Qt, Signal
from PySide6.QtWidgets import QWidget

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
HOTKEY_ID = 0x5650


class HotkeyRegistrationError(RuntimeError):
    """全局快捷键不受支持、被占用或释放失败"""

    code = "ui.global_hotkey"


class HotkeyBackend(Protocol):
    """隐藏 Qt 窗口使用的原生快捷键边界"""

    def register(
        self,
        window_id: int,
        modifiers: int = MOD_ALT | MOD_CONTROL | MOD_NOREPEAT,
        virtual_key: int = VK_SPACE,
        hotkey_id: int = HOTKEY_ID,
    ) -> None: ...

    def matches(self, message: object) -> bool: ...

    def unregister(self, hotkey_id: int = HOTKEY_ID) -> None: ...


@dataclass(frozen=True, slots=True)
class HotkeySpec:
    """经过严格解析的 Windows 快捷键"""

    modifiers: int
    virtual_key: int
    normalized: str


def parse_shortcut(shortcut: str) -> HotkeySpec:
    """解析只包含安全修饰键和单个普通按键的快捷键"""

    if not isinstance(shortcut, str):
        raise HotkeyRegistrationError("快捷键必须是文本")
    parts = [part.strip() for part in shortcut.split("+") if part.strip()]
    if len(parts) < 2:
        raise HotkeyRegistrationError("快捷键必须包含修饰键")
    modifier_map = {
        "ctrl": (MOD_CONTROL, "Ctrl"),
        "control": (MOD_CONTROL, "Ctrl"),
        "alt": (MOD_ALT, "Alt"),
        "shift": (MOD_SHIFT, "Shift"),
    }
    modifiers = MOD_NOREPEAT
    normalized_modifiers: list[str] = []
    keys: list[str] = []
    for part in parts:
        modifier = modifier_map.get(part.casefold())
        if modifier is None:
            keys.append(part)
            continue
        flag, label = modifier
        if modifiers & flag:
            raise HotkeyRegistrationError("快捷键包含重复修饰键")
        modifiers |= flag
        normalized_modifiers.append(label)
    if not normalized_modifiers or len(keys) != 1:
        raise HotkeyRegistrationError("快捷键必须包含一个普通按键")
    key = keys[0].upper()
    if key == "SPACE":
        virtual_key = VK_SPACE
        key_label = "Space"
    elif len(key) == 1 and ("A" <= key <= "Z" or "0" <= key <= "9"):
        virtual_key = ord(key)
        key_label = key
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        virtual_key = 0x70 + int(key[1:]) - 1
        key_label = key
    else:
        raise HotkeyRegistrationError("快捷键按键不受支持")
    order = {"Ctrl": 0, "Alt": 1, "Shift": 2}
    normalized_modifiers.sort(key=order.__getitem__)
    return HotkeySpec(
        modifiers,
        virtual_key,
        "+".join((*normalized_modifiers, key_label)),
    )


class WindowsHotkeyBackend:
    """封装 RegisterHotKey 生命周期和 WM_HOTKEY 识别"""

    def __init__(
        self,
        *,
        user32: Any | None = None,
        message_reader: Callable[[object], tuple[int, int]] | None = None,
    ) -> None:
        if user32 is None:
            if os.name != "nt":
                raise HotkeyRegistrationError("当前系统不支持 Windows 全局快捷键")
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
            except OSError as error:
                raise HotkeyRegistrationError("Windows 快捷键 API 加载失败") from error
        self._user32 = user32
        self._message_reader = message_reader or self._read_native_message
        self._registrations: dict[int, int] = {}
        self._user32.RegisterHotKey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.UnregisterHotKey.restype = wintypes.BOOL

    def register(
        self,
        window_id: int,
        modifiers: int = MOD_ALT | MOD_CONTROL | MOD_NOREPEAT,
        virtual_key: int = VK_SPACE,
        hotkey_id: int = HOTKEY_ID,
    ) -> None:
        """为指定原生窗口幂等注册解析后的快捷键"""

        if hotkey_id in self._registrations:
            return
        if not self._user32.RegisterHotKey(
            window_id,
            hotkey_id,
            modifiers,
            virtual_key,
        ):
            raise HotkeyRegistrationError("全局快捷键注册失败或已被占用")
        self._registrations[hotkey_id] = window_id

    def matches(self, message: object) -> bool:
        """判断原生消息是否属于当前快捷键"""

        message_id, parameter = self._message_reader(message)
        return message_id == WM_HOTKEY and parameter in self._registrations

    def unregister(self, hotkey_id: int = HOTKEY_ID) -> None:
        """幂等释放已注册的全局快捷键"""

        window_id = self._registrations.pop(hotkey_id, None)
        if window_id is None:
            return
        if not self._user32.UnregisterHotKey(window_id, hotkey_id):
            raise HotkeyRegistrationError("全局快捷键释放失败")

    @staticmethod
    def _read_native_message(message: object) -> tuple[int, int]:
        native = wintypes.MSG.from_address(int(message))
        return int(native.message), int(native.wParam)


class GlobalHotkeyWidget(QWidget):
    """接收原生快捷键消息但永不显示的 Qt 窗口"""

    activated = Signal()
    currentShortcutChanged = Signal()
    registrationFailed = Signal(str)

    def __init__(self, *, backend: HotkeyBackend | None = None) -> None:
        super().__init__()
        self._backend = backend or WindowsHotkeyBackend()
        self._released = False
        self._active_id: int | None = HOTKEY_ID
        self._shortcut = "Ctrl+Alt+Space"
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self._backend.register(int(self.winId()))

    @Property(str, notify=currentShortcutChanged)
    def current_shortcut(self) -> str:
        return self._shortcut

    def try_replace(self, shortcut: str) -> None:
        """先注册候选快捷键，成功后再释放旧快捷键"""

        try:
            spec = parse_shortcut(shortcut)
        except HotkeyRegistrationError:
            self.registrationFailed.emit("快捷键格式无效")
            return
        if spec.normalized == self._shortcut:
            return
        if self._active_id is None:
            self._shortcut = spec.normalized
            self.currentShortcutChanged.emit()
            return
        candidate_id = (
            HOTKEY_ID if self._active_id == HOTKEY_ID + 1 else HOTKEY_ID + 1
        )
        try:
            self._backend.register(
                int(self.winId()),
                spec.modifiers,
                spec.virtual_key,
                candidate_id,
            )
        except HotkeyRegistrationError:
            self.registrationFailed.emit("快捷键被其他应用占用")
            return
        previous_id = self._active_id
        self._active_id = candidate_id
        self._shortcut = spec.normalized
        self._backend.unregister(previous_id)
        self.currentShortcutChanged.emit()

    def set_enabled(self, enabled: bool) -> None:
        """即时启用或停用当前全局快捷键"""

        if type(enabled) is not bool:
            raise TypeError("快捷键开关必须是布尔值")
        if enabled == (self._active_id is not None):
            return
        if not enabled:
            active_id = self._active_id
            self._active_id = None
            if active_id is not None:
                self._backend.unregister(active_id)
            return
        spec = parse_shortcut(self._shortcut)
        self._backend.register(
            int(self.winId()),
            spec.modifiers,
            spec.virtual_key,
            HOTKEY_ID,
        )
        self._active_id = HOTKEY_ID

    def nativeEvent(self, event_type, message):
        if self._backend.matches(message):
            self.activated.emit()
            return True, 0
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event) -> None:
        if not self._released:
            self._released = True
            active_id = self._active_id
            self._active_id = None
            if active_id is not None:
                self._backend.unregister(active_id)
        super().closeEvent(event)


class GlobalHotkeyService(QObject):
    """以候选 ID 注册新快捷键并在成功后释放旧快捷键"""

    currentShortcutChanged = Signal()
    registrationFailed = Signal(str)

    def __init__(
        self,
        window_id: int,
        *,
        backend: HotkeyBackend | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._window_id = window_id
        self._backend = backend or WindowsHotkeyBackend()
        self._active_id: int | None = None
        self._shortcut = ""

    @Property(str, notify=currentShortcutChanged)
    def current_shortcut(self) -> str:
        return self._shortcut

    def start(self, shortcut: str) -> None:
        if self._active_id is not None:
            return
        spec = parse_shortcut(shortcut)
        self._backend.register(
            self._window_id,
            spec.modifiers,
            spec.virtual_key,
            HOTKEY_ID,
        )
        self._active_id = HOTKEY_ID
        self._shortcut = spec.normalized
        self.currentShortcutChanged.emit()

    def try_replace(self, shortcut: str) -> None:
        try:
            spec = parse_shortcut(shortcut)
        except HotkeyRegistrationError:
            self.registrationFailed.emit("快捷键格式无效")
            return
        if spec.normalized == self._shortcut:
            return
        candidate_id = HOTKEY_ID if self._active_id == HOTKEY_ID + 1 else HOTKEY_ID + 1
        try:
            self._backend.register(
                self._window_id,
                spec.modifiers,
                spec.virtual_key,
                candidate_id,
            )
        except HotkeyRegistrationError:
            self.registrationFailed.emit("快捷键被其他应用占用")
            return
        previous_id = self._active_id
        self._active_id = candidate_id
        self._shortcut = spec.normalized
        if previous_id is not None:
            self._backend.unregister(previous_id)
        self.currentShortcutChanged.emit()

    def close(self) -> None:
        if self._active_id is not None:
            self._backend.unregister(self._active_id)
            self._active_id = None


def create_global_hotkey() -> GlobalHotkeyWidget | None:
    """尽力创建全局快捷键且失败时保留其他激活入口"""

    try:
        return GlobalHotkeyWidget()
    except HotkeyRegistrationError:
        return None
