import multiprocessing
import sys

import pytest

import app
from core.worker_entry import run_worker


def test_main_calls_freeze_support_and_dispatches_ui(monkeypatch):
    calls = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))

    result = app.main([], ui_entry=lambda smoke: calls.append(("ui", smoke)) or 7)

    assert result == 7
    assert calls == ["freeze", ("ui", False)]


def test_main_dispatches_smoke_without_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))

    result = app.main(
        ["--smoke-test"],
        ui_entry=lambda smoke: calls.append(("ui", smoke)) or 0,
        worker_entry=lambda: calls.append("worker") or 0,
    )

    assert result == 0
    assert calls == ["freeze", ("ui", True)]


def test_main_dispatches_tool_worker_without_importing_ui(monkeypatch):
    calls = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))

    result = app.main(
        ["--tool-worker"],
        ui_entry=lambda smoke: calls.append("ui") or 0,
        worker_entry=lambda: calls.append("worker") or 9,
    )

    assert result == 9
    assert calls == ["freeze", "worker"]


def test_main_rejects_conflicting_modes():
    with pytest.raises(SystemExit):
        app.main(["--tool-worker", "--smoke-test"])


def test_frozen_smoke_checks_bundled_pet_and_user_data_directory(
    monkeypatch,
    tmp_path,
):
    bundle = tmp_path / "bundle"
    pet = bundle / "assets" / "pet" / "dpsk-girl"
    pet.mkdir(parents=True)
    (pet / "pet.json").write_text("{}", encoding="utf-8")
    (pet / "spritesheet.webp").write_bytes(b"webp")
    qml = bundle / "ui" / "qml"
    qml.mkdir(parents=True)
    (qml / "Main.qml").write_text("import QtQuick", encoding="utf-8")
    brand = bundle / "assets" / "ui" / "brand"
    brand.mkdir(parents=True)
    (brand / "voicepet-mark.svg").write_text("<svg/>", encoding="utf-8")
    theme = bundle / "assets" / "ui" / "themes" / "sunny_sea"
    theme.mkdir(parents=True)
    (theme / "welcome.webp").write_bytes(b"webp")
    local = tmp_path / "local"
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(bundle / "VoicePet.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    assert app._run_frozen_smoke() == 0
    assert (local / "VoicePet").is_dir()


def test_frozen_smoke_rejects_bundle_without_qml_and_theme_assets(
    monkeypatch,
    tmp_path,
):
    bundle = tmp_path / "bundle"
    pet = bundle / "assets" / "pet" / "dpsk-girl"
    pet.mkdir(parents=True)
    (pet / "pet.json").write_text("{}", encoding="utf-8")
    (pet / "spritesheet.webp").write_bytes(b"webp")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(bundle / "VoicePet.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert app._run_frozen_smoke() == 4


def test_frozen_smoke_loads_qml_after_resource_validation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("core.config.default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "_run_frozen_smoke", lambda: calls.append("files") or 0)
    monkeypatch.setattr(
        "ui.application.run_ui",
        lambda *, smoke_test: calls.append(("qml", smoke_test)) or 0,
    )

    assert app._run_ui(True) == 0
    assert calls == ["files", ("qml", True)]


def test_worker_rejection_handles_windowed_build_without_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)

    assert run_worker() == 2
