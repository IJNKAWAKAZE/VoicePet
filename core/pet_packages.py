"""Codex v2 桌宠包的有界校验与原子安装"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


class PetPackageError(RuntimeError):
    """桌宠包来源、内容、安全边界或安装失败"""

    code = "pet.package"


class PetPackageInstaller:
    """在临时目录完整校验后安装新的用户桌宠"""

    def __init__(
        self,
        pets_directory: str | Path,
        *,
        max_archive_bytes: int = 32 * 1024 * 1024,
        max_files: int = 32,
        max_expanded_bytes: int = 64 * 1024 * 1024,
        max_pixels: int = 4_000_000,
    ) -> None:
        if min(max_archive_bytes, max_files, max_expanded_bytes, max_pixels) <= 0:
            raise PetPackageError("桌宠包限制必须大于零")
        self._pets_directory = Path(pets_directory).expanduser().resolve()
        self._max_archive_bytes = max_archive_bytes
        self._max_files = max_files
        self._max_expanded_bytes = max_expanded_bytes
        self._max_pixels = max_pixels

    def validate_directory(self, source: str | Path) -> str:
        """只读验证已展开桌宠目录并返回形象标识"""

        source_path = Path(source).expanduser().resolve()
        if not source_path.is_dir():
            raise PetPackageError("桌宠校验来源必须是目录")
        pet_id, _ = self._validate_package(source_path)
        return pet_id

    def install(self, source: str | Path) -> Path:
        """安装目录或 ZIP 形式的桌宠包且不覆盖现有形象"""

        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise PetPackageError("桌宠包不存在")
        self._pets_directory.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=".pet-install-", dir=self._pets_directory)
        ).resolve()
        try:
            unpacked = staging / "unpacked"
            unpacked.mkdir()
            if source_path.is_dir():
                self._copy_directory(source_path, unpacked)
            elif source_path.is_file():
                self._extract_archive(source_path, unpacked)
            else:
                raise PetPackageError("桌宠包来源类型无效")
            package_root = self._find_package_root(unpacked)
            pet_id, sheet_path = self._validate_package(package_root)
            target = (self._pets_directory / pet_id).resolve()
            if target.parent != self._pets_directory or target.exists():
                raise PetPackageError("同名桌宠已存在或目标路径无效")
            prepared = staging / pet_id
            prepared.mkdir()
            shutil.copy2(package_root / "pet.json", prepared / "pet.json")
            shutil.copy2(sheet_path, prepared / sheet_path.name)
            os.replace(prepared, target)
            return target
        except PetPackageError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            raise PetPackageError("桌宠包安装失败") from error
        finally:
            self._remove_staging(staging)

    def _copy_directory(self, source: Path, destination: Path) -> None:
        files = [path for path in source.rglob("*") if path.is_file()]
        if any(path.is_symlink() for path in source.rglob("*")):
            raise PetPackageError("桌宠目录不能包含符号链接")
        total = sum(path.stat().st_size for path in files)
        if len(files) > self._max_files or total > self._max_expanded_bytes:
            raise PetPackageError("桌宠目录超过文件数量或大小限制")
        for path in files:
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    def _extract_archive(self, source: Path, destination: Path) -> None:
        if source.stat().st_size > self._max_archive_bytes:
            raise PetPackageError("桌宠压缩包超过大小限制")
        with zipfile.ZipFile(source) as archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            if len(files) > self._max_files:
                raise PetPackageError("桌宠压缩包文件数量过多")
            if sum(info.file_size for info in files) > self._max_expanded_bytes:
                raise PetPackageError("桌宠压缩包展开大小过大")
            for info in files:
                relative = self._safe_archive_path(info)
                target = (destination / Path(*relative.parts)).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError as error:
                    raise PetPackageError("桌宠压缩包包含越界路径") from error
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as input_file, target.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file)

    @staticmethod
    def _safe_archive_path(info: zipfile.ZipInfo) -> PurePosixPath:
        name = info.filename.replace("\\", "/")
        relative = PurePosixPath(name)
        mode = info.external_attr >> 16
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or ":" in relative.parts[0]
            or stat.S_ISLNK(mode)
            or info.flag_bits & 0x1
        ):
            raise PetPackageError("桌宠压缩包包含不安全路径或条目")
        return relative

    @staticmethod
    def _find_package_root(unpacked: Path) -> Path:
        manifests = tuple(unpacked.rglob("pet.json"))
        if len(manifests) != 1:
            raise PetPackageError("桌宠包必须包含唯一 pet.json")
        return manifests[0].parent

    def _validate_package(self, package_root: Path) -> tuple[str, Path]:
        try:
            data = json.loads((package_root / "pet.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PetPackageError("桌宠清单无法读取") from error
        required = {
            "id",
            "displayName",
            "description",
            "spriteVersionNumber",
            "spritesheetPath",
        }
        if not isinstance(data, dict) or not required.issubset(data):
            raise PetPackageError("桌宠清单缺少必要字段")
        pet_id = data["id"]
        if not isinstance(pet_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", pet_id) is None:
            raise PetPackageError("桌宠 ID 格式无效")
        if data["spriteVersionNumber"] != 2:
            raise PetPackageError("桌宠图集版本必须为 2")
        for field in ("displayName", "description", "spritesheetPath"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise PetPackageError("桌宠清单文本字段无效")
        sheet_path = (package_root / data["spritesheetPath"]).resolve()
        try:
            sheet_path.relative_to(package_root.resolve())
        except ValueError as error:
            raise PetPackageError("桌宠图集路径超出形象目录") from error
        if not sheet_path.is_file():
            raise PetPackageError("桌宠图集文件不存在")
        self._validate_image(sheet_path)
        return pet_id, sheet_path

    def _validate_image(self, path: Path) -> None:
        try:
            from PIL import Image
        except (ImportError, ModuleNotFoundError) as error:
            raise PetPackageError("缺少桌宠图像校验依赖") from error
        try:
            with Image.open(path) as image:
                if image.size != (1536, 2288):
                    raise PetPackageError("桌宠图集尺寸必须为 1536x2288")
                if image.width * image.height > self._max_pixels:
                    raise PetPackageError("桌宠图集像素数量过大")
                rgba = image.convert("RGBA")
                if rgba.getchannel("A").getextrema()[0] == 255:
                    raise PetPackageError("桌宠图集必须包含透明背景")
                if rgba.crop((0, 0, 1536, 208)).getchannel("A").getbbox() is None:
                    raise PetPackageError("桌宠 idle 行不能为空")
        except PetPackageError:
            raise
        except (OSError, ValueError) as error:
            raise PetPackageError("桌宠图集无法解码") from error

    def _remove_staging(self, staging: Path) -> None:
        if staging.parent != self._pets_directory or not staging.name.startswith(
            ".pet-install-"
        ):
            raise PetPackageError("桌宠临时目录安全校验失败")
        shutil.rmtree(staging, ignore_errors=True)
