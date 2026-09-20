import json
import shutil

import pytest

from core.pet_packages import PetPackageInstaller
from ui.pet_catalog import discover_pet_choices, resolve_active_pet_directory


def copy_named_pet(parent, folder):
    directory = parent / folder
    shutil.copytree("assets/pet/kawakaze", directory)
    manifest = json.loads((directory / "pet.json").read_text(encoding="utf-8"))
    manifest["id"] = "forest-cat"
    manifest["displayName"] = "森林猫"
    (directory / "pet.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


@pytest.mark.parametrize("builtin", [True, False])
def test_catalog_identifies_pet_by_manifest_not_folder_name(tmp_path, builtin):
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "data" / "pets"
    directory = copy_named_pet(builtin_root if builtin else user_root, "森林猫-main")
    choices = discover_pet_choices(tmp_path / "data", built_in_root=builtin_root)
    assert len(choices) == 1
    assert choices[0].pet_id == "forest-cat"
    assert choices[0].directory == directory
    assert choices[0].built_in is builtin


def test_saved_selection_resolves_renamed_user_folder(tmp_path):
    directory = copy_named_pet(tmp_path / "pets", "森林猫-main")
    assert resolve_active_pet_directory("forest-cat", tmp_path) == directory


def test_remove_identifies_renamed_user_folder_without_touching_other_pets(tmp_path):
    directory = copy_named_pet(tmp_path / "pets", "森林猫-main")
    other = tmp_path / "pets" / "keep"
    other.mkdir()
    installer = PetPackageInstaller(tmp_path / "pets")
    assert installer.remove("forest-cat") is True
    assert not directory.exists()
    assert other.is_dir()
