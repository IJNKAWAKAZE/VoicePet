"""VoicePet Qt 主进程与 Tool Worker 的统一入口"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


def _run_ui(smoke_test: bool) -> int:
    if smoke_test and getattr(sys, "frozen", False):
        return _run_frozen_smoke()
    from ui.application import run_ui

    return run_ui(smoke_test=smoke_test)


def _run_frozen_smoke() -> int:
    """不初始化 Qt 即验证冻结资源和用户数据目录"""

    bundle_root = Path(getattr(sys, "_MEIPASS", ""))
    local_app_data = os.environ.get("LOCALAPPDATA")
    required = (
        bundle_root / "assets" / "pet" / "dpsk-girl" / "pet.json",
        bundle_root / "assets" / "pet" / "dpsk-girl" / "spritesheet.webp",
    )
    if not bundle_root.is_dir() or not local_app_data:
        return 3
    if not all(path.is_file() for path in required):
        return 4
    data_root = Path(local_app_data) / "VoicePet"
    try:
        data_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 5
    return 0


def _run_worker() -> int:
    from core.worker_entry import run_worker

    return run_worker()


def main(
    argv: Sequence[str] | None = None,
    *,
    ui_entry: Callable[[bool], int] = _run_ui,
    worker_entry: Callable[[], int] = _run_worker,
) -> int:
    """调用冻结支持并按互斥模式分发进程职责"""

    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="VoicePet")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--tool-worker", action="store_true")
    modes.add_argument("--smoke-test", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.tool_worker:
        return worker_entry()
    return ui_entry(arguments.smoke_test)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
