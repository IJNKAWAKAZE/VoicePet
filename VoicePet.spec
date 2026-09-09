"""VoicePet 的 PyInstaller onedir 打包定义"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

root = Path(SPEC).resolve().parent
datas = [
    (str(root / "assets" / "pet"), "assets/pet"),
    (str(root / "assets" / "ui"), "assets/ui"),
    (str(root / "ui" / "qml"), "ui/qml"),
]
datas += collect_data_files("faster_whisper", includes=["assets/*.onnx"])
binaries = collect_dynamic_libs("sherpa_onnx")
hiddenimports = [
    "PySide6",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "edge_tts",
    "openai",
    "sounddevice",
    "webrtcvad",
    "_webrtcvad",
    "faster_whisper",
    "jsonschema",
    "pypinyin",
    "sentencepiece",
    "sherpa_onnx",
    "ctranslate2",
    "av",
    "win32com",
    "win32com.client",
    "pythoncom",
    "pywintypes",
]
for package in ("openai_codex", "codex_cli_bin"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hidden)

analysis = Analysis(
    [str(root / "app.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(root / "packaging" / "hooks")],
    noarchive=False,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name='VoicePet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(root / "assets" / "ui" / "brand" / "voicepet.ico"),
)
distribution = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name='VoicePet',
)
