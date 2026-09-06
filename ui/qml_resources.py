"""QML 与本地视觉素材的可信路径边界"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QUrl


class QmlResourceError(RuntimeError):
    """QML 资源路径无效或越过允许边界"""


def qml_root() -> Path:
    """返回源码或 PyInstaller 冻结环境中的 QML 根目录"""

    if getattr(sys, "frozen", False):
        frozen_root = getattr(sys, "_MEIPASS", None)
        if not frozen_root:
            raise QmlResourceError("冻结资源根目录不可用")
        return Path(frozen_root).resolve() / "ui" / "qml"
    return Path(__file__).resolve().parent / "qml"


def ui_assets_root() -> Path:
    """返回源码或 PyInstaller 冻结环境中的界面素材根目录"""

    if getattr(sys, "frozen", False):
        frozen_root = getattr(sys, "_MEIPASS", None)
        if not frozen_root:
            raise QmlResourceError("冻结资源根目录不可用")
        return Path(frozen_root).resolve() / "assets" / "ui"
    return Path(__file__).resolve().parent.parent / "assets" / "ui"


def validated_asset_url(
    path: str | Path,
    allowed_roots: Sequence[Path],
) -> QUrl:
    """验证本地文件位于可信根目录后生成 QML URL"""

    try:
        resolved_path = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise QmlResourceError("素材文件不存在或无法解析") from error
    if not resolved_path.is_file():
        raise QmlResourceError("素材路径不是文件")

    for allowed_root in allowed_roots:
        try:
            root = Path(allowed_root).expanduser().resolve(strict=True)
            resolved_path.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        return QUrl.fromLocalFile(str(resolved_path))
    raise QmlResourceError("素材文件不在允许目录中")
