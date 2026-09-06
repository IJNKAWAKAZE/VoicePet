from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QUrl
from test_qml_application import build_controller, completed


@pytest.mark.parametrize(
    ("dialog_name", "method_name", "filename"),
    [("diagnosticsExportDialog", "export_diagnostics", "诊断 #1.zip"),
     ("memoriesExportDialog", "export_memories", "记忆 #1.json")],
)
def test_export_dialog_acceptance_passes_local_path(qapp, tmp_path, dialog_name, method_name, filename):
    controller, service, _tray, qml, *_ = build_controller(qapp)
    exported = []

    def export(destination):
        exported.append(destination)
        return completed(0)

    setattr(service, method_name, export)
    controller.start()
    try:
        root = qml.root_objects[0]
        root.show()
        dialog = root.findChild(QObject, dialog_name)
        assert dialog is not None
        dialog.setProperty("options", 8)
        dialog.open()
        qapp.processEvents()
        destination = tmp_path / filename
        assert dialog.setProperty("selectedFile", QUrl.fromLocalFile(str(destination)))
        dialog.accepted.emit()
        qapp.processEvents()
        assert [Path(path) for path in exported] == [destination]
        dialog.close()
    finally:
        controller.close()
