"""VoicePet Qt 主进程与 Agent Worker 的统一入口"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


def _run_ui(smoke_test: bool) -> int:
    if smoke_test and getattr(sys, "frozen", False):
        resource_status = _run_frozen_smoke()
        if resource_status != 0:
            return resource_status
    from core.config import default_config_path
    from core.memory_reset import InstanceBusyError, InstanceGuard, show_startup_notice
    from core.memory_schema import MemorySchemaError
    from ui.application import run_ui

    try:
        with InstanceGuard(default_config_path().parent):
            return run_ui(smoke_test=smoke_test)
    except InstanceBusyError as error:
        show_startup_notice(str(error))
        return 1
    except MemorySchemaError:
        show_startup_notice("测试记忆结构需要初始化。请退出程序后运行 VoicePet.exe --reset-test-memory，再正常启动")
        return 1
    except OSError:
        show_startup_notice("无法访问 VoicePet 数据目录，请检查目录是否存在以及文件权限")
        return 1


def _run_frozen_smoke() -> int:
    """不初始化 Qt 即验证冻结资源和用户数据目录"""

    bundle_root = Path(getattr(sys, "_MEIPASS", ""))
    local_app_data = os.environ.get("LOCALAPPDATA")
    required = (
        bundle_root / "ui" / "qml" / "Main.qml",
        bundle_root / "assets" / "ui" / "brand" / "voicepet-mark.svg",
        bundle_root
        / "assets"
        / "ui"
        / "themes"
        / "sunny_sea"
        / "welcome.webp",
        bundle_root / "assets" / "pet" / "dpsk-girl" / "pet.json",
        bundle_root / "assets" / "pet" / "dpsk-girl" / "spritesheet.webp",
    )
    if not bundle_root.is_dir() or not local_app_data:
        return 3
    if not all(path.is_file() for path in required):
        return 4
    try:
        from codex_cli_bin import bundled_codex_path
        if not bundled_codex_path().is_file():
            return 6
    except (ImportError, OSError):
        return 6
    data_root = Path(local_app_data) / "VoicePet"
    try:
        data_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 5
    return 0


def _run_agent_worker() -> int:
    """进入独立 Agent Worker 进程，避免加载 Qt 主线程"""

    import asyncio
    import json
    import os

    from core.agent_codex import (
        AgentCodexCompatibilityError,
        CodexAgentAdapter,
        require_supported_sdk,
    )
    from core.agent_worker import run_agent_worker

    try:
        require_supported_sdk()
    except AgentCodexCompatibilityError:
        return 2
    try:
        # 冻结版无控制台时 sys.stdin 为空，直接使用父进程传入的匿名管道
        input_stream = os.fdopen(os.dup(0), "rb", buffering=0)
        output_stream = os.fdopen(os.dup(1), "wb", buffering=0)
        bootstrap = json.loads(input_stream.readline(1024 * 1024 + 1))
        adapter = CodexAgentAdapter(
            api_key=bootstrap["api_key"],
            data_directory=bootstrap["data_directory"],
            model=bootstrap["model"],
            base_url=bootstrap.get("base_url", ""),
            reasoning_effort=bootstrap.get("reasoning_effort", "low"),
            system_prompt=bootstrap.get("system_prompt", ""),
        )
        asyncio.run(run_agent_worker(lambda: adapter, input_stream, output_stream))
        return 0
    except (KeyError, TypeError, ValueError, AgentCodexCompatibilityError):
        return 2


def _run_memory_reset() -> int:
    from core.memory_reset import reset_memory_entry

    return reset_memory_entry()


def main(
    argv: Sequence[str] | None = None,
    *,
    ui_entry: Callable[[bool], int] = _run_ui,
    agent_worker_entry: Callable[[], int] = _run_agent_worker,
    reset_entry: Callable[[], int] = _run_memory_reset,
) -> int:
    """调用冻结支持并按互斥模式分发进程职责"""

    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="VoicePet")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--agent-worker", action="store_true")
    modes.add_argument("--smoke-test", action="store_true")
    modes.add_argument("--reset-test-memory", action="store_true")
    modes.add_argument("--mcp-server", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.agent_worker:
        return agent_worker_entry()
    if arguments.mcp_server:
        from core.mcp_server import serve
        serve()
        return 0
    if arguments.reset_test_memory:
        return reset_entry()
    return ui_entry(arguments.smoke_test)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
