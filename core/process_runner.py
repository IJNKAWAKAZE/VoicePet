"""受控工具使用的有界异步子进程执行器"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .cancellation import CancellationToken
from .tool_types import ToolConfigurationError, ToolExecutionError


class ProcessConfigurationError(ToolConfigurationError):
    """本地子进程请求参数无效"""

    code = "tool.process_configuration"


class ProcessStartError(ToolExecutionError):
    """操作系统拒绝或无法启动子进程"""

    code = "tool.process_start"


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """内存受限的子进程退出信息"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool


class AsyncSubprocessRunner:
    """只通过 exec 语义启动程序并约束生命周期和输出"""

    def __init__(self, *, terminate_grace: float = 0.5) -> None:
        if terminate_grace <= 0:
            raise ProcessConfigurationError("进程终止宽限期必须大于零")
        self._terminate_grace = terminate_grace

    async def run(
        self,
        *,
        program: Path,
        args: Sequence[str],
        cwd: Path,
        timeout: float,
        output_limit: int,
        token: CancellationToken,
        environment: Mapping[str, str] | None = None,
    ) -> ProcessOutcome:
        resolved_program, resolved_cwd, normalized_args = self._validate_request(
            program,
            args,
            cwd,
            timeout,
            output_limit,
            environment,
        )
        token.throw_if_cancelled()
        try:
            process = await asyncio.create_subprocess_exec(
                str(resolved_program),
                *normalized_args,
                cwd=str(resolved_cwd),
                env=None if environment is None else dict(environment),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise ProcessStartError(
                f"子进程启动失败: {type(error).__name__}"
            ) from error

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            self._drain(process.stdout, output_limit)
        )
        stderr_task = asyncio.create_task(
            self._drain(process.stderr, output_limit)
        )
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(token.wait())
        timed_out = False
        cancelled = False
        try:
            done, _ = await asyncio.wait(
                (wait_task, cancel_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                cancelled = True
                await self._terminate(process)
            elif wait_task not in done:
                timed_out = True
                await self._terminate(process)
            else:
                await wait_task
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

        stdout_result, stderr_result = await asyncio.gather(
            stdout_task,
            stderr_task,
        )
        if cancelled:
            token.throw_if_cancelled()
        exit_code = process.returncode
        assert exit_code is not None
        return ProcessOutcome(
            exit_code,
            stdout_result[0].decode("utf-8", errors="replace"),
            stderr_result[0].decode("utf-8", errors="replace"),
            timed_out,
            stdout_result[1] or stderr_result[1],
        )

    @staticmethod
    def _validate_request(
        program: Path,
        args: Sequence[str],
        cwd: Path,
        timeout: float,
        output_limit: int,
        environment: Mapping[str, str] | None,
    ) -> tuple[Path, Path, tuple[str, ...]]:
        program = Path(program)
        cwd = Path(cwd)
        if not program.is_absolute():
            raise ProcessConfigurationError("程序路径必须是绝对路径")
        try:
            resolved_program = program.resolve(strict=True)
        except OSError as error:
            raise ProcessConfigurationError("程序路径不存在") from error
        if not resolved_program.is_file():
            raise ProcessConfigurationError("程序路径必须是文件")
        if not cwd.is_absolute():
            raise ProcessConfigurationError("工作目录必须是绝对路径")
        try:
            resolved_cwd = cwd.resolve(strict=True)
        except OSError as error:
            raise ProcessConfigurationError("工作目录不存在") from error
        if not resolved_cwd.is_dir():
            raise ProcessConfigurationError("工作目录必须是目录")
        normalized_args = tuple(args)
        if any(not isinstance(argument, str) for argument in normalized_args):
            raise ProcessConfigurationError("程序参数必须全部是字符串")
        if timeout <= 0:
            raise ProcessConfigurationError("进程超时必须大于零")
        if output_limit <= 0:
            raise ProcessConfigurationError("进程输出上限必须大于零")
        if environment is not None and any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ProcessConfigurationError("进程环境必须是字符串映射")
        return resolved_program, resolved_cwd, normalized_args

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._terminate_grace)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    async def _drain(
        stream: asyncio.StreamReader,
        limit: int,
    ) -> tuple[bytes, bool]:
        retained = bytearray()
        truncated = False
        while chunk := await stream.read(4096):
            remaining = limit - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(retained), truncated
