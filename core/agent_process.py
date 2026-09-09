"""Agent Worker 子进程、凭据启动和进程树生命周期"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from .agent_client import AgentWorkerClient
from .windows_job import WindowsJob, WindowsJobError


class AgentProcessError(RuntimeError):
    code="agent.process"
class AgentWorkerProcessManager:
    def __init__(self, *, api_key: str, data_directory: str|Path, model: str, base_url: str="", reasoning_effort: str="low", system_prompt: str="", python_executable: str|None=None):
        if not api_key or "\n" in api_key or "\r" in api_key: raise ValueError("Agent API Key 无效")
        self._bootstrap={"api_key":api_key,"data_directory":str(Path(data_directory).resolve()),"model":model,"reasoning_effort":reasoning_effort,"system_prompt":system_prompt}
        self._bootstrap["base_url"] = base_url
        self._python=python_executable or sys.executable; self._process=None; self._job=None; self._client=None
    async def start(self)->AgentWorkerClient:
        if self._client is not None: return self._client
        command=[self._python,"--agent-worker"] if getattr(sys,"frozen",False) else [self._python,"-u",str(Path(__file__).resolve().parent.parent / "app.py"),"--agent-worker"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=os.name!="nt", creationflags=creationflags)
        if os.name=="nt":
            try: self._job=WindowsJob(); self._job.assign(self._process.pid)
            except WindowsJobError as error: await self.stop(force=True); raise AgentProcessError("无法接管 Agent 进程树") from error
        assert self._process.stdin and self._process.stdout
        self._process.stdin.write((json.dumps(self._bootstrap,ensure_ascii=False,separators=(",",":"))+"\n").encode()); await self._process.stdin.drain()
        # 初始化失败后允许重试，凭据仍仅通过匿名管道传递
        self._client=AgentWorkerClient(self._process.stdout,self._process.stdin)
        try: await asyncio.wait_for(self._client.initialize(),30)
        except Exception:  # noqa: BLE001
            await self.stop(force=True)
            raise AgentProcessError("Agent Worker 初始化失败") from None
        return self._client
    async def stop(self, *, force: bool=False)->None:
        if self._client is not None and not force:
            try: await asyncio.wait_for(self._client.close(),5)
            except (AgentProcessError, OSError, TimeoutError): force=True
        if self._process is not None:
            if force and self._process.returncode is None:
                self._process.kill()
            try: await asyncio.wait_for(self._process.wait(),2)
            except TimeoutError: self._process.kill(); await self._process.wait()
        if self._job is not None: self._job.close()
        self._client=None; self._process=None; self._job=None



