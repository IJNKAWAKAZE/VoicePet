"""清理 VoicePet 专用 Codex 目录中的线程文件。"""

from __future__ import annotations

from pathlib import Path


class CodexSessionFiles:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().absolute()

    def delete_thread(self, thread_id: str) -> int:
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 256 or any(
            char in thread_id for char in ("/", "\\", "*", "?", "..", "\x00")
        ):
            raise ValueError("Codex 线程 ID 无效")
        if self._root.resolve() != self._root:
            raise OSError("Codex 数据目录不能是链接")
        targets = []
        for name in ("sessions", "archived_sessions"):
            directory = self._root / name
            if directory.resolve() != directory:
                raise OSError("Codex 会话目录不能是链接")
            for path in directory.rglob(f"rollout-*-{thread_id}.jsonl"):
                if path.resolve() != path or not path.is_file():
                    raise OSError("Codex 会话文件路径无效")
                path.resolve().relative_to(directory)
                targets.append(path)
        for path in targets:
            path.unlink(missing_ok=True)
        return len(targets)
