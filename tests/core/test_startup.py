from pathlib import Path

from core.startup import WindowsStartupManager, quote_startup_command


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 1
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def CreateKeyEx(self, root, path, reserved, access):
        assert root is self.HKEY_CURRENT_USER
        assert reserved == 0
        assert access == self.KEY_SET_VALUE
        self.path = path
        return FakeKey()

    def SetValueEx(self, key, name, reserved, value_type, value):
        del key
        assert reserved == 0
        assert value_type == self.REG_SZ
        self.values[name] = value

    def DeleteValue(self, key, name):
        del key
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_startup_manager_writes_and_removes_current_user_run_value():
    registry = FakeRegistry()
    manager = WindowsStartupManager(
        command=(r"C:\Program Files\VoicePet\VoicePet.exe",),
        registry=registry,
    )

    manager.set_enabled(True)

    assert registry.values["VoicePet"] == (
        '"C:\\Program Files\\VoicePet\\VoicePet.exe"'
    )
    manager.set_enabled(False)
    assert registry.values == {}
    manager.set_enabled(False)


def test_quote_startup_command_preserves_executable_and_arguments():
    command = quote_startup_command(
        (str(Path(r"C:\Python\pythonw.exe")), r"E:\Voice Pet\app.py")
    )

    assert command == 'C:\\Python\\pythonw.exe "E:\\Voice Pet\\app.py"'
