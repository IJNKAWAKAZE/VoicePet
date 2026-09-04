"""Tool Worker 固定工具目录的唯一构造入口"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .audit import AuditStore
from .builtin_tools import MoveFileTool, MoveFileUndoTool, SystemInfoTool
from .path_security import ScopedPathResolver
from .tool_types import RegisteredTool, ToolCatalog

WORKER_TOOL_NAMES = ("system_info", "move_file", "undo_move_file")


def build_worker_catalog(
    allowed_roots: Sequence[str | Path],
    audit_store: AuditStore,
) -> ToolCatalog:
    """用允许目录和审计事实源构造固定 Worker 工具目录"""

    resolver = ScopedPathResolver(allowed_roots)
    handlers = (
        SystemInfoTool(),
        MoveFileTool(resolver),
        MoveFileUndoTool(audit_store, resolver),
    )
    return ToolCatalog(
        tuple(RegisteredTool(handler.manifest, handler) for handler in handlers)
    )
