"""桌宠候选目录发现与安全路径解析。"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImageReader

_PET_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MAX_MANIFEST_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class PetChoice:
    """设置页可选择的已验证桌宠形象"""

    pet_id: str
    display_name: str
    directory: Path
    built_in: bool

    @property
    def label(self) -> str:
        return f"{self.display_name}（{self.pet_id}）"


def discover_pet_choices(
    data_root: str | Path,
    *,
    built_in_root: str | Path | None = None,
) -> tuple[PetChoice, ...]:
    """从内置与用户目录读取安全的 v2 桌宠候选"""

    source_roots = (
        (
            Path(built_in_root).expanduser().resolve()
            if built_in_root is not None
            else default_pet_directory().resolve().parent
        ),
        Path(data_root).expanduser().resolve() / "pets",
    )
    choices: list[PetChoice] = []
    seen_ids: set[str] = set()
    for built_in, source_root in ((True, source_roots[0]), (False, source_roots[1])):
        discovered: list[PetChoice] = []
        try:
            directories = tuple(path for path in source_root.iterdir() if path.is_dir())
        except OSError:
            continue
        for directory in directories:
            choice = _read_pet_choice(directory, built_in=built_in)
            if choice is None or choice.pet_id in seen_ids:
                continue
            seen_ids.add(choice.pet_id)
            discovered.append(choice)
        choices.extend(
            sorted(
                discovered,
                key=lambda choice: (
                    choice.display_name.casefold(),
                    choice.pet_id,
                ),
            )
        )
    return tuple(choices)


def _read_pet_choice(directory: Path, *, built_in: bool) -> PetChoice | None:
    root = directory.resolve()
    manifest_path = root / "pet.json"
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            return None
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pet_id = data.get("id")
    display_name = data.get("displayName")
    sprite_path = data.get("spritesheetPath")
    if (
        not isinstance(pet_id, str)
        or _PET_ID_PATTERN.fullmatch(pet_id) is None
        or not isinstance(display_name, str)
        or not display_name.strip()
        or not isinstance(sprite_path, str)
        or not sprite_path.strip()
    ):
        return None
    sheet = (root / sprite_path).resolve()
    try:
        sheet.relative_to(root)
    except ValueError:
        return None
    if not sheet.is_file():
        return None
    # 发现列表与加载器使用相同布局门槛，不再依赖可缺省的版本号
    reader = QImageReader(str(sheet))
    size = reader.size()
    if (size.width(), size.height()) not in {(1536, 1872), (1536, 2288)}:
        return None
    return PetChoice(pet_id, display_name.strip(), root, built_in)


def default_pet_directory() -> Path:
    """返回源码或冻结包中的内置桌宠目录"""

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parents[1]))
    return bundle_root / "assets" / "pet" / "kawakaze"


def resolve_active_pet_directory(
    active_skin: str,
    data_root: str | Path,
) -> Path:
    """优先选择用户形象并在名称异常或缺失时回退内置形象"""

    fallback = default_pet_directory().resolve()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", active_skin) is None:
        return fallback
    user_root = Path(data_root).expanduser().resolve() / "pets"
    user_pet = (user_root / active_skin).resolve()
    if user_pet.parent == user_root and user_pet.is_dir():
        return user_pet
    built_in = (fallback.parent / active_skin).resolve()
    if built_in.parent == fallback.parent and built_in.is_dir():
        return built_in
    # 手动解压的外层目录可能带版本后缀，按清单标识恢复形象
    for choice in discover_pet_choices(data_root):
        if choice.pet_id == active_skin:
            return choice.directory
    return fallback
