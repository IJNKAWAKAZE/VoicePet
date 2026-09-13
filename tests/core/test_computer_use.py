import io

import pytest
from PIL import Image

from core import computer_use
from core.computer_use import (
    ComputerUseError,
    bgra_to_rgb,
    downscale_rgb,
    encode_png,
    encode_png_rgb,
    normalize_absolute,
    parse_key_sequence,
    screen_point,
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
