"""Windows 桌面自动化原语：窗口枚举、窗口截图和鼠标键盘注入。

MCP 工具层只做参数校验和结果封装，真实副作用集中在这个模块，便于单独测试。
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass
from typing import Any

IS_WINDOWS = os.name == "nt"
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
WHEEL_DELTA = 120
PW_RENDERFULLCONTENT = 0x00000002
DWMWA_CLOAKED = 14
SW_RESTORE = 9
SRCCOPY = 0x00CC0020
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
CLIPBOARD_RETRY_ATTEMPTS = 10
CLIPBOARD_RETRY_SECONDS = 0.02


class ComputerUseError(RuntimeError):
    """桌面自动化不可用或参数无效"""


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_ulong),
        ("biSizeImage", ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_ulong),
        ("biClrImportant", ctypes.c_ulong),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_ulong * 3)]


_LIBRARIES: dict[str, Any] = {}
_DPI_READY = False


def _library(name: str) -> Any:
    if not IS_WINDOWS:
        raise ComputerUseError("桌面自动化仅支持 Windows")
    library = _LIBRARIES.get(name)
    if library is None:
        library = ctypes.WinDLL(name, use_last_error=True)
        _LIBRARIES[name] = library
    return library


def _user32() -> Any:
    library = _library("user32")
    if not getattr(library, "_voicepet_ready", False):
        library.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
        library.SendInput.restype = ctypes.c_uint
        library.GetDC.restype = ctypes.c_void_p
        library.GetWindowDC.restype = ctypes.c_void_p
        library.GetForegroundWindow.restype = ctypes.c_void_p
        library.GetSystemMetrics.argtypes = (ctypes.c_int,)
        library.GetSystemMetrics.restype = ctypes.c_int
        library.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(_RECT))
        library.GetWindowRect.restype = ctypes.c_bool
        library.PrintWindow.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        library.PrintWindow.restype = ctypes.c_bool
        library.GetWindowTextLengthW.argtypes = (ctypes.c_void_p,)
        library.GetWindowTextLengthW.restype = ctypes.c_int
        library.GetWindowTextW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
        library.GetWindowTextW.restype = ctypes.c_int
        library.IsWindow.argtypes = (ctypes.c_void_p,)
        library.IsWindow.restype = ctypes.c_bool
        library.IsWindowVisible.argtypes = (ctypes.c_void_p,)
        library.IsWindowVisible.restype = ctypes.c_bool
        library.IsIconic.argtypes = (ctypes.c_void_p,)
        library.IsIconic.restype = ctypes.c_bool
        library.EnumWindows.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        library.EnumWindows.restype = ctypes.c_bool
        library.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        library.SetForegroundWindow.restype = ctypes.c_bool
        library.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
        library.ShowWindow.restype = ctypes.c_bool
        library.LockWorkStation.argtypes = ()
        library.LockWorkStation.restype = ctypes.c_bool
        library.PrintWindow.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        library.PrintWindow.restype = ctypes.c_bool
        library.AttachThreadInput.argtypes = (ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool)
        library.AttachThreadInput.restype = ctypes.c_bool
        library.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        library.GetWindowThreadProcessId.restype = ctypes.c_ulong
        library.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        library.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        library.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        library.ReleaseDC.restype = ctypes.c_int
        library.OpenClipboard.argtypes = (ctypes.c_void_p,)
        library.OpenClipboard.restype = ctypes.c_bool
        library.CloseClipboard.argtypes = ()
        library.CloseClipboard.restype = ctypes.c_bool
        library.EmptyClipboard.argtypes = ()
        library.EmptyClipboard.restype = ctypes.c_bool
        library.IsClipboardFormatAvailable.argtypes = (ctypes.c_uint,)
        library.IsClipboardFormatAvailable.restype = ctypes.c_bool
        library.GetClipboardData.argtypes = (ctypes.c_uint,)
        library.GetClipboardData.restype = ctypes.c_void_p
        library.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
        library.SetClipboardData.restype = ctypes.c_void_p
        library._voicepet_ready = True
    return library


def _kernel32() -> Any:
    library = _library("kernel32")
    if not getattr(library, "_voicepet_ready", False):
        library.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
        library.GlobalAlloc.restype = ctypes.c_void_p
        library.GlobalLock.argtypes = (ctypes.c_void_p,)
        library.GlobalLock.restype = ctypes.c_void_p
        library.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        library.GlobalUnlock.restype = ctypes.c_bool
        library.GlobalFree.argtypes = (ctypes.c_void_p,)
        library.GlobalFree.restype = ctypes.c_void_p
        library.SetSystemPowerState.argtypes = (ctypes.c_bool, ctypes.c_bool)
        library.SetSystemPowerState.restype = ctypes.c_bool
        library._voicepet_ready = True
    return library


def _gdi32() -> Any:
    library = _library("gdi32")
    if not getattr(library, "_voicepet_ready", False):
        library.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
        library.CreateCompatibleDC.restype = ctypes.c_void_p
        library.CreateCompatibleBitmap.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
        library.CreateCompatibleBitmap.restype = ctypes.c_void_p
        library.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        library.SelectObject.restype = ctypes.c_void_p
        library.DeleteObject.argtypes = (ctypes.c_void_p,)
        library.DeleteDC.argtypes = (ctypes.c_void_p,)
        library.BitBlt.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
        )
        library.BitBlt.restype = ctypes.c_bool
        library.GetDIBits.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint,
        )
        library.GetDIBits.restype = ctypes.c_int
        library._voicepet_ready = True
    return library


def _dwmapi() -> Any:
    library = _library("dwmapi")
    if not getattr(library, "_voicepet_ready", False):
        library.DwmGetWindowAttribute.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint)
        library.DwmGetWindowAttribute.restype = ctypes.c_long
        library._voicepet_ready = True
    return library


def _current_thread_id() -> int:
    return int(_library("kernel32").GetCurrentThreadId())


def ensure_dpi_awareness() -> None:
    """让窗口坐标和截图都使用物理像素，避免多屏缩放下的偏移"""
    global _DPI_READY
    if _DPI_READY or not IS_WINDOWS:
        return
    _DPI_READY = True
    user32 = _user32()
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return
    except (AttributeError, OSError):
        pass
    try:
        _library("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError, ComputerUseError):
        pass


def window_usable(
    *,
    title: str,
    visible: bool,
    cloaked: bool,
    width: int,
    height: int,
    minimized: bool,
    include_minimized: bool,
) -> bool:
    """过滤隐藏窗口、UWP 幽灵窗口、零尺寸窗口和无标题窗口"""
    if not visible or cloaked or minimized and not include_minimized:
        return False
    if not title.strip():
        return False
    return width > 0 and height > 0


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    title: str
    process: str
    left: int
    top: int
    width: int
    height: int
    minimized: bool
    foreground: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "title": self.title,
            "process": self.process,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "minimized": self.minimized,
            "foreground": self.foreground,
        }


def _is_cloaked(handle: int) -> bool:
    try:
        value = ctypes.c_int(0)
        result = _dwmapi().DwmGetWindowAttribute(
            ctypes.c_void_p(handle), ctypes.c_uint(DWMWA_CLOAKED), ctypes.byref(value), ctypes.sizeof(value)
        )
    except (AttributeError, OSError, ComputerUseError):
        return False
    return result == 0 and value.value != 0


def _process_name(handle: int) -> str:
    pid = ctypes.c_ulong(0)
    _user32().GetWindowThreadProcessId(ctypes.c_void_p(handle), ctypes.byref(pid))
    try:
        import psutil
        return psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001 进程名只用于展示，取不到时留空
        return ""


def window_rect(handle: int) -> tuple[int, int, int, int]:
    """返回窗口的 left、top、width、height（物理像素）"""
    rect = _RECT()
    if not _user32().GetWindowRect(ctypes.c_void_p(handle), ctypes.byref(rect)):
        raise ComputerUseError("无法读取窗口位置")
    return int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top)


def foreground_window() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def list_windows(*, include_minimized: bool = False, limit: int = 60) -> list[WindowInfo]:
    """按 Z 序枚举桌面窗口，前台窗口排在最前面"""
    ensure_dpi_awareness()
    user32 = _user32()
    foreground = foreground_window()
    found: list[WindowInfo] = []

    def visit(handle: int, _param: int) -> bool:
        try:
            length = user32.GetWindowTextLengthW(handle)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, length + 1)
            left, top, width, height = window_rect(handle)
            minimized = bool(user32.IsIconic(handle))
            usable = window_usable(
                title=buffer.value,
                visible=bool(user32.IsWindowVisible(handle)),
                cloaked=_is_cloaked(handle),
                width=width,
                height=height,
                minimized=minimized,
                include_minimized=include_minimized,
            )
            if usable:
                found.append(
                    WindowInfo(int(handle), buffer.value.strip(), _process_name(handle), left, top, width, height, minimized, int(handle) == foreground)
                )
        except (ComputerUseError, OSError, ValueError):
            # 单个窗口读不到信息时跳过，不能让枚举整体中断
            return True
        return len(found) < limit

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(visit)
    user32.EnumWindows(callback, None)
    return found


def resolve_window(reference: object = None) -> int:
    """把工具参数换成窗口句柄；0 表示整个虚拟屏幕"""
    ensure_dpi_awareness()
    user32 = _user32()
    if reference is None or reference == "":
        return foreground_window()
    if isinstance(reference, bool):
        raise ComputerUseError("窗口参数无效")
    if isinstance(reference, int) or isinstance(reference, str) and reference.strip().isdigit():
        handle = int(reference)
        if handle == 0 or user32.IsWindow(ctypes.c_void_p(handle)):
            return handle
        raise ComputerUseError(f"窗口句柄不存在：{handle}")
    if not isinstance(reference, str):
        raise ComputerUseError("窗口参数无效")
    text = reference.strip().casefold()
    if text in {"screen", "desktop", "screenshot", "屏幕", "整个屏幕", "全屏"}:
        return 0
    if text in {"active", "foreground", "current", "当前窗口", "活动窗口", "前台窗口"}:
        return foreground_window()
    matches = [item for item in list_windows(include_minimized=True, limit=200) if text in item.title.casefold()]
    if not matches:
        raise ComputerUseError(f"找不到标题包含“{reference}”的窗口")
    for item in matches:
        if item.foreground:
            return item.handle
    return matches[0].handle


def _send_foreground(user32: Any, handle: int) -> None:
    """直接请求系统把窗口切到前台"""
    user32.SetForegroundWindow(ctypes.c_void_p(handle))


def _send_foreground_with_attach(user32: Any, handle: int) -> None:
    """系统只允许前台线程或与其共享输入队列的线程调用 SetForegroundWindow"""
    caller = _current_thread_id()
    foreground_thread = _window_thread(foreground_window())
    attached = False
    if foreground_thread and foreground_thread != caller:
        attached = bool(user32.AttachThreadInput(caller, foreground_thread, True))
    try:
        user32.SetForegroundWindow(ctypes.c_void_p(handle))
    finally:
        if attached:
            user32.AttachThreadInput(caller, foreground_thread, False)


def focus_window(handle: int) -> None:
    """把窗口带到前台；最小化的窗口先恢复，切换被系统拒绝时抛出 ComputerUseError"""
    user32 = _user32()
    if handle == 0:
        return
    if not user32.IsWindow(ctypes.c_void_p(handle)):
        raise ComputerUseError("窗口不存在")
    if user32.IsIconic(ctypes.c_void_p(handle)):
        user32.ShowWindow(ctypes.c_void_p(handle), SW_RESTORE)
        time.sleep(0.2)
    if foreground_window() == handle:
        return
    _send_foreground(user32, handle)
    if foreground_window() != handle:
        _send_foreground_with_attach(user32, handle)
    if foreground_window() != handle:
        raise ComputerUseError("系统拒绝了窗口切换，请先手动点一下目标窗口再试")


def _window_thread(handle: int) -> int:
    if handle == 0:
        return 0
    return int(_user32().GetWindowThreadProcessId(ctypes.c_void_p(handle), None))


def normalize_absolute(x: int, y: int, *, left: int, top: int, width: int, height: int) -> tuple[int, int]:
    """把屏幕坐标换算成 SendInput 需要的 0-65535 归一化坐标"""
    if width <= 1 or height <= 1:
        raise ComputerUseError("虚拟屏幕尺寸无效")
    return round((x - left) * 65535 / (width - 1)), round((y - top) * 65535 / (height - 1))


def screen_point(handle: int | None, x: int, y: int) -> tuple[int, int]:
    """窗口句柄非空时把窗口相对坐标换算成屏幕坐标"""
    if not handle:
        return int(x), int(y)
    left, top, _width, _height = window_rect(handle)
    return left + int(x), top + int(y)


def bgra_to_rgb(width: int, height: int, pixels: bytes | bytearray | memoryview) -> bytes:
    """把 GDI 输出的 BGRA 像素转成 PNG 需要的 RGB 行数据"""
    view = memoryview(pixels)
    if view.nbytes != width * height * 4:
        raise ComputerUseError("像素数据尺寸不匹配")
    rgb = bytearray(width * height * 3)
    rgb[0::3] = view[2::4]
    rgb[1::3] = view[1::4]
    rgb[2::3] = view[0::4]
    return bytes(rgb)


def downscale_rgb(width: int, height: int, rgb: bytes, step: int) -> tuple[int, int, bytes]:
    """按整数步长抽样缩小图片，避免缩放带来的额外依赖"""
    if step <= 1:
        return width, height, rgb
    data = rgb if isinstance(rgb, bytes) else bytes(rgb)
    stride = width * 3
    scaled_width = len(range(0, width, step))
    result = bytearray()
    for y in range(0, height, step):
        row = data[y * stride : (y + 1) * stride]
        pixels = bytearray(scaled_width * 3)
        pixels[0::3] = row[0::3][0::step]
        pixels[1::3] = row[1::3][0::step]
        pixels[2::3] = row[2::3][0::step]
        result += pixels
    return scaled_width, len(range(0, height, step)), bytes(result)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def encode_png_rgb(width: int, height: int, rgb: bytes | memoryview) -> bytes:
    """把 RGB 行数据编码成 PNG，避免为了截图引入图像库依赖"""
    if width <= 0 or height <= 0:
        raise ComputerUseError("图像尺寸无效")
    data = rgb if isinstance(rgb, bytes) else bytes(rgb)
    stride = width * 3
    if len(data) != stride * height:
        raise ComputerUseError("像素数据尺寸不匹配")
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        rows += data[y * stride : (y + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _chunk(b"IEND", b"")
    )


def encode_png(width: int, height: int, bgra: bytes | bytearray | memoryview) -> bytes:
    return encode_png_rgb(width, height, bgra_to_rgb(width, height, bgra))


@dataclass(frozen=True, slots=True)
class Capture:
    png: bytes
    width: int
    height: int
    step: int


def _grab(handle: int) -> tuple[int, int, bytes]:
    user32, gdi32 = _user32(), _gdi32()
    if handle == 0:
        left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        source = user32.GetDC(None)
    else:
        if not user32.IsWindow(ctypes.c_void_p(handle)):
            raise ComputerUseError("窗口不存在")
        if user32.IsIconic(ctypes.c_void_p(handle)):
            raise ComputerUseError("窗口已最小化，请先恢复窗口再截图")
        left, top, width, height = window_rect(handle)
        source = user32.GetWindowDC(ctypes.c_void_p(handle))
    if not source:
        raise ComputerUseError("无法获取窗口绘图上下文")
    memory = gdi32.CreateCompatibleDC(ctypes.c_void_p(source))
    bitmap = gdi32.CreateCompatibleBitmap(ctypes.c_void_p(source), width, height)
    if not memory or not bitmap:
        raise ComputerUseError("无法创建截图缓冲区")
    previous = gdi32.SelectObject(ctypes.c_void_p(memory), ctypes.c_void_p(bitmap))
    try:
        if handle == 0:
            captured = gdi32.BitBlt(ctypes.c_void_p(memory), 0, 0, width, height, ctypes.c_void_p(source), left, top, SRCCOPY)
        elif user32.PrintWindow(ctypes.c_void_p(handle), ctypes.c_void_p(memory), PW_RENDERFULLCONTENT):
            captured = True
        else:
            # 部分应用不支持 PrintWindow，退回屏幕拷贝（此时被遮挡区域会失真）
            captured = gdi32.BitBlt(ctypes.c_void_p(memory), 0, 0, width, height, ctypes.c_void_p(source), 0, 0, SRCCOPY)
        if not captured:
            raise ComputerUseError("整屏截图失败，请确认桌面会话处于活动状态" if handle == 0 else "窗口截图失败，请先激活目标窗口")
        return width, height, _bitmap_bytes(memory, bitmap, width, height)
    finally:
        gdi32.SelectObject(ctypes.c_void_p(memory), ctypes.c_void_p(previous))
        gdi32.DeleteObject(ctypes.c_void_p(bitmap))
        gdi32.DeleteDC(ctypes.c_void_p(memory))
        user32.ReleaseDC(None, ctypes.c_void_p(source))


def _bitmap_bytes(memory: int, bitmap: int, width: int, height: int) -> bytes:
    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # 负高度表示自上而下存储
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    copied = _gdi32().GetDIBits(
        ctypes.c_void_p(memory), ctypes.c_void_p(bitmap), 0, height, buffer, ctypes.byref(info), 0
    )
    if not copied:
        raise ComputerUseError("读取窗口位图失败")
    return buffer.raw


def capture_window(handle: int, *, max_width: int = 0) -> Capture:
    """截取窗口或整屏，max_width 大于 0 时按整数步长缩小"""
    ensure_dpi_awareness()
    width, height, bgra = _grab(handle)
    step = 1
    if max_width > 0 and width > max_width:
        step = (width + max_width - 1) // max_width
    scaled_width, scaled_height, rgb = downscale_rgb(width, height, bgra_to_rgb(width, height, bgra), step)
    return Capture(encode_png_rgb(scaled_width, scaled_height, rgb), scaled_width, scaled_height, step)


def _mouse_input(flags: int, x: int = 0, y: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, 0)))


def _key_input(key: int, flags: int = 0, *, scan: int = 0) -> _INPUT:
    # 虚拟键码注入必须让系统自己映射扫描码，否则中文键盘布局下会打出别的字符
    if flags & KEYEVENTF_UNICODE:
        return _INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(0, scan, flags, 0, 0)))
    return _INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(key, 0, flags, 0, 0)))


def _send(inputs: list[_INPUT]) -> None:
    if not inputs:
        return
    array = (_INPUT * len(inputs))(*inputs)
    sent = _user32().SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise ComputerUseError("输入注入被系统拒绝，请确认前台窗口权限一致")


def _absolute_point(x: int, y: int) -> tuple[int, int]:
    user32 = _user32()
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return normalize_absolute(x, y, left=left, top=top, width=width, height=height)


def _move_input(x: int, y: int) -> _INPUT:
    normalized_x, normalized_y = _absolute_point(x, y)
    return _mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, normalized_x, normalized_y)


MOUSE_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(
    x: int,
    y: int,
    *,
    window: object = None,
    button: str = "left",
    clicks: int = 1,
    activate: bool = True,
) -> dict[str, Any]:
    """点击窗口相对坐标；window 为空时坐标是屏幕坐标"""
    handle = resolve_window(window)
    if activate:
        focus_window(handle)
    flags = MOUSE_BUTTONS.get(button)
    if flags is None:
        raise ComputerUseError(f"不支持的鼠标按键：{button}")
    screen_x, screen_y = screen_point(handle, x, y)
    inputs = [_move_input(screen_x, screen_y)]
    for index in range(max(1, clicks)):
        if index:
            time.sleep(0.05)
        inputs.append(_mouse_input(flags[0]))
        inputs.append(_mouse_input(flags[1]))
    _send(inputs)
    return {"clicked": clicks, "button": button, "x": screen_x, "y": screen_y, "window": handle}


def scroll(
    *,
    amount: int,
    x: int | None = None,
    y: int | None = None,
    window: object = None,
) -> dict[str, Any]:
    """滚动滚轮，amount 为正向上、为负向下"""
    handle = resolve_window(window)
    inputs: list[_INPUT] = []
    if x is not None and y is not None:
        focus_window(handle)
        inputs.append(_move_input(*screen_point(handle, x, y)))
    steps = max(-20, min(20, int(amount)))
    for _ in range(abs(steps)):
        inputs.append(_mouse_input(MOUSEEVENTF_WHEEL, data=WHEEL_DELTA if steps > 0 else -WHEEL_DELTA))
    _send(inputs)
    return {"scrolled": steps, "window": handle}


def drag(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    *,
    window: object = None,
    button: str = "left",
    steps: int = 12,
) -> dict[str, Any]:
    """按住鼠标从起点拖到终点，坐标默认相对窗口"""
    handle = resolve_window(window)
    focus_window(handle)
    flags = MOUSE_BUTTONS.get(button)
    if flags is None:
        raise ComputerUseError(f"不支持的鼠标按键：{button}")
    start = screen_point(handle, from_x, from_y)
    end = screen_point(handle, to_x, to_y)
    total = max(2, min(60, int(steps)))
    inputs = [_move_input(*start), _mouse_input(flags[0])]
    for index in range(1, total + 1):
        inputs.append(_move_input(round(start[0] + (end[0] - start[0]) * index / total), round(start[1] + (end[1] - start[1]) * index / total)))
    inputs.append(_mouse_input(flags[1]))
    _send(inputs)
    return {"dragged": {"from": start, "to": end}, "window": handle}


_VIRTUAL_KEYS: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "windows": 0x5B,
    "lwin": 0x5B,
    "menu": 0x5D,
    "apps": 0x5D,
    "multiply": 0x6A,
    "add": 0x6B,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "nexttrack": 0xB0,
    "prevtrack": 0xB1,
    "stop": 0xB2,
    "playpause": 0xB3,
    ";": 0xBA,
    "semicolon": 0xBA,
    "=": 0xBB,
    "equals": 0xBB,
    ",": 0xBC,
    "comma": 0xBC,
    "-": 0xBD,
    "minus": 0xBD,
    "hyphen": 0xBD,
    ".": 0xBE,
    "period": 0xBE,
    "dot": 0xBE,
    "/": 0xBF,
    "slash": 0xBF,
    "`": 0xC0,
    "backtick": 0xC0,
    "grave": 0xC0,
    "[": 0xDB,
    "bracketleft": 0xDB,
    "\\": 0xDC,
    "backslash": 0xDC,
    "]": 0xDD,
    "bracketright": 0xDD,
    "'": 0xDE,
    "quote": 0xDE,
    "apostrophe": 0xDE,
}
_VIRTUAL_KEYS.update({f"f{index}": 0x6F + index for index in range(1, 25)})
_VIRTUAL_KEYS.update({f"numpad{index}": 0x60 + index for index in range(10)})


def virtual_key(name: str) -> int:
    """把按键名换成虚拟键码，支持字母、数字、功能键、常用控制键和标点键"""
    key = name.strip().casefold()
    if key in _VIRTUAL_KEYS:
        return _VIRTUAL_KEYS[key]
    if len(key) == 1:
        code = ord(key)
        if 0x61 <= code <= 0x7A:
            return code - 0x20
        if 0x30 <= code <= 0x39:
            return code
    raise ComputerUseError(f"不支持的按键：{name}")


def parse_key_sequence(sequence: str) -> list[list[int]]:
    """把 “ctrl+shift+s” 或 “enter tab” 解析成按键序列"""
    if not isinstance(sequence, str) or not sequence.strip():
        raise ComputerUseError("按键序列不能为空")
    strokes: list[list[int]] = []
    for stroke in sequence.split():
        keys = [part for part in stroke.split("+") if part]
        if not keys:
            raise ComputerUseError(f"按键序列无效：{stroke}")
        strokes.append([virtual_key(part) for part in keys])
    return strokes


def press_key(sequence: str) -> dict[str, Any]:
    """按下组合键或连续按键"""
    strokes = parse_key_sequence(sequence)
    inputs: list[_INPUT] = []
    for stroke in strokes:
        inputs.extend(_key_input(key) for key in stroke)
        inputs.extend(_key_input(key, KEYEVENTF_KEYUP) for key in reversed(stroke))
    _send(inputs)
    return {"pressed": sequence.strip(), "count": len(strokes)}


KEYSTROKE_DELAY = 0.006
PASTE_SETTLE_SECONDS = 0.2


def _open_clipboard() -> Any:
    """剪贴板被别的进程占有时短暂重试"""
    user32 = _user32()
    for _ in range(CLIPBOARD_RETRY_ATTEMPTS):
        if user32.OpenClipboard(None):
            return user32
        time.sleep(CLIPBOARD_RETRY_SECONDS)
    raise ComputerUseError("剪贴板被其他程序占用，请稍后重试")


def read_clipboard_text() -> str | None:
    """读取剪贴板文本，内容不是文本时返回 None"""
    kernel32 = _kernel32()
    user32 = _open_clipboard()
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def write_clipboard_text(text: str) -> None:
    """把文本写入系统剪贴板，内容在调用结束后仍归系统持有"""
    if not isinstance(text, str):
        raise ComputerUseError("剪贴板内容必须是文本")
    kernel32 = _kernel32()
    buffer = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buffer)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise ComputerUseError("剪贴板内存分配失败")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise ComputerUseError("剪贴板内存锁定失败")
    try:
        ctypes.memmove(pointer, buffer, size)
    finally:
        kernel32.GlobalUnlock(handle)
    user32 = _open_clipboard()
    try:
        user32.EmptyClipboard()
        # 写入成功后所有权转移给系统，由剪贴板负责释放这块内存
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise ComputerUseError("剪贴板写入被系统拒绝")
    except BaseException:
        kernel32.GlobalFree(handle)
        raise
    finally:
        user32.CloseClipboard()


def _type_keystrokes(text: str) -> dict[str, Any]:
    """逐字注入 Unicode 扫描码；换行和制表符改用真实按键"""
    for char in text:
        if char == "\n":
            press_key("enter")
            continue
        if char == "\t":
            press_key("tab")
            continue
        code = ord(char)
        units = [code] if code <= 0xFFFF else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        inputs: list[_INPUT] = []
        for unit in units:
            inputs.append(_key_input(0, KEYEVENTF_UNICODE, scan=unit))
            inputs.append(_key_input(0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, scan=unit))
        _send(inputs)
        time.sleep(KEYSTROKE_DELAY)
    return {"typed": len(text), "method": "keys"}


def _paste_text(text: str) -> dict[str, Any]:
    """用剪贴板粘贴文字，结束后尽力还原用户原来的文本剪贴板"""
    previous = read_clipboard_text()
    write_clipboard_text(text)
    try:
        press_key("ctrl+v")
        time.sleep(PASTE_SETTLE_SECONDS)
    finally:
        if previous is not None:
            write_clipboard_text(previous)
    return {"typed": len(text), "method": "clipboard"}


def type_text(text: str, *, method: str = "auto") -> dict[str, Any]:
    """向当前窗口输入文字。

    auto 优先用剪贴板粘贴：逐字注入的按键会被输入法和安全软件串字或丢字，
    粘贴失败时退回按键注入。method="keys" 可强制逐字注入。
    """
    if not isinstance(text, str) or not text:
        raise ComputerUseError("输入内容不能为空")
    if method not in {"auto", "keys", "clipboard"}:
        raise ComputerUseError(f"不支持的输入方式：{method}")
    if method == "keys":
        return _type_keystrokes(text)
    try:
        return _paste_text(text)
    except ComputerUseError:
        if method == "clipboard":
            raise
        return _type_keystrokes(text)


MAX_SHUTDOWN_DELAY_SECONDS = 315360000
NO_PENDING_SHUTDOWN_CODE = 1116
SHUTDOWN_FLAGS = {"shutdown": "/s", "restart": "/r"}


def lock_workstation() -> dict[str, Any]:
    """锁定当前 Windows 会话，系统拒绝时抛出 ComputerUseError"""
    if not _user32().LockWorkStation():
        raise ComputerUseError("系统拒绝了锁屏请求")
    return {"locked": True}


def suspend_system() -> dict[str, Any]:
    """让系统进入睡眠，系统拒绝时抛出 ComputerUseError"""
    if not _kernel32().SetSystemPowerState(False, True):
        raise ComputerUseError("系统拒绝了睡眠请求，请确认电源策略允许待机")
    return {"sleeping": True}


def _shutdown_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """执行系统关机命令并取回输出，windowed 冻结程序也不会闪出控制台窗口"""
    return subprocess.run(
        ["shutdown", *arguments],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _command_failure(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    detail = text.splitlines()[0].strip()[:200] if text else ""
    return detail or f"退出码 {completed.returncode}"


def schedule_shutdown(action: str, delay_seconds: int) -> dict[str, Any]:
    """请求关机或重启，命令没被系统接受时抛出 ComputerUseError"""
    flag = SHUTDOWN_FLAGS.get(action)
    if flag is None:
        raise ComputerUseError(f"不支持的关机动作：{action}")
    completed = _shutdown_command([flag, "/t", str(delay_seconds)])
    if completed.returncode != 0:
        raise ComputerUseError(f"关机请求未被系统接受：{_command_failure(completed)}")
    return {"action": action, "delay_seconds": delay_seconds}


def cancel_shutdown() -> dict[str, Any]:
    """取消挂起的关机，没有挂起任务时如实回报而不是谎报成功"""
    completed = _shutdown_command(["/a"])
    if completed.returncode == NO_PENDING_SHUTDOWN_CODE:
        return {"cancelled": False, "reason": "当前没有等待执行的关机或重启任务"}
    if completed.returncode != 0:
        raise ComputerUseError(f"取消关机失败：{_command_failure(completed)}")
    return {"cancelled": True}
