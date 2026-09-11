import tomllib
from pathlib import Path


def test_audio_optional_dependencies_are_pinned_to_compatible_ranges():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["optional-dependencies"]["audio"] == [
        "sounddevice>=0.5.6,<0.6",
        "webrtcvad-wheels>=2.0.14,<3",
    ]


def test_asr_optional_dependencies_are_pinned_to_compatible_ranges():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["optional-dependencies"]["asr"] == [
        "faster-whisper>=1.2.1,<2",
        "opencc-python-reimplemented>=0.1.7,<0.2",
        "numpy>=1.26,<3",
    ]


def test_wake_optional_dependencies_are_pinned_to_compatible_ranges():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["optional-dependencies"]["wake"] == [
        "numpy>=1.26,<3",
        "pypinyin>=0.55,<0.56",
        "sentencepiece>=0.2.1,<0.3",
        "sherpa-onnx>=1.13.7,<1.14",
    ]


def test_chinese_wake_runtime_types_are_public():
    import core

    for name in (
        "SherpaOnnxKeywordDetector",
        "WakeCommandPending",
        "WakeDownloadProgress",
        "WakeModelFiles",
        "WakeModelState",
        "WakeRuntimeStatus",
        "WakeWordRuntimeService",
    ):
        assert hasattr(core, name)
        assert name in core.__all__
    assert not hasattr(core, "OpenWakeWordDetector")
    assert "OpenWakeWordDetector" not in core.__all__


def test_llm_optional_dependency_is_pinned_to_compatible_range():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["optional-dependencies"]["llm"] == [
        "openai>=3.7,<4",
    ]


def test_tts_optional_dependencies_are_pinned_to_compatible_ranges():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["optional-dependencies"]["tts"] == [
        "edge-tts>=7.2.8,<8",
        "pywin32>=311,<312",
    ]


def test_ui_optional_dependency_is_pinned_to_compatible_range():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["optional-dependencies"]["ui"] == [
        "PySide6>=6.9,<7",
    ]


def test_build_optional_dependency_is_pinned_to_compatible_range():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["optional-dependencies"]["build"] == [
        "pyinstaller>=6.15,<7",
    ]


def test_project_has_no_builtin_update_dependency_group():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "update" not in metadata["project"]["optional-dependencies"]


def test_application_script_points_to_unified_entry():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["scripts"]["voicepet"] == "app:main"
