import json
import shutil
import zipfile
from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle

from core.config import AppConfig, UiConfig
from core.pet_packages import PetPackageInstaller
from ui.pet_animation_model import PetAnimationModel
from ui.pet_catalog import discover_pet_choices
from ui.viewmodels.pets import PetViewModel
from ui.viewmodels.settings import SettingsViewModel
from ui.viewmodels.theme import ThemeViewModel


@pytest.mark.parametrize(
    ("dialog_name", "selection", "filename"),
    [("petFileDialog", "selectedFile", "小猫 #1.zip"),
     ("petFileDialog", "selectedFile", "小猫 #1.codex-pet"),
     ("petFolderDialog", "selectedFolder", "小猫 #1")],
)
def test_dialog_acceptance_passes_native_path_to_installer(
    qapp, tmp_path, dialog_name, selection, filename,
):
    package = tmp_path / "package"
    shutil.copytree("assets/pet/kawakaze", package)
    manifest = json.loads((package / "pet.json").read_text(encoding="utf-8"))
    manifest["id"] = "forest-cat"
    manifest["displayName"] = "森林猫"
    (package / "pet.json").write_text(json.dumps(manifest), encoding="utf-8")
    source = tmp_path / filename
    if selection == "selectedFolder":
        shutil.copytree(package, source)
    else:
        with zipfile.ZipFile(source, "w") as archive:
            for path in package.iterdir():
                archive.write(path, "外层目录/" + path.name)
    imported = []
    pending = Future()

    class Runtime:
        def install_pet(self, path):
            imported.append(path)
            return pending

    class Store:
        def save(self, config):
            pass

    settings = SettingsViewModel(AppConfig(), Store())
    data_root = tmp_path / "data"
    installer = PetPackageInstaller(data_root / "pets")
    pets = PetViewModel(Runtime(), settings, lambda: discover_pet_choices(data_root))
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    animation = PetAnimationModel(Path("assets/pet/kawakaze").resolve())
    QQuickStyle.setStyle("Basic")
    engine = QQmlEngine()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(items))
    component = QQmlComponent(engine, QUrl.fromLocalFile(
        str(Path("ui/qml/screens/settings/PetSettings.qml").resolve()),
    ))
    root = component.createWithInitialProperties({
        "theme": theme, "settings": settings, "pets": pets, "animationModel": animation,
    })
    window = QQuickWindow()
    try:
        assert root is not None, component.errors()
        root.setParentItem(window.contentItem())
        window.resize(1040, 680)
        window.show()
        dialog = root.findChild(QObject, dialog_name)
        dialog.setProperty("options", 8)
        dialog.open()
        qapp.processEvents()
        assert dialog.setProperty(selection, QUrl.fromLocalFile(str(source)))
        dialog.accepted.emit()
        qapp.processEvents()
        assert not warnings, [item.toString() for item in warnings]
        assert len(imported) == 1
        assert Path(imported[0]) == source
        assert pets.importBusy is True
        pending.set_result(installer.install(imported[0]))
        qapp.processEvents()
        assert pets.importBusy is False
        assert settings.config.ui.active_skin == "forest-cat"
        assert pets.directory_for("forest-cat") == data_root / "pets" / "forest-cat"
        assert "已导入并切换" in root.findChild(QObject, "petImportStatus").property("text")
    finally:
        window.close()
        if root is not None:
            root.deleteLater()
        engine.deleteLater()
        qapp.processEvents()
