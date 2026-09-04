"""管理当前 Windows 用户的 VoicePet 开机启动项"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "VoicePet"


class StartupError(RuntimeError):
    """开机启动项读写失败"""


def quote_startup_command(command: Sequence[str]) -> str:
    """生成适用于 Windows Run 注册表值的命令行"""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise StartupError("开机启动命令无效")
    return subprocess.list2cmdline(list(command))


def default_startup_command() -> tuple[str, ...]:
    """返回冻结版或源码版当前进程对应的启动命令"""

    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return (str(executable),)
    pythonw = executable.with_name("pythonw.exe")
    launcher = pythonw if pythonw.is_file() else executable
    return (str(launcher), str(Path(__file__).parents[1] / "app.py"))


class WindowsStartupManager:
    """通过当前用户 Run 注册表项管理开机启动"""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        registry: Any | None = None,
    ) -> None:
        if registry is None:
            try:
                import winreg
            except (ImportError, ModuleNotFoundError) as error:
                raise StartupError("当前系统不支持 Windows 开机启动") from error
            registry = winreg
        self._registry = registry
        self._command = quote_startup_command(command or default_startup_command())

    def set_enabled(self, enabled: bool) -> None:
        """启用或移除当前用户开机启动项"""

        if type(enabled) is not bool:
            raise TypeError("开机启动开关必须是布尔值")
        try:
            with self._registry.CreateKeyEx(
                self._registry.HKEY_CURRENT_USER,
                _RUN_KEY,
                0,
                self._registry.KEY_SET_VALUE,
            ) as key:
                if enabled:
                    self._registry.SetValueEx(
                        key,
                        _VALUE_NAME,
                        0,
                        self._registry.REG_SZ,
                        self._command,
                    )
                else:
                    try:
                        self._registry.DeleteValue(key, _VALUE_NAME)
                    except FileNotFoundError:
                        pass
        except OSError as error:
            raise StartupError("开机启动设置写入失败") from error
