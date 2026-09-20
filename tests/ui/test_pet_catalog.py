import json
import shutil
from pathlib import Path

from ui.pet_catalog import PetChoice, discover_pet_choices, resolve_active_pet_directory


def write_pet_choice(
    parent,
    directory_name,
    *,
    pet_id=None,
    display_name="测试形象",
    version=2,
    sprite_path="spritesheet.webp",
):
    directory = parent / directory_name
    directory.mkdir(parents=True)
    (directory / "pet.json").write_text(
        json.dumps(
            {
                "id": pet_id or directory_name,
                "displayName": display_name,
                "description": "测试说明",
                "spriteVersionNumber": version,
                "spritesheetPath": sprite_path,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if sprite_path == "spritesheet.webp":
        shutil.copyfile("assets/pet/kawakaze/spritesheet.webp", directory / sprite_path)
    return directory


def test_pet_discovery_returns_safe_built_in_first_choices(tmp_path):
    built_in_root = tmp_path / "built-in"
    user_root = tmp_path / "data" / "pets"
    built_in = write_pet_choice(
        built_in_root,
        "ocean-girl",
        display_name="海洋少女",
    )
    write_pet_choice(
        user_root,
        "forest-cat",
        display_name="森林猫",
    )
    write_pet_choice(
        user_root,
        "ocean-girl",
        display_name="重复形象",
    )
    write_pet_choice(
        user_root,
        "wrong-directory",
        pet_id="different-id",
        display_name="改名形象",
    )
    write_pet_choice(user_root, "old-version", version=1)
    escaping = write_pet_choice(
        user_root,
        "escaping",
        sprite_path="../outside.webp",
    )
    (escaping.parent / "outside.webp").write_bytes(b"outside")
    malformed = user_root / "malformed"
    malformed.mkdir()
    (malformed / "pet.json").write_text("{", encoding="utf-8")

    choices = discover_pet_choices(
        tmp_path / "data",
        built_in_root=built_in_root,
    )

    assert choices == (
        PetChoice("ocean-girl", "海洋少女", built_in.resolve(), True),
        PetChoice(
            "different-id", "改名形象", (user_root / "wrong-directory").resolve(), False,
        ),
        PetChoice(
            "forest-cat",
            "森林猫",
            (user_root / "forest-cat").resolve(),
            False,
        ),
        PetChoice("old-version", "测试形象", (user_root / "old-version").resolve(), False),
    )
    assert [choice.label for choice in choices] == [
        "海洋少女（ocean-girl）",
        "改名形象（different-id）",
        "森林猫（forest-cat）",
        "测试形象（old-version）",
    ]


def test_active_pet_resolution_prefers_user_install_and_rejects_traversal(tmp_path):
    user_pet = tmp_path / "pets" / "custom"
    user_pet.mkdir(parents=True)

    assert resolve_active_pet_directory("custom", tmp_path) == user_pet.resolve()
    assert resolve_active_pet_directory("../outside", tmp_path) == Path(
        "assets/pet/kawakaze"
    ).resolve()
