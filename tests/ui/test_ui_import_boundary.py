import json
import subprocess
import sys
from pathlib import Path


def test_qml_entry_does_not_load_or_export_legacy_widgets():
    script = (
        "import json, sys; import ui.application, ui.pet_animation; "
        "print(json.dumps({'modules': sorted(sys.modules), "
        "'legacy_controller': hasattr(ui.application, 'ApplicationController'), "
        "'legacy_animation': hasattr(ui.pet_animation, 'PetAnimationWidget'), "
        "'exports': ui.__all__}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    loaded = json.loads(result.stdout)

    assert not {
        "ui.pet_shell",
        "ui.settings_window",
        "ui.manual_input",
        "ui.confirm_dialog",
    }.intersection(loaded["modules"])
    assert not loaded["legacy_controller"]
    assert not loaded["legacy_animation"]
    assert {"ui.pet_catalog", "ui.qml_application", "ui.qml_runtime"}.issubset(
        loaded["modules"]
    )
    assert not {
        "PetShellWindow",
        "SettingsWindow",
        "ConversationBubble",
        "SonarStatusWidget",
    }.intersection(loaded["exports"])
