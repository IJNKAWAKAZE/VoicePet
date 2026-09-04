"""本地工具共享的允许根目录路径解析器"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from .tool_types import ToolExecutionError


class PathSecurityError(ToolExecutionError):
    """路径不在允许资源范围或类型不符合要求"""

    code = "tool.path_denied"


class ScopedPathResolver:
    """把未信任路径解析到预先配置的真实根目录内"""

    _EXPANSION_PATTERN = re.compile(r"%[^%]+%|\$env:|\$\(", re.IGNORECASE)
    _WILDCARD_PATTERN = re.compile(r"[*?\[\]]")

    def __init__(self, allowed_roots: Sequence[str | os.PathLike[str]]) -> None:
        if not allowed_roots:
            raise PathSecurityError("至少需要一个允许根目录")
        roots: list[Path] = []
        for raw_root in allowed_roots:
            root = Path(raw_root)
            if not root.is_absolute():
                raise PathSecurityError("允许根目录必须是绝对路径")
            try:
                resolved = root.resolve(strict=True)
            except OSError as error:
                raise PathSecurityError("允许根目录不存在") from error
            if not resolved.is_dir():
                raise PathSecurityError("允许根路径必须是目录")
            roots.append(resolved)
        self._roots = tuple(roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    def existing_file(self, value: str) -> Path:
        path = self._existing(value)
        if not path.is_file():
            raise PathSecurityError("目标必须是普通文件")
        return path

    def directory(self, value: str) -> Path:
        path = self._existing(value)
        if not path.is_dir():
            raise PathSecurityError("目标必须是目录")
        return path

    def new_file(self, value: str) -> Path:
        candidate = self._literal_absolute(value)
        if candidate.exists():
            raise PathSecurityError("目标文件已存在")
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError as error:
            raise PathSecurityError("目标父目录不存在") from error
        if not parent.is_dir():
            raise PathSecurityError("目标父路径必须是目录")
        resolved = parent / candidate.name
        self._ensure_scoped(resolved)
        return resolved

    def _existing(self, value: str) -> Path:
        candidate = self._literal_absolute(value)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise PathSecurityError("目标路径不存在") from error
        self._ensure_scoped(resolved)
        return resolved

    def _literal_absolute(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise PathSecurityError("路径不能为空")
        if (
            value.startswith("~")
            or self._EXPANSION_PATTERN.search(value)
            or self._WILDCARD_PATTERN.search(value)
        ):
            raise PathSecurityError("路径不能包含展开表达式或通配符")
        candidate = Path(value)
        if not candidate.is_absolute():
            raise PathSecurityError("路径必须是绝对路径")
        return candidate

    def _ensure_scoped(self, candidate: Path) -> None:
        candidate_text = os.path.normcase(str(candidate))
        for root in self._roots:
            root_text = os.path.normcase(str(root))
            try:
                if os.path.commonpath((candidate_text, root_text)) == root_text:
                    return
            except ValueError:
                continue
        raise PathSecurityError("路径超出允许资源范围")
