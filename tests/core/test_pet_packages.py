import shutil
import zipfile
from pathlib import Path

import pytest

import core
from core.pet_packages import PetPackageError, PetPackageInstaller


def test_pet_package_types_are_publicly_exported():
    assert core.PetPackageError is PetPackageError
    assert core.PetPackageInstaller is PetPackageInstaller


def test_installer_preserves_nested_spritesheet_path(tmp_path):
    import json

    source = tmp_path / "source"
    shutil.copytree("assets/pet/kawakaze", source)
    (source / "images").mkdir()
    (source / "spritesheet.webp").rename(source / "images" / "spritesheet.webp")
    manifest = json.loads((source / "pet.json").read_text(encoding="utf-8"))
    manifest["spritesheetPath"] = "images/spritesheet.webp"
    (source / "pet.json").write_text(json.dumps(manifest), encoding="utf-8")
    installer = PetPackageInstaller(tmp_path / "pets")
    installed = installer.install(source)
    assert (installed / "images" / "spritesheet.webp").is_file()
    assert installer.validate_directory(installed) == "kawakaze"


def test_pet_package_installer_validates_and_atomically_installs_directory(tmp_path):
    installer = PetPackageInstaller(tmp_path / "pets")
    source = Path("assets/pet/kawakaze").resolve()

    installed = installer.install(source)

    assert installed == (tmp_path / "pets" / "kawakaze").resolve()
    assert (installed / "pet.json").is_file()
    assert (installed / "spritesheet.webp").is_file()
    assert not any(path.name.startswith(".pet-install-") for path in installed.parent.iterdir())


def test_pet_package_installer_validates_directory_without_installing(tmp_path):
    pets = tmp_path / "pets"
    installer = PetPackageInstaller(pets)
    source = Path("assets/pet/kawakaze").resolve()

    pet_id = installer.validate_directory(source)

    assert pet_id == "kawakaze"
    assert not pets.exists()


def test_pet_package_installer_rejects_non_directory_validation_source(tmp_path):
    source = tmp_path / "pet.codex-pet"
    source.write_bytes(b"archive")

    with pytest.raises(PetPackageError, match="目录"):
        PetPackageInstaller(tmp_path / "pets").validate_directory(source)


def test_pet_package_installer_rejects_zip_traversal_without_touching_pets(tmp_path):
    pets = tmp_path / "pets"
    existing = pets / "current"
    existing.mkdir(parents=True)
    sentinel = existing / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    archive = tmp_path / "malicious.codex-pet"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../outside.txt", "attack")
        package.writestr("pet.json", "{}")

    with pytest.raises(PetPackageError, match="路径"):
        PetPackageInstaller(pets).install(archive)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "outside.txt").exists()


def test_pet_package_installer_removes_only_valid_direct_user_pet(tmp_path):
    pets = tmp_path / "pets"
    installer = PetPackageInstaller(pets)
    installed = installer.install(Path("assets/pet/kawakaze").resolve())

    assert installer.remove("kawakaze") is True
    assert not installed.exists()
    assert installer.remove("kawakaze") is False


def test_pet_package_installer_refuses_traversal_and_external_builtin(tmp_path):
    installer = PetPackageInstaller(tmp_path / "user-pets")

    with pytest.raises(PetPackageError, match="ID"):
        installer.remove("../kawakaze")

    assert installer.remove("kawakaze") is False
    assert Path("assets/pet/kawakaze/pet.json").is_file()
