"""持久化不属于用户配置的桌宠窗口状态"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WindowStateError(RuntimeError):
    """桌宠窗口状态保存失败"""


@dataclass(frozen=True, slots=True)
class WindowPosition:
    x: int
    y: int

    def __post_init__(self) -> None:
        if (
            type(self.x) is not int
            or type(self.y) is not int
            or abs(self.x) > 1_000_000
            or abs(self.y) > 1_000_000
        ):
            raise ValueError("桌宠位置坐标无效")


class WindowPositionStore:
    """原子保存桌宠渲染器的屏幕坐标"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> WindowPosition | None:
        if not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"pet_x", "pet_y"}:
                return None
            return WindowPosition(data["pet_x"], data["pet_y"])
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None

    def save(self, position: WindowPosition) -> None:
        if not isinstance(position, WindowPosition):
            raise TypeError("桌宠位置类型无效")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            {"pet_x": position.x, "pet_y": position.y},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self._path)
            temporary_path = None
        except OSError as error:
            raise WindowStateError("桌宠位置保存失败") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
