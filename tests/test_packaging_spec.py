from pathlib import Path


def test_pyinstaller_spec_uses_onedir_and_bundles_default_pet():
    spec = Path("VoicePet.spec").read_text(encoding="utf-8")

    assert "COLLECT(" in spec
    assert "assets/pet" in spec
    assert "name='VoicePet'" in spec
    assert "console=False" in spec
    assert "BUNDLE(" not in spec


def test_pyinstaller_spec_collects_required_optional_runtime_packages():
    spec = Path("VoicePet.spec").read_text(encoding="utf-8")

    for package in (
        "PySide6",
        "edge_tts",
        "openai",
        "sounddevice",
        "webrtcvad",
        "faster_whisper",
        "jsonschema",
        "pypinyin",
        "sentencepiece",
        "sherpa_onnx",
        "win32com",
    ):
        assert package in spec
    assert "openwakeword" not in spec
    assert "collect_dynamic_libs" in spec


def test_pyinstaller_spec_bundles_faster_whisper_vad_model():
    spec = Path("VoicePet.spec").read_text(encoding="utf-8")

    assert "collect_data_files" in spec
    assert 'collect_data_files("faster_whisper"' in spec
    assert 'includes=["assets/*.onnx"]' in spec


def test_pyinstaller_spec_uses_local_webrtcvad_hook_override():
    spec = Path("VoicePet.spec").read_text(encoding="utf-8")
    hook = Path("packaging/hooks/hook-webrtcvad.py").read_text(encoding="utf-8")

    assert "hookspath" in spec
    assert "packaging" in spec
    assert 'hiddenimports = ["_webrtcvad"]' in hook


def test_release_build_contains_only_the_main_application():
    spec = Path("VoicePet.spec").read_text(encoding="utf-8")
    script = Path("packaging/build_release.ps1").read_text(encoding="utf-8")

    combined = f"{spec}\n{script}"
    assert "VoicePetUpdate" not in combined
    assert "update-channel.json" not in combined
    assert "cryptography" not in spec
    assert script.count("PyInstaller") == 1


def test_pyinstaller_spec_bundles_qml_theme_assets_and_qt_quick_runtime():
    spec = Path("VoicePet.spec").read_text(encoding="utf-8")

    assert 'root / "ui" / "qml"' in spec
    assert 'root / "assets" / "ui"' in spec
    assert 'root / "assets" / "ui" / "brand" / "voicepet.ico"' in spec
    for module in (
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickControls2",
    ):
        assert module in spec


def test_python_package_data_includes_qml_and_brand_svg():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert '[tool.setuptools.package-data]' in pyproject
    assert '"qml/**/*.qml"' in pyproject
    assert '"qml/**/*.svg"' in pyproject
    assert '"assets/ui/brand/*.svg"' in pyproject
