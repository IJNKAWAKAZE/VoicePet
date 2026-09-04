from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from ui.global_hotkey import (
    HOTKEY_ID,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    VK_SPACE,
    WM_HOTKEY,
    GlobalHotkeyWidget,
    HotkeyRegistrationError,
    WindowsHotkeyBackend,
    create_global_hotkey,
)


def app_instance():
    return QApplication.instance() or QApplication([])


class NativeFunction:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


def test_windows_backend_registers_default_global_shortcut_and_unregisters_once():
    user32 = SimpleNamespace(
        RegisterHotKey=NativeFunction(1),
        UnregisterHotKey=NativeFunction(1),
    )
    backend = WindowsHotkeyBackend(
        user32=user32,
        message_reader=lambda message: message,
    )

    backend.register(123)
    backend.register(123)
    assert backend.matches((WM_HOTKEY, HOTKEY_ID)) is True
    assert backend.matches((WM_HOTKEY, HOTKEY_ID + 1)) is False
    backend.unregister()
    backend.unregister()

    assert user32.RegisterHotKey.calls == [
        (
            123,
            HOTKEY_ID,
            MOD_ALT | MOD_CONTROL | MOD_NOREPEAT,
            VK_SPACE,
        )
    ]
    assert user32.UnregisterHotKey.calls == [(123, HOTKEY_ID)]


def test_windows_backend_maps_registration_conflict_to_safe_error():
    user32 = SimpleNamespace(
        RegisterHotKey=NativeFunction(0),
        UnregisterHotKey=NativeFunction(1),
    )
    backend = WindowsHotkeyBackend(user32=user32)

    import pytest

    with pytest.raises(HotkeyRegistrationError, match="注册"):
        backend.register(123)


def test_hidden_widget_emits_matching_hotkey_and_releases_backend_once():
    app_instance()

    class Backend:
        def __init__(self):
            self.registered = []
            self.unregistered = 0

        def register(self, window_id):
            self.registered.append(window_id)

        def matches(self, message):
            return message == "hotkey"

        def unregister(self):
            self.unregistered += 1

    backend = Backend()
    widget = GlobalHotkeyWidget(backend=backend)
    activated = []
    widget.activated.connect(lambda: activated.append(True))

    handled = widget.nativeEvent(b"windows_generic_MSG", "hotkey")
    registered_window_id = backend.registered[0]
    widget.close()
    widget.close()

    assert registered_window_id > 0
    assert backend.registered == [registered_window_id]
    assert handled == (True, 0)
    assert activated == [True]
    assert backend.unregistered == 1
    assert widget.isVisible() is False


def test_hotkey_factory_returns_none_when_registration_is_unavailable(monkeypatch):
    def unavailable():
        raise HotkeyRegistrationError("unsupported")

    monkeypatch.setattr("ui.global_hotkey.GlobalHotkeyWidget", unavailable)

    assert create_global_hotkey() is None
