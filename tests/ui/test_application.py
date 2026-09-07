import os
from concurrent.futures import Future

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QSG_RHI_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, Signal

import ui.application as application_module
from ui.application import run_ui


class FakeRuntimeHost:
    def __init__(self):
        self.started = 0
        self.closed = 0

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def list_sessions(self):
        future = Future()
        future.set_result(())
        return future

    def current_session_id(self):
        future = Future()
        future.set_result("")
        return future

    def list_tts_voices(self):
        future = Future()
        future.set_result(())
        return future


class FakeHotkey(QObject):
    activated = Signal()

    def __init__(self):
        super().__init__()
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeStartupManager:
    def __init__(self):
        self.calls = []

    def set_enabled(self, enabled):
        self.calls.append(enabled)


def test_run_ui_smoke_constructs_and_closes_without_event_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert run_ui(
        smoke_test=True,
        hotkey_factory=lambda: (_ for _ in ()).throw(
            AssertionError("smoke registered global hotkey")
        ),
        credential_store_factory=lambda path: (_ for _ in ()).throw(
            AssertionError(f"smoke accessed credentials: {path}")
        ),
    ) == 0
    assert not (tmp_path / "VoicePet" / "config.json").exists()


def test_run_ui_normal_mode_builds_starts_and_closes_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime = FakeRuntimeHost()
    hotkey = FakeHotkey()
    startup = FakeStartupManager()
    built = []

    def runtime_builder(config, event_bus, data_root, *, api_key, pet_directory):
        built.append((config, event_bus, data_root, api_key, pet_directory))
        return object()

    def host_factory(services):
        assert services is not None
        return runtime

    result = run_ui(
        runtime_builder=runtime_builder,
        runtime_host_factory=host_factory,
        credential_store_factory=lambda path: type(
            "Credentials",
            (),
            {"get": lambda self, name: "sk-runtime-secret"},
        )(),
        hotkey_factory=lambda: hotkey,
        startup_manager_factory=lambda: startup,
        event_loop=lambda: 17,
    )

    assert result == 17
    assert len(built) == 1
    assert built[0][2] == (tmp_path / "VoicePet").resolve()
    assert built[0][3] == "sk-runtime-secret"
    assert built[0][4].name == "dpsk-girl"
    assert runtime.started == 1
    assert runtime.closed == 1
    assert hotkey.closed == 1
    assert startup.calls == [False]


def test_run_ui_closes_resources_when_qml_controller_start_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime = FakeRuntimeHost()
    hotkey = FakeHotkey()
    monkeypatch.setattr(
        application_module.QmlApplicationController,
        "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("qml failed")),
    )

    with pytest.raises(RuntimeError, match="qml failed"):
        run_ui(
            runtime_builder=lambda *args, **kwargs: object(),
            runtime_host_factory=lambda services: runtime,
            credential_store_factory=lambda path: type(
                "Credentials",
                (),
                {"get": lambda self, name: None},
            )(),
            hotkey_factory=lambda: hotkey,
            startup_manager_factory=FakeStartupManager,
            event_loop=lambda: 0,
        )

    assert runtime.closed == 1
    assert hotkey.closed == 1
