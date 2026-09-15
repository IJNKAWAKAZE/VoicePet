"""Agent Worker 的 JSONL 生命周期服务"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, BinaryIO

from .agent_rpc import (
    AgentRpcNotification,
    AgentRpcRequest,
    AgentRpcResponse,
    decode_message,
    encode_message,
)
from .agent_types import AgentEvent, AgentEventType, AgentTurnRequest


class AgentWorkerError(RuntimeError):
    code = "agent.worker"


class AgentWorkerServer:
    """Worker 按轮次标识并发执行多个会话并优先处理控制请求"""

    def __init__(self, adapter: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._adapter = adapter
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    async def serve(self) -> None:
        try:
            while not self._closed:
                line = await self._reader.readline()
                if not line:
                    break
                message = None
                try:
                    message = decode_message(line)
                    if not isinstance(message, AgentRpcRequest):
                        raise ValueError("Worker 只接受请求")  # noqa: TRY004
                    result = await self._handle(message)
                    await self._send(AgentRpcResponse(message.request_id, result=result))
                except Exception as error:  # noqa: BLE001 仅本地校验错误允许返回正文
                    detail = str(error) if isinstance(error, AgentWorkerError) else f"Agent Worker 请求失败（{type(error).__name__}）"
                    await self._send(AgentRpcResponse(message.request_id if isinstance(message, AgentRpcRequest) else "invalid", error={"code": -32000, "message": detail or "Agent Worker 请求失败"}))
        finally:
            await self._cancel_all_turns()
            self._closed = True
            await self._adapter.close()

    async def _cancel_all_turns(self) -> None:
        tasks = [task for task in self._turn_tasks.values() if not task.done()]
        self._turn_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle(self, request: AgentRpcRequest) -> dict[str, Any]:
        if request.method == "agent.initialize":
            return await self._adapter.initialize()
        if request.method == "agent.turn.start":
            turn = AgentTurnRequest.from_mapping(request.params)
            if turn.turn_id in self._turn_tasks:
                raise AgentWorkerError("Agent 轮次标识重复")
            self._turn_tasks[turn.turn_id] = asyncio.create_task(self._run_turn(turn))
            return {"accepted": True, "turn_id": turn.turn_id}
        if request.method == "agent.turn.cancel":
            self._require_active(request.params.get("turn_id"))
            await self._adapter.cancel(request.params["turn_id"])
            return {"accepted": True}
        if request.method == "agent.approval.resolve":
            approval_id = request.params.get("approval_id")
            decision = request.params.get("decision")
            resolver = getattr(self._adapter, "resolve_interaction", None)
            if callable(resolver) and isinstance(approval_id, str) and isinstance(decision, str):
                if not resolver(approval_id, decision):
                    raise AgentWorkerError("审批请求无效")
                return {"accepted": True}
            raise AgentWorkerError("审批请求无效")
        if request.method == "agent.shutdown":
            for turn_id in tuple(self._turn_tasks):
                await self._adapter.cancel(turn_id)
            self._closed = True
            return {"accepted": True}
        raise AgentWorkerError("Agent 方法无效")

    async def _run_turn(self, turn: AgentTurnRequest) -> None:
        try:
            async for event in self._adapter.run_turn(turn):
                await self._send(AgentRpcNotification("agent.event", event.to_mapping()))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            await self._send(AgentRpcNotification("agent.event", AgentEvent(turn.session_id, turn.turn_id, 0, AgentEventType.ERROR, {"code": "agent.turn"}).to_mapping()))
        finally:
            self._turn_tasks.pop(turn.turn_id, None)

    def _require_active(self, turn_id: object) -> None:
        if not isinstance(turn_id, str) or turn_id not in self._turn_tasks:
            raise AgentWorkerError("Agent 轮次不匹配")

    async def _send(self, message: AgentRpcResponse | AgentRpcNotification) -> None:
        data = encode_message(message)
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()


async def run_agent_worker(adapter_factory: Callable[[], Any], input_stream: BinaryIO, output_stream: BinaryIO) -> None:
    loop = asyncio.get_running_loop()
    reader = _PipeReader(input_stream)
    writer = _PipeWriter(output_stream)
    adapter = adapter_factory()
    server = AgentWorkerServer(adapter, reader, writer)
    setter = getattr(adapter, "set_interaction_callback", None)
    if callable(setter):
        def publish_interaction(payload: dict[str, Any]) -> bool:
            event = AgentEvent(str(payload["session_id"]), str(payload["turn_id"]), 0, AgentEventType.APPROVAL_REQUEST, payload)
            future = asyncio.run_coroutine_threadsafe(server._send(AgentRpcNotification("agent.event", event.to_mapping())), loop)
            try:
                future.result(timeout=5)
                return True
            except (TimeoutError, OSError):
                return False
        setter(publish_interaction)
    await server.serve()


class _PipeReader:
    """匿名管道通过线程读取，兼容 Windows 非重叠标准句柄"""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    async def readline(self) -> bytes:
        return await asyncio.to_thread(self._stream.readline, 1024 * 1024 + 1)


class _PipeWriter:
    """保留发送锁内的完整帧并在线程中写入管道"""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._pending = b""

    def write(self, data: bytes) -> None:
        self._pending += data

    async def drain(self) -> None:
        data, self._pending = self._pending, b""
        await asyncio.to_thread(self._flush, data)

    def _flush(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = self._stream.write(view)
            if not written:
                raise BrokenPipeError("Agent 输出管道已关闭")
            view = view[written:]
        self._stream.flush()




