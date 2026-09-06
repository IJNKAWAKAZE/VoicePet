"""与正常运行共享独占锁的显式测试记忆清理入口"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Self

from .config import default_config_path
from .memory_schema import MemorySchemaError, reset_test_memory_data


class InstanceBusyError(RuntimeError):
    """同一数据目录正在被 VoicePet 主进程使用"""


class InstanceGuard:
    """主窗口与重置命令共同持有的进程锁，进程退出后由系统释放"""

    def __init__(self, data_root: str | Path) -> None:
        self._root = Path(data_root).resolve()
        self._file: BinaryIO | None = None

    def __enter__(self) -> Self:
        self._root.mkdir(parents=True, exist_ok=True)
        file = (self._root / ".voicepet-instance.lock").open("a+b")
        try:
            # Windows 字节锁要求锁定位置存在，锁文件不含用户数据
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            file.close()
            raise InstanceBusyError("VoicePet 正在运行，请先从托盘退出后再操作") from error
        self._file = file
        return self

    def __exit__(self, *exc_info) -> None:
        file, self._file = self._file, None
        if file is not None:
            file.close()


class _ResetDialogs:
    """无需 QML 和 Runtime 的基础窗口反馈"""

    def __init__(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox

        self._app = QApplication.instance() or QApplication([])
        self._message_box = QMessageBox

    def confirm(self, text: str) -> bool:
        box = self._message_box
        return box.question(None, "清理 VoicePet 测试数据", text,
                            box.StandardButton.Yes | box.StandardButton.No,
                            box.StandardButton.No) == box.StandardButton.Yes

    def notify(self, title: str, text: str) -> None:
        self._message_box.information(None, title, text)


def show_startup_notice(message: str) -> None:
    """windowed 程序通过窗口而不是不存在的标准错误流报告原因"""

    _ResetDialogs().notify("VoicePet", message)


def reset_memory_entry(
    *, config_path: Path | None = None, confirm: Callable[[str], bool] | None = None,
    notify: Callable[[str, str], None] | None = None,
) -> int:
    """返回成功 0、失败 1 或取消 2，不启动任何聊天或模型服务"""

    dialogs = _ResetDialogs() if confirm is None or notify is None else None
    confirm = confirm or dialogs.confirm
    notify = notify or dialogs.notify
    root = (config_path or default_config_path()).parent.resolve()
    directory = root / "data"
    database = (directory / "assistant.db").resolve()
    if database.parent != directory or database.name != "assistant.db":
        notify("未清理数据", "数据路径不是应用数据目录内的 assistant.db，已拒绝操作")
        return 1
    if not database.exists():
        notify("无需清理", "当前没有测试会话或记忆数据库")
        return 0
    if not database.is_file():
        notify("未清理数据", "目标不是有效的数据库文件")
        return 1
    confirmed_database = database
    try:
        approved = confirm(
            f"目标：{database}\n\n将永久清除测试聊天、近期摘要和长期记忆，此操作不可恢复。"
            "\n配置、API 凭据、宠物形象、模型和审计记录会保留。\n\n确定清理吗？"
        )
        if not approved:
            return 2
        with InstanceGuard(root):
            if database.resolve() != confirmed_database:
                notify("未清理数据", "确认后的数据库路径发生变化，已拒绝操作")
                return 1
            counts = reset_test_memory_data(database)
    except InstanceBusyError as error:
        notify("请先退出 VoicePet", str(error))
        return 1
    except (MemorySchemaError, OSError):
        notify("清理失败", "测试记忆未能完成初始化，请检查文件权限并确认程序已退出")
        return 1
    removed = sum(counts.get(table, 0) for table in ("memories", "session_turns", "short_term_summaries"))
    notify("清理完成", f"已清除 {removed} 条测试聊天、摘要或记忆，并重建相关表。现在可以正常启动 VoicePet。")
    return 0
