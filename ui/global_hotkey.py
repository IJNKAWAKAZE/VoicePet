"""使用 Windows 原生全局快捷键提供唤醒降级入口"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from typing import Any, Protocol

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
HOTKEY_ID = 0x5650


class HotkeyRegistrationError(RuntimeError):
    """全局快捷键不受支持、被占用或释放失败"""

    code = "ui.global_hotkey"


class HotkeyBackend(Protocol):
    """隐藏 Qt 窗口使用的原生快捷键边界"""

    def register(self, window_id: int) -> None: ...

    def matches(self, message: object) -> bool: ...

    def unregister(self) -> None: ...


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
        self._window_id: int | None = None
        self._user32.RegisterHotKey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.UnregisterHotKey.restype = wintypes.BOOL

    def register(self, window_id: int) -> None:
        """为指定原生窗口幂等注册默认快捷键"""

        if self._window_id is not None:
            return
        modifiers = MOD_ALT | MOD_CONTROL | MOD_NOREPEAT
        if not self._user32.RegisterHotKey(
            window_id,
            HOTKEY_ID,
            modifiers,
            VK_SPACE,
        ):
            raise HotkeyRegistrationError("全局快捷键注册失败或已被占用")
        self._window_id = window_id

    def matches(self, message: object) -> bool:
        """判断原生消息是否属于当前快捷键"""

        message_id, parameter = self._message_reader(message)
        return message_id == WM_HOTKEY and parameter == HOTKEY_ID

    def unregister(self) -> None:
        """幂等释放已注册的全局快捷键"""

        window_id, self._window_id = self._window_id, None
        if window_id is None:
            return
        if not self._user32.UnregisterHotKey(window_id, HOTKEY_ID):
            raise HotkeyRegistrationError("全局快捷键释放失败")

    @staticmethod
    def _read_native_message(message: object) -> tuple[int, int]:
        native = wintypes.MSG.from_address(int(message))
        return int(native.message), int(native.wParam)


class GlobalHotkeyWidget(QWidget):
    """接收原生快捷键消息但永不显示的 Qt 窗口"""

    activated = Signal()

    def __init__(self, *, backend: HotkeyBackend | None = None) -> None:
        super().__init__()
        self._backend = backend or WindowsHotkeyBackend()
        self._released = False
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self._backend.register(int(self.winId()))

    def nativeEvent(self, event_type, message):
        if self._backend.matches(message):
            self.activated.emit()
            return True, 0
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event) -> None:
        if not self._released:
            self._released = True
            self._backend.unregister()
        super().closeEvent(event)


def create_global_hotkey() -> GlobalHotkeyWidget | None:
    """尽力创建全局快捷键且失败时保留其他激活入口"""

    try:
        return GlobalHotkeyWidget()
    except HotkeyRegistrationError:
        return None
