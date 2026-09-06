from concurrent.futures import Future
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from core.config import AppConfig
from core.pet_packages import PetPackageError
from ui.pet_shell import PetChoice
from ui.viewmodels.pets import PetViewModel
from ui.viewmodels.settings import SettingsViewModel


def completed(value=None):
    future = Future()
    future.set_result(value)
    return future


class Store:
    def __init__(self):
        self.saved = []

    def save(self, config):
        self.saved.append(config)


class Runtime:
    def __init__(self, installed):
        self.installed = installed
        self.imported = []
        self.deleted = []

    def install_pet(self, source):
        self.imported.append(source)
        return completed(self.installed)

    def remove_pet(self, pet_id):
        self.deleted.append(pet_id)
        return completed(True)


def test_catalog_orders_builtin_first_and_searches_name_and_id(tmp_path):
    builtin = PetChoice("dpsk-girl", "鲸鱼娘", Path("assets/pet/dpsk-girl").resolve(), True)
    user = PetChoice("forest-cat", "森林猫", Path("assets/pet/dpsk-girl").resolve(), False)
    choices = (user, builtin)
    viewmodel = PetViewModel(
        Runtime(tmp_path / "pets" / "new-pet"),
        SettingsViewModel(AppConfig(), Store()),
        lambda: choices,
    )

    viewmodel.refresh()

    assert viewmodel.catalog_model.data(viewmodel.catalog_model.index(0), Qt.UserRole + 1) == "dpsk-girl"
    viewmodel.set_search("forest")
    assert viewmodel.catalog_model.rowCount() == 1
    viewmodel.set_search("鲸鱼")
    assert viewmodel.catalog_model.rowCount() == 1


def test_select_emits_preview_and_saves_active_pet():
    store = Store()
    settings = SettingsViewModel(AppConfig(), store)
    choices = (
        PetChoice("dpsk-girl", "鲸鱼娘", Path("assets/pet/dpsk-girl").resolve(), True),
    )
    viewmodel = PetViewModel(Runtime(Path("unused")), settings, lambda: choices)
    preview = QSignalSpy(viewmodel.previewRequested)
    viewmodel.refresh()

    viewmodel.select_pet("dpsk-girl")

    assert preview.count() == 1
    assert settings.config.ui.active_skin == "dpsk-girl"


def test_delete_active_user_pet_switches_to_builtin_first(tmp_path):
    store = Store()
    initial = AppConfig.from_dict(
        {**AppConfig().to_dict(), "ui": {**AppConfig().to_dict()["ui"], "active_skin": "forest-cat"}}
    )
    settings = SettingsViewModel(initial, store)
    builtin = PetChoice("dpsk-girl", "鲸鱼娘", Path("assets/pet/dpsk-girl").resolve(), True)
    user = PetChoice("forest-cat", "森林猫", Path("assets/pet/dpsk-girl").resolve(), False)
    runtime = Runtime(tmp_path / "pets" / "new-pet")
    viewmodel = PetViewModel(runtime, settings, lambda: (builtin, user))
    viewmodel.refresh()

    viewmodel.delete_pet("forest-cat")

    assert settings.config.ui.active_skin == "dpsk-girl"
    assert runtime.deleted == ["forest-cat"]


def test_builtin_pet_cannot_be_deleted():
    builtin = PetChoice("dpsk-girl", "鲸鱼娘", Path("assets/pet/dpsk-girl").resolve(), True)
    runtime = Runtime(Path("unused"))
    viewmodel = PetViewModel(runtime, SettingsViewModel(AppConfig(), Store()), lambda: (builtin,))
    viewmodel.refresh()

    viewmodel.delete_pet("dpsk-girl")

    assert runtime.deleted == []


def test_pet_directory_lookup_only_returns_catalog_entries():
    directory = Path("assets/pet/dpsk-girl").resolve()
    builtin = PetChoice("dpsk-girl", "鲸鱼娘", directory, True)
    viewmodel = PetViewModel(
        Runtime(Path("unused")),
        SettingsViewModel(AppConfig(), Store()),
        lambda: (builtin,),
    )
    viewmodel.refresh()

    assert viewmodel.directory_for("dpsk-girl") == directory
    assert viewmodel.directory_for("missing") is None


def test_import_refreshes_and_selects_new_pet(tmp_path):
    installed = tmp_path / "pets" / "new-pet"
    current = [
        PetChoice("dpsk-girl", "鲸鱼娘", Path("assets/pet/dpsk-girl").resolve(), True),
    ]
    runtime = Runtime(installed)
    settings = SettingsViewModel(AppConfig(), Store())
    viewmodel = PetViewModel(runtime, settings, lambda: tuple(current))
    viewmodel.refresh()
    current.append(PetChoice("new-pet", "新形象", Path("assets/pet/dpsk-girl").resolve(), False))

    viewmodel.import_pet("D:/new.codex-pet")

    assert runtime.imported == ["D:/new.codex-pet"]
    assert settings.config.ui.active_skin == "new-pet"


def test_failed_import_displays_validation_reason_and_keeps_current_pet():
    pending = Future()
    runtime = Runtime(Path("unused"))
    runtime.install_pet = lambda source: pending
    settings = SettingsViewModel(AppConfig(), Store())
    pets = PetViewModel(runtime, settings, lambda: ())
    errors = QSignalSpy(pets.errorOccurred)
    pets.import_pet("invalid.zip")
    assert pets.importBusy is True
    pending.set_exception(PetPackageError("桌宠包必须包含唯一 pet.json"))
    assert pets.importBusy is False
    assert pets.statusMessage == "桌宠包必须包含唯一 pet.json"
    assert errors.count() == 1
    assert settings.config.ui.active_skin == "dpsk-girl"
