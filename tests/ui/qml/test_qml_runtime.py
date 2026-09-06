import pytest
from PySide6.QtCore import QObject, QUrl

from ui.qml_resources import qml_root
from ui.qml_runtime import QmlRuntime, QmlRuntimeError


def test_runtime_loads_and_closes_bootstrap(qapp):
    marker = QObject()
    runtime = QmlRuntime(qapp, {"testMarker": marker}, qml_root() / "Bootstrap.qml")

    runtime.load()

    roots = runtime.root_objects
    assert len(roots) == 1
    assert roots[0].objectName() == "qmlBootstrap"

    runtime.close()

    assert runtime.root_objects == ()


def test_runtime_rejects_qml_with_load_warning(qapp, tmp_path):
    broken = tmp_path / "Broken.qml"
    broken.write_text("import QtQuick\nQtObject { missingProperty: true }", encoding="utf-8")
    runtime = QmlRuntime(qapp, {}, broken)

    with pytest.raises(QmlRuntimeError, match="QML"):
        runtime.load()

    runtime.close()


def test_runtime_accepts_qurl_entrypoint(qapp):
    runtime = QmlRuntime(qapp, {}, QUrl.fromLocalFile(str(qml_root() / "Bootstrap.qml")))

    runtime.load()

    assert runtime.root_objects[0].objectName() == "qmlBootstrap"
    runtime.close()


@pytest.mark.parametrize(
    "object_name",
    [
        "demoButton",
        "demoIconButton",
        "demoTextField",
        "demoTextArea",
        "demoCard",
        "demoToggle",
        "demoComboBox",
        "demoSlider",
        "demoBadge",
    ],
)
def test_bootstrap_loads_shared_component(object_name, qapp):
    runtime = QmlRuntime(qapp, {}, qml_root() / "Bootstrap.qml")

    runtime.load()

    assert runtime.root_objects[0].findChild(QObject, object_name) is not None
    runtime.close()
