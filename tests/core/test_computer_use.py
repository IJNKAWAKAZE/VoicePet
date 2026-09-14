import io
import subprocess

import pytest
from PIL import Image

from core import computer_use
from core.computer_use import (
    ComputerUseError,
    bgra_to_rgb,
    cancel_shutdown,
    downscale_rgb,
    encode_png,
    encode_png_rgb,
    lock_workstation,
    normalize_absolute,
    parse_key_sequence,
    schedule_shutdown,
    screen_point,
    suspend_system,
    type_text,
    virtual_key,
    window_usable,
)


def test_normalize_absolute_maps_virtual_screen_corners():
    assert normalize_absolute(0, 0, left=0, top=0, width=1920, height=1080) == (0, 0)
    assert normalize_absolute(1919, 1079, left=0, top=0, width=1920, height=1080) == (65535, 65535)
    assert normalize_absolute(-960, 540, left=-1920, top=0, width=3840, height=1080) == (
        round(960 * 65535 / 3839),
        round(540 * 65535 / 1079),
    )


def test_normalize_absolute_rejects_degenerate_screen():
    with pytest.raises(ComputerUseError):
        normalize_absolute(0, 0, left=0, top=0, width=1, height=1080)


def test_screen_point_passes_through_screen_coordinates_without_window():
    assert screen_point(None, 12, 34) == (12, 34)
    assert screen_point(0, 12, 34) == (12, 34)


def test_bgra_to_rgb_swaps_blue_and_red_channels():
    pixels = bytes([1, 2, 3, 255, 4, 5, 6, 255])
    assert bgra_to_rgb(2, 1, pixels) == bytes([3, 2, 1, 6, 5, 4])
    with pytest.raises(ComputerUseError):
        bgra_to_rgb(2, 1, b"\x00" * 4)


def test_encode_png_produces_decodable_image_with_requested_colors():
    pixels = bytes([0, 0, 255, 255]) + bytes([0, 255, 0, 255])
    image = Image.open(io.BytesIO(encode_png(2, 1, pixels)))
    assert image.size == (2, 1)
    assert image.mode == "RGB"
    assert image.convert("RGB").tobytes() == bytes([255, 0, 0, 0, 255, 0])


def test_encode_png_rejects_mismatched_pixel_data():
    with pytest.raises(ComputerUseError):
        encode_png_rgb(2, 2, b"\x00" * 3)


def test_downscale_rgb_samples_whole_pixels_by_stride():
    rgb = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    width, height, scaled = downscale_rgb(4, 1, rgb, 2)
    assert (width, height) == (2, 1)
    assert scaled == bytes([1, 2, 3, 7, 8, 9])
    assert downscale_rgb(4, 1, rgb, 1) == (4, 1, rgb)


def test_parse_key_sequence_supports_combinations_and_successive_keys():
    assert parse_key_sequence("ctrl+shift+S") == [[0x11, 0x10, 0x53]]
    assert parse_key_sequence("enter tab") == [[0x0D], [0x09]]
    assert parse_key_sequence("alt+F4") == [[0x12, 0x73]]
    assert virtual_key("numpad7") == 0x67


def test_parse_key_sequence_rejects_empty_and_unknown_keys():
    with pytest.raises(ComputerUseError):
        parse_key_sequence("   ")
    with pytest.raises(ComputerUseError):
        parse_key_sequence("bogus_key")
    with pytest.raises(ComputerUseError):
        parse_key_sequence("ctrl+$")


def test_window_usable_filters_hidden_cloaked_and_untitled_windows():
    base = {
        "title": "记事本",
        "visible": True,
        "cloaked": False,
        "width": 800,
        "height": 600,
        "minimized": False,
        "include_minimized": False,
    }
    assert window_usable(**base)
    assert not window_usable(**{**base, "visible": False})
    assert not window_usable(**{**base, "cloaked": True})
    assert not window_usable(**{**base, "title": "   "})
    assert not window_usable(**{**base, "width": 0})
    assert not window_usable(**{**base, "minimized": True})
    assert window_usable(**{**base, "minimized": True, "include_minimized": True})


class _InputRecorder:
    """记录键盘与剪贴板调用，避免测试真的改动系统状态"""

    def __init__(self, clipboard: str | None = None, paste_error: BaseException | None = None) -> None:
        self.clipboard = clipboard
        self.paste_error = paste_error
        self.writes: list[str] = []
        self.keys: list[str] = []
        self.keystrokes: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(computer_use, "read_clipboard_text", self.read)
        monkeypatch.setattr(computer_use, "write_clipboard_text", self.write)
        monkeypatch.setattr(computer_use, "press_key", self.press)
        monkeypatch.setattr(computer_use, "_type_keystrokes", self.keystroke)
        monkeypatch.setattr(computer_use.time, "sleep", lambda _seconds: None)

    def read(self) -> str | None:
        return self.clipboard

    def write(self, text: str) -> None:
        self.writes.append(text)

    def press(self, sequence: str) -> dict[str, object]:
        self.keys.append(sequence)
        if self.paste_error is not None and sequence == "ctrl+v":
            raise self.paste_error
        return {"pressed": sequence.strip(), "count": 1}

    def keystroke(self, text: str) -> dict[str, object]:
        self.keystrokes.append(text)
        return {"typed": len(text), "method": "keys"}


def test_type_text_prefers_clipboard_paste_and_restores_previous_content(monkeypatch):
    recorder = _InputRecorder("用户原来的剪贴板")
    recorder.install(monkeypatch)

    text = "你好，这是 VoicePet 写的一句话。"
    result = type_text(text)

    assert result == {"typed": len(text), "method": "clipboard"}
    assert recorder.writes == [text, "用户原来的剪贴板"]
    assert recorder.keys == ["ctrl+v"]
    assert recorder.keystrokes == []


def test_type_text_does_not_restore_when_clipboard_has_no_text(monkeypatch):
    recorder = _InputRecorder(None)
    recorder.install(monkeypatch)

    assert type_text("abc").get("method") == "clipboard"
    assert recorder.writes == ["abc"]


def test_type_text_falls_back_to_keystrokes_when_paste_fails(monkeypatch):
    recorder = _InputRecorder("原内容", paste_error=ComputerUseError("注入被拒"))
    recorder.install(monkeypatch)

    result = type_text("纯 ASCII 文本")

    assert result == {"typed": 10, "method": "keys"}
    assert recorder.keystrokes == ["纯 ASCII 文本"]
    assert recorder.writes[-1] == "原内容"


def test_type_text_keys_method_skips_clipboard(monkeypatch):
    recorder = _InputRecorder("原内容")
    recorder.install(monkeypatch)

    assert type_text("plain", method="keys") == {"typed": 5, "method": "keys"}
    assert recorder.writes == []
    assert recorder.keys == []


def test_type_text_clipboard_method_propagates_paste_failure(monkeypatch):
    recorder = _InputRecorder("原内容", paste_error=ComputerUseError("注入被拒"))
    recorder.install(monkeypatch)

    with pytest.raises(ComputerUseError):
        type_text("abc", method="clipboard")
    assert recorder.keystrokes == []


def test_type_text_rejects_empty_text_and_unknown_method():
    with pytest.raises(ComputerUseError):
        type_text("")
    with pytest.raises(ComputerUseError):
        type_text("abc", method="telepathy")


class _ShutdownRecorder:
    """记录关机命令，避免测试真的执行系统关机"""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.reply = subprocess.CompletedProcess(["shutdown"], returncode, stdout, stderr)
        self.commands: list[list[str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(computer_use.subprocess, "run", self.run)
        monkeypatch.setattr(computer_use.subprocess, "Popen", self.popen)

    def run(self, command, **kwargs):
        self.commands.append(list(command))
        return self.reply

    def popen(self, *args, **kwargs):
        raise AssertionError("关机不应使用不检查结果的 Popen")


def test_schedule_shutdown_waits_for_the_system_command(monkeypatch):
    recorder = _ShutdownRecorder(0)
    recorder.install(monkeypatch)

    assert schedule_shutdown("restart", 60) == {"action": "restart", "delay_seconds": 60}
    assert recorder.commands == [["shutdown", "/r", "/t", "60"]]


def test_schedule_shutdown_reports_request_the_system_rejected(monkeypatch):
    recorder = _ShutdownRecorder(1, stdout="Usage: shutdown [/i | /l | /s]\n更多帮助")
    recorder.install(monkeypatch)

    with pytest.raises(ComputerUseError) as error:
        schedule_shutdown("shutdown", 60)

    assert "关机请求未被系统接受" in str(error.value)
    assert "Usage: shutdown" in str(error.value)


def test_schedule_shutdown_rejects_unknown_action(monkeypatch):
    recorder = _ShutdownRecorder(0)
    recorder.install(monkeypatch)

    with pytest.raises(ComputerUseError):
        schedule_shutdown("reboot", 0)
    assert recorder.commands == []


def test_cancel_shutdown_reports_missing_pending_task(monkeypatch):
    recorder = _ShutdownRecorder(1116, stdout="Unable to abort the system shutdown because no shutdown was in progress.(1116)")
    recorder.install(monkeypatch)

    assert cancel_shutdown() == {"cancelled": False, "reason": "当前没有等待执行的关机或重启任务"}
    assert recorder.commands == [["shutdown", "/a"]]


def test_cancel_shutdown_reports_success_then_failure(monkeypatch):
    recorder = _ShutdownRecorder(0)
    recorder.install(monkeypatch)
    assert cancel_shutdown() == {"cancelled": True}

    failing = _ShutdownRecorder(5, stderr="拒绝访问。")
    failing.install(monkeypatch)
    with pytest.raises(ComputerUseError) as error:
        cancel_shutdown()

    assert "拒绝访问" in str(error.value)


class _PowerApi:
    """电源与锁屏 API 的假实现，记录调用但不改动本机状态"""

    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted
        self.calls: list[str] = []

    def LockWorkStation(self) -> bool:
        self.calls.append("lock")
        return self.accepted

    def SetSystemPowerState(self, suspend: bool, force: bool) -> bool:
        self.calls.append("suspend")
        return self.accepted


def test_lock_workstation_reports_refusal(monkeypatch):
    denied = _PowerApi(False)
    monkeypatch.setattr(computer_use, "_user32", lambda: denied)

    with pytest.raises(ComputerUseError) as error:
        lock_workstation()

    assert "拒绝" in str(error.value)
    assert denied.calls == ["lock"]


def test_suspend_system_reports_success_and_refusal(monkeypatch):
    accepted = _PowerApi(True)
    monkeypatch.setattr(computer_use, "_kernel32", lambda: accepted)
    assert suspend_system() == {"sleeping": True}

    denied = _PowerApi(False)
    monkeypatch.setattr(computer_use, "_kernel32", lambda: denied)
    with pytest.raises(ComputerUseError) as error:
        suspend_system()

    assert "睡眠" in str(error.value)
    assert denied.calls == ["suspend"]


class _FakeDeskUser32:
    """用脚本化窗口模拟 EnumWindows，验证单个窗口读不到信息时不会中断枚举"""

    def __init__(self, titles: dict[int, str]) -> None:
        self.titles = titles
        self.foreground = min(titles)

    def SetProcessDpiAwarenessContext(self, context):
        return True

    def EnumWindows(self, callback, param):
        for handle in self.titles:
            if not callback(handle, param):
                break
        return True

    def GetWindowTextLengthW(self, handle):
        return len(self.titles[handle])

    def GetWindowTextW(self, handle, buffer, size):
        buffer.value = self.titles[handle]

    def IsWindowVisible(self, handle):
        return True

    def IsIconic(self, handle):
        return False

    def GetForegroundWindow(self):
        return self.foreground


def test_list_windows_skips_windows_whose_geometry_cannot_be_read(monkeypatch):
    monkeypatch.setattr(computer_use, "ensure_dpi_awareness", lambda: None)
    monkeypatch.setattr(computer_use, "_user32", lambda: _FakeDeskUser32({11: "记事本", 22: "画图"}))
    monkeypatch.setattr(computer_use, "_is_cloaked", lambda handle: False)
    monkeypatch.setattr(computer_use, "_process_name", lambda handle: "fake.exe")

    def rect(handle):
        if handle == 11:
            raise ComputerUseError("无法读取窗口位置")
        return 100, 200, 300, 400

    monkeypatch.setattr(computer_use, "window_rect", rect)

    windows = computer_use.list_windows()

    assert [window.handle for window in windows] == [22]
    assert windows[0].title == "画图"


class _FakeForegroundUser32:
    """模拟前台窗口切换，验证被拒时会升级重试并最终如实报错"""

    def __init__(self, foreground: int, outcomes: list[bool]) -> None:
        self.foreground = foreground
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def IsWindow(self, handle):
        return True

    def IsIconic(self, handle):
        return False

    def GetForegroundWindow(self):
        return self.foreground

    def GetWindowThreadProcessId(self, handle, process_id):
        return 1

    def AttachThreadInput(self, first, second, attach):
        self.calls.append(f"attach:{first}->{second}:{attach}")
        return True

    def SetForegroundWindow(self, handle):
        target = handle.value if hasattr(handle, "value") else int(handle)
        self.calls.append(f"set:{target}")
        if not self.outcomes or not self.outcomes.pop(0):
            return False
        self.foreground = target
        return True


def test_focus_window_uses_the_first_switch_when_the_system_allows_it(monkeypatch):
    user32 = _FakeForegroundUser32(500, [True])
    monkeypatch.setattr(computer_use, "_user32", lambda: user32)
    monkeypatch.setattr(computer_use, "_current_thread_id", lambda: 7)

    computer_use.focus_window(900)

    assert user32.calls == ["set:900"]


def test_focus_window_escalates_to_attached_input_queue_when_refused(monkeypatch):
    user32 = _FakeForegroundUser32(500, [False, True])
    monkeypatch.setattr(computer_use, "_user32", lambda: user32)
    monkeypatch.setattr(computer_use, "_current_thread_id", lambda: 7)

    computer_use.focus_window(900)

    assert user32.calls == ["set:900", "attach:7->1:True", "set:900", "attach:7->1:False"]


def test_focus_window_reports_switch_the_system_refused_twice(monkeypatch):
    user32 = _FakeForegroundUser32(500, [False, False])
    monkeypatch.setattr(computer_use, "_user32", lambda: user32)
    monkeypatch.setattr(computer_use, "_current_thread_id", lambda: 7)

    with pytest.raises(ComputerUseError) as error:
        computer_use.focus_window(900)

    assert "窗口切换" in str(error.value)
    assert user32.calls == ["set:900", "attach:7->1:True", "set:900", "attach:7->1:False"]


def test_virtual_key_supports_punctuation_keys():
    assert virtual_key(";") == 0xBA
    assert virtual_key("semicolon") == 0xBA
    assert virtual_key(",") == 0xBC
    assert virtual_key("-") == 0xBD
    assert virtual_key("\\") == 0xDC
    assert virtual_key("'") == 0xDE
    assert parse_key_sequence("ctrl+;") == [[0x11, 0xBA]]
    assert parse_key_sequence("ctrl+shift+=") == [[0x11, 0x10, 0xBB]]
