"""主进程侧 Tool Worker 匿名管道与崩溃生命周期"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .audit import AuditContext
from .cancellation import CancellationToken
from .policy import PolicyDecision, ToolProposal
from .tool_client import ToolWorkerClient
from .tool_types import ToolExecutionResult
from .worker_bootstrap import WorkerBootstrap, encode_bootstrap


class WorkerProcessError(RuntimeError):
    """Tool Worker 启动、崩溃或重启限制错误"""

    code = "worker.process"


class ToolWorkerProcessManager:
    """通过匿名管道启动 Worker 且从不重放 execute"""

    def __init__(
        self,
        bootstrap: WorkerBootstrap,
        *,
        command: Sequence[str] | None = None,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 2.0,
        restart_limit: int = 3,
        auto_restart: bool = True,
        restart_backoff: float = 0.5,
    ) -> None:
        if (
            startup_timeout <= 0
            or shutdown_timeout <= 0
            or restart_limit <= 0
            or restart_backoff <= 0
        ):
            raise WorkerProcessError("Worker 生命周期限制必须大于零")
        if type(auto_restart) is not bool:
            raise WorkerProcessError("Worker 自动重启开关类型无效")
        self._bootstrap = bootstrap
        self._command = tuple(command or self._default_command())
        secret_forms = {bootstrap.secret.hex(), bootstrap.secret.decode("ascii", errors="ignore")}
        if any(secret and secret in part for part in self._command for secret in secret_forms):
            raise WorkerProcessError("Worker 命令行不能包含会话密钥")
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
        self._restart_limit = restart_limit
        self._auto_restart = auto_restart
        self._restart_backoff = restart_backoff
        self._process: asyncio.subprocess.Process | None = None
        self._client: ToolWorkerClient | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._crash_count = 0

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def is_available(self) -> bool:
        return self._crash_count < self._restart_limit

    @property
    def crash_count(self) -> int:
        return self._crash_count

    async def start(self) -> None:
        if self.is_running:
            return
        if not self.is_available:
            raise WorkerProcessError("Tool Worker 重启次数已达上限")
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as error:
            raise WorkerProcessError("Tool Worker 启动失败") from error
        assert process.stdin is not None
        assert process.stdout is not None
        self._process = process
        self._client = ToolWorkerClient(process.stdout, process.stdin)
        try:
            process.stdin.write(encode_bootstrap(self._bootstrap))
            await process.stdin.drain()
            async with asyncio.timeout(self._startup_timeout):
                await self._client.ping()
        except Exception as error:
            await self._terminate_process()
            raise WorkerProcessError("Tool Worker 就绪检查失败") from error
        self._monitor_task = asyncio.create_task(
            self._monitor_process(process),
            name=f"voicepet-tool-worker-monitor-{process.pid}",
        )

    async def ping(self):
        if not self.is_running or self._client is None:
            raise WorkerProcessError("Tool Worker 当前不可用")
        return await self._client.ping()

    async def execute(
        self,
        proposal: ToolProposal,
        decision: PolicyDecision,
        authorization: str | None,
        token: CancellationToken,
        *,
        audit_context: AuditContext | None = None,
    ) -> ToolExecutionResult:
        if not self.is_running or self._client is None:
            raise WorkerProcessError("Tool Worker 当前不可用")
        return await self._client.execute(
            proposal,
            decision,
            authorization,
            token,
            audit_context=audit_context,
        )

    async def restart(self) -> None:
        if self.is_running:
            return
        if not self.is_available:
            raise WorkerProcessError("Tool Worker 重启次数已达上限")
        await self.start()

    async def wait_for_exit(self) -> int:
        if self._process is None:
            raise WorkerProcessError("Tool Worker 尚未启动")
        process = self._process
        monitor = self._monitor_task
        code = await process.wait()
        if monitor is not None:
            await asyncio.gather(monitor, return_exceptions=True)
        return code

    async def stop(self) -> None:
        if self._process is None:
            return
        self._stopping = True
        monitor = self._monitor_task
        try:
            if self._client is not None:
                await self._client.close()
            try:
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=self._shutdown_timeout,
                )
            except TimeoutError:
                await self._terminate_process()
        finally:
            if monitor is not None and monitor is not asyncio.current_task():
                if not monitor.done():
                    monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            self._client = None
            self._process = None
            self._monitor_task = None
            self._stopping = False

    def terminate_for_test(self) -> None:
        if self._process is None:
            raise WorkerProcessError("Tool Worker 尚未启动")
        self._process.kill()

    async def _terminate_process(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        self._process.terminate()
        try:
            await asyncio.wait_for(
                self._process.wait(),
                timeout=self._shutdown_timeout,
            )
        except TimeoutError:
            self._process.kill()
            await self._process.wait()

    async def _monitor_process(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        await process.wait()
        if self._stopping or self._process is not process:
            return
        self._crash_count += 1
        if not self._auto_restart or not self.is_available:
            return
        while self.is_available and not self._stopping:
            delay = self._restart_backoff * (2 ** (self._crash_count - 1))
            await asyncio.sleep(delay)
            if self._stopping or self.is_running:
                return
            try:
                await self.start()
            except WorkerProcessError:
                self._crash_count += 1
                continue
            return

    @staticmethod
    def _default_command() -> tuple[str, ...]:
        if getattr(sys, "frozen", False):
            return (sys.executable, "--tool-worker")
        return (sys.executable, str(Path(__file__).parents[1] / "app.py"), "--tool-worker")
