"""执行前准备差异并复核文件状态的 Agent 动态工具"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .agent_codex import dynamic_tool_specs
from .agent_types import freeze_json, thaw_json

MAX_FILE_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024


class AgentActionError(RuntimeError):
    code = "agent.action"


def classify_dynamic_action(tool, arguments):
    if tool == "voicepet_shell":
        return "command"
    if tool != "voicepet_file_change":
        raise AgentActionError("Agent 动态工具无效")
    try:
        return {"create": "file_create", "edit": "file_patch", "replace": "file_replace", "delete": "file_delete"}[arguments.get("operation")]
    except (KeyError, TypeError):
        raise AgentActionError("Agent 文件操作无效") from None


def _path(value):
    path = Path(os.path.abspath(value))
    # 所有父目录一起检查，避免通过链接和 Windows 联接跳转目标
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AgentActionError("不允许操作重解析路径")
    return path


def _read(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise AgentActionError("目标不是允许的文本文件")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
        raise AgentActionError("文件过大或不是文本")
    return raw, raw.decode("utf-8")


@dataclass(frozen=True, slots=True)
class PreparedAction:
    tool: str
    kind: str
    arguments: Mapping = field(repr=False)
    details: Mapping = field(repr=False)
    content: bytes | None = field(default=None, repr=False)
    old_hash: str | None = None


class AgentActionExecutor:
    """准备阶段不产生副作用，批准后再次检查目标再执行"""

    def prepare(self, tool, arguments):
        try:
            return self._prepare(tool, arguments)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise AgentActionError("操作参数或目标文件无效") from None

    def _prepare(self, tool, arguments):
        from jsonschema import Draft202012Validator
        specs = {item["name"]: item["inputSchema"] for item in dynamic_tool_specs()}
        if tool not in specs:
            raise AgentActionError("Agent 动态工具无效")
        arguments = thaw_json(freeze_json(arguments))
        if not Draft202012Validator(specs[tool]).is_valid(arguments):
            raise AgentActionError("操作参数无效")
        kind = classify_dynamic_action(tool, arguments)
        if kind == "command":
            cwd = _path(arguments["cwd"])
            program = shutil.which(arguments["program"])
            if not program or not cwd.is_dir():
                raise AgentActionError("命令或工作目录不可用")
            arguments = {**arguments, "program": str(Path(program).resolve()), "cwd": str(cwd)}
            details = {"command": subprocess.list2cmdline([arguments["program"], *arguments["args"]]), "cwd": str(cwd), "operation": "command"}
            return PreparedAction(tool, kind, freeze_json(arguments), freeze_json(details))
        path = _path(arguments["path"])
        operation = arguments["operation"]
        old_hash, old_text = None, ""
        if operation == "create":
            if path.exists() or arguments["expected_sha256"] is not None:
                raise AgentActionError("创建目标已存在或校验值无效")
        else:
            raw, old_text = _read(path)
            old_hash = hashlib.sha256(raw).hexdigest()
            if arguments["expected_sha256"] != old_hash:
                raise AgentActionError("文件校验值缺失或已过期，请重新读取")
        content = arguments["content"]
        if operation == "edit":
            if not arguments["edits"] or content:
                raise AgentActionError("编辑必须提供非空替换列表")
            content = old_text
            for edit in arguments["edits"]:
                old = edit["old_text"]
                count = content.count(old)
                if not old or not count or (not edit["replace_all"] and count != 1):
                    raise AgentActionError("编辑目标缺失或不唯一")
                content = content.replace(old, edit["new_text"], -1 if edit["replace_all"] else 1)
        elif arguments["edits"] or (operation == "delete" and content):
            raise AgentActionError("文件操作包含不适用的字段")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES or b"\x00" in encoded:
            raise AgentActionError("文件内容过大或不是文本")
        diff = "".join(difflib.unified_diff(old_text.splitlines(keepends=True), content.splitlines(keepends=True), fromfile=str(path), tofile=str(path)))
        details = {"operation": operation, "path": str(path), "diff": diff[:160_000], "truncated": len(diff) > 160_000}
        return PreparedAction(tool, kind, freeze_json({**arguments, "path": str(path)}), freeze_json(details), encoded, old_hash)

    async def execute(self, action):
        if action.kind == "command":
            return await self._shell(action)
        # 单个文本文件最多 512 KiB，复核到写入间不跨异步等待点
        return self._file(action)

    def _file(self, action):
        temporary = None
        try:
            path = _path(action.arguments["path"])
            operation = action.arguments["operation"]
            if action.old_hash is not None:
                raw, _ = _read(path)
                if hashlib.sha256(raw).hexdigest() != action.old_hash:
                    raise AgentActionError("文件在审批期间已变化")
            elif path.exists():
                raise AgentActionError("创建目标在审批期间已存在")
            if operation == "delete":
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(prefix=".voicepet-", dir=path.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(action.content)
                    output.flush()
                    os.fsync(output.fileno())
                if operation == "create":
                    # 硬链接要求目标不存在，避免原子替换覆盖并发创建的文件
                    os.link(temporary, path)
                else:
                    os.replace(temporary, path)
            return {"status": "completed", "operation": operation, "path": str(path)}
        except (OSError, UnicodeError):
            raise AgentActionError("文件执行失败") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _shell(self, action):
        from .windows_job import WindowsJob
        args = action.arguments
        started = time.monotonic()
        job = WindowsJob() if os.name == "nt" else None
        process = None
        async def collect(stream):
            output = bytearray()
            truncated = False
            while chunk := await stream.read(16_384):
                remaining = MAX_OUTPUT_BYTES - len(output)
                output.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining
            return output.decode("utf-8", errors="replace"), truncated
        try:
            process = await asyncio.create_subprocess_exec(args["program"], *args["args"], cwd=args["cwd"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, start_new_session=os.name != "nt")
            if job:
                job.assign(process.pid)
            output_tasks = [asyncio.create_task(collect(process.stdout)), asyncio.create_task(collect(process.stderr))]
            timed_out = False
            try:
                async with asyncio.timeout(args["timeout"]):
                    await process.wait()
                    outputs = await asyncio.gather(*output_tasks)
            except TimeoutError:
                timed_out = True
                self._kill(process, job)
                outputs = await asyncio.gather(*output_tasks)
            except BaseException:
                self._kill(process, job)
                await asyncio.gather(*output_tasks, return_exceptions=True)
                raise
            return {"returncode": process.returncode, "stdout": outputs[0][0], "stderr": outputs[1][0], "truncated": outputs[0][1] or outputs[1][1], "timed_out": timed_out, "duration_ms": round((time.monotonic()-started)*1000)}
        except OSError:
            raise AgentActionError("命令启动失败") from None
        finally:
            if process:
                self._kill(process, job)
                await process.wait()
            if job:
                job.close()

    @staticmethod
    def _kill(process, job):
        if job:
            job.close()
        elif os.name != "nt":
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

