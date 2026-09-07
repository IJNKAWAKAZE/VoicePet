from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QSignalSpy, QTest
from test_pet_viewmodel import Runtime, Store
from test_qml_application import build_controller

from core.config import AppConfig
from ui.pet_catalog import PetChoice
from ui.viewmodels.pets import PetViewModel
from ui.viewmodels.settings import SettingsViewModel


@pytest.fixture
def pets(qapp, monkeypatch):
    monkeypatch.setattr(PetViewModel, "_STATUS_TIMEOUT_MS", 40, raising=False)
    directory = Path("assets/pet/dpsk-girl").resolve()
    choices = (PetChoice("guga", "咕嘎", directory, False),)
    runtime = Runtime(Path("pets/guga"))
    vm = PetViewModel(runtime, SettingsViewModel(AppConfig(), Store()), lambda: choices)
    yield vm, runtime
    vm.deleteLater()
    qapp.processEvents()


def test_import_success_expires_without_user_input_and_keeps_selection(pets):
    vm, _ = pets
    vm.import_pet("guga.zip")
    assert vm.statusMessage == "已导入并切换：咕嘎"
    changed = QSignalSpy(vm.statusMessageChanged)
    assert changed.wait(1000)
    assert vm.statusMessage == ""
    assert vm.catalogModel.data(vm.catalogModel.index(0), Qt.UserRole + 5) is True


def test_busy_import_is_not_expired_by_previous_success_timer(pets):
    vm, runtime = pets
    vm.import_pet("first.zip")
    pending = Future()
    runtime.install_pet = lambda source: pending
    vm.import_pet("second.zip")
    QTest.qWait(100)
    assert vm.importBusy
    assert "正在" in vm.statusMessage
    pending.set_result(Path("pets/guga"))
    changed = QSignalSpy(vm.statusMessageChanged)
    assert changed.wait(1000)
    assert not vm.importBusy
    assert vm.statusMessage == ""


def test_new_success_restarts_status_lifetime(pets, monkeypatch):
    vm, _ = pets
    vm.import_pet("first.zip")
    monkeypatch.setattr(PetViewModel, "_STATUS_TIMEOUT_MS", 5000)
    vm.import_pet("second.zip")
    QTest.qWait(100)
    assert vm.statusMessage == "已导入并切换：咕嘎"


@pytest.mark.parametrize("navigation", ["section", "category", "hide"])
def test_leaving_pet_page_clears_completed_import_status(qapp, navigation):
    controller, runtime, _, qml, shell, *_ = build_controller(qapp)
    controller.start()
    try:
        shell.show_main("settings")
        page = qml.root_objects[0].findChild(QObject, "settingsPage")
        page.setProperty("category", "pets")
        qapp.processEvents()
        vm = controller._pets
        pending = Future()
        runtime.install_pet = lambda source: pending
        vm.import_pet("builtin-copy.zip")
        pending.set_result(Path("pets/dpsk-girl"))
        assert "已导入" in vm.statusMessage
        qapp.processEvents()
        items = [page]
        for item in items:
            items.extend(item.childItems())
        badge = next((item for item in items if item.objectName() == "activePetBadge_dpsk-girl"), None)
        assert badge is not None and badge.property("visible")
        if navigation == "section":
            shell.navigate("chat")
        elif navigation == "category":
            page.setProperty("category", "general")
        else:
            shell.hide_main()
        qapp.processEvents()
        assert vm.statusMessage == ""
    finally:
        controller.close()


@pytest.mark.parametrize("navigation", ["category", "hide"])
def test_returning_to_pet_page_does_not_restore_background_completion(qapp, navigation):
    controller, runtime, _, qml, shell, *_ = build_controller(qapp)
    controller.start()
    try:
        shell.show_main("settings")
        page = qml.root_objects[0].findChild(QObject, "settingsPage")
        page.setProperty("category", "pets")
        qapp.processEvents()
        pending = Future()
        runtime.install_pet = lambda source: pending
        vm = controller._pets
        vm.import_pet("builtin-copy.zip")
        if navigation == "category":
            page.setProperty("category", "general")
        else:
            shell.hide_main()
        qapp.processEvents()
        assert vm.importBusy and "正在" in vm.statusMessage
        pending.set_result(Path("pets/dpsk-girl"))
        if navigation == "category":
            page.setProperty("category", "pets")
        else:
            shell.show_main("settings")
        qapp.processEvents()
        assert vm.statusMessage == ""
        assert not qml.root_objects[0].findChild(QObject, "petImportStatus").property("visible")
    finally:
        controller.close()
