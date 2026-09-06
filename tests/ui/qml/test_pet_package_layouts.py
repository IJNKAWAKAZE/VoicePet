import json
import zipfile

import pytest
from PIL import Image, ImageDraw

from core.events import ConversationPhase
from core.pet_packages import PetPackageError, PetPackageInstaller
from ui.pet_animation import PetAssetError, PetSpriteAtlas
from ui.pet_animation_model import PetAnimationModel
from ui.pet_shell import discover_pet_choices


@pytest.fixture
def nine_row_pet(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pet.json").write_text(json.dumps({
        "id": "guga", "displayName": "咕嘎", "description": "测试九行动画",
        "spritesheetPath": "spritesheet.webp",
    }), encoding="utf-8")
    image = Image.new("RGBA", (1536, 1872))
    draw = ImageDraw.Draw(image)
    for row in range(9):
        draw.rectangle((10, row * 208 + 10, 100, row * 208 + 150), fill="pink")
    image.save(source / "spritesheet.webp", lossless=True)
    return source


@pytest.mark.parametrize("archive", [False, True])
def test_nine_row_package_import_discovery_and_animation(nine_row_pet, tmp_path, qapp, archive):
    source = nine_row_pet
    if archive:
        source = tmp_path / "guga.codex-pet.zip"
        with zipfile.ZipFile(source, "w") as package:
            for path in nine_row_pet.iterdir():
                package.write(path, path.name)
    installer = PetPackageInstaller(tmp_path / "data/pets")
    installed = installer.install(source)
    assert installer.validate_directory(installed) == "guga"
    assert any(choice.pet_id == "guga" for choice in discover_pet_choices(tmp_path / "data"))
    animation = PetAnimationModel(installed)
    for phase in ConversationPhase:
        animation.set_phase(phase)
        animation.advance_frame()
        assert 0 <= animation.row < 9
        assert not animation._atlas.frame(animation.row, animation.column).isNull()
    with pytest.raises(PetAssetError, match="索引"):
        animation._atlas.frame(9, 0)


@pytest.mark.parametrize("version", [1, 2, 99, "legacy"])
def test_version_metadata_does_not_gate_valid_layout(nine_row_pet, tmp_path, qapp, version):
    manifest = nine_row_pet / "pet.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["spriteVersionNumber"] = version
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert PetPackageInstaller(tmp_path / "pets").validate_directory(nine_row_pet) == "guga"
    assert PetSpriteAtlas.load(nine_row_pet).pet_id == "guga"
    choices = discover_pet_choices(tmp_path / "data", built_in_root=tmp_path)
    assert any(choice.pet_id == "guga" for choice in choices)


def test_missing_real_required_field_names_the_field(nine_row_pet, tmp_path):
    manifest = nine_row_pet / "pet.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    del data["spritesheetPath"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PetPackageError, match="spritesheetPath"):
        PetPackageInstaller(tmp_path / "pets").validate_directory(nine_row_pet)


def test_removing_version_gate_does_not_accept_arbitrary_dimensions(nine_row_pet, tmp_path, qapp):
    Image.new("RGBA", (1536, 1664)).save(nine_row_pet / "spritesheet.webp", lossless=True)
    with pytest.raises(PetPackageError, match="尺寸"):
        PetPackageInstaller(tmp_path / "pets").validate_directory(nine_row_pet)
    with pytest.raises(PetAssetError, match="尺寸"):
        PetSpriteAtlas.load(nine_row_pet)
    assert not discover_pet_choices(tmp_path / "data", built_in_root=tmp_path)
