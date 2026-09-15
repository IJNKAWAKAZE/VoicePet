"""主进程中的 Agent Worker 异步客户端"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import uuid4

from .agent_rpc import (
    AgentRpcNotification,
    AgentRpcRequest,
    AgentRpcResponse,
    decode_message,
    encode_message,
)
from .agent_types import AgentCapabilities, AgentEvent, AgentTurnRequest


class AgentClientError(RuntimeError):
    code = "agent.client"


class AgentWorkerClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._pending: dict[str, asyncio.Future[Mapping[str, object]]] = {}
        self._events: asyncio.Queue[object] = asyncio.Queue(maxsize=256)
        # 多会话并行时每个轮次独占自己的事件队列，避免并发轮次互相偷取事件
        self._turn_events: dict[str, asyncio.Queue[object]] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._failure: AgentClientError | None = None
        self._reader_task = asyncio.create_task(self._read_loop())

    async def initialize(self) -> AgentCapabilities:
        return AgentCapabilities.from_mapping(await self._call("agent.initialize", {"protocol_version": 1}))

    async def start_turn(self, request: AgentTurnRequest) -> None:
        self._turn_events[request.turn_id] = asyncio.Queue(maxsize=256)
        await self._call("agent.turn.start", request.to_mapping())

    async def cancel_turn(self, turn_id: str) -> None:
        await self._call("agent.turn.cancel", {"turn_id": turn_id, "reason": "user_cancelled"})

    def release_turn(self, turn_id: str) -> None:
        """轮次结束后释放它的事件队列，避免并发会话堆积"""

        self._turn_events.pop(turn_id, None)

    async def next_turn_event(self, turn_id: str) -> AgentEvent:
        """只读取指定轮次的事件，保证并发轮次不会互相抢事件"""

        queue = self._turn_events.get(turn_id)
        if queue is None:
            return await self.next_event()
        return await self._read_event(queue)

    async def resolve_approval(self, approval_id: str, decision: str) -> None:
        await self._call("agent.approval.resolve", {"approval_id": approval_id, "decision": decision})

    async def next_event(self) -> AgentEvent:
        return await self._read_event(self._events)

    async def _read_event(self, queue: asyncio.Queue[object]) -> AgentEvent:
        if not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, BaseException):
                raise item
            assert isinstance(item, AgentEvent)
            return item
        event_task = asyncio.create_task(queue.get())
        try:
            await asyncio.wait((event_task, self._reader_task), return_when=asyncio.FIRST_COMPLETED)
            if event_task.done():
                result = event_task.result()
                if isinstance(result, BaseException):
                    raise result
                assert isinstance(result, AgentEvent)
                return result
            raise self._failure or AgentClientError("Agent Worker 连接已断开")
        finally:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._call_while_closing("agent.shutdown", {})
        finally:
            self._writer.close()
            await self._writer.wait_closed()
            await self._reader_task

    async def _call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if self._closed:
            raise AgentClientError("Agent Worker 客户端已关闭")
        return await self._send(method, params)

    async def _call_while_closing(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return await self._send(method, params)

    async def _send(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if self._failure is not None:
            raise self._failure
        request_id = uuid4().hex
        future: asyncio.Future[Mapping[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(10):
                async with self._write_lock:
                    self._writer.write(encode_message(AgentRpcRequest(request_id, method, params)))
                    await self._writer.drain()
                return await future
        except TimeoutError as error:
            raise AgentClientError("Agent Worker 请求超时") from error
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    raise AgentClientError("Agent Worker 连接已断开")
                message = decode_message(line)
                if isinstance(message, AgentRpcResponse):
                    future = self._pending.pop(message.request_id, None)
                    if future is None or future.done():
                        continue
                    if message.error is not None:
                        detail = message.error.get("message") if isinstance(message.error, Mapping) else None
                        future.set_exception(AgentClientError(str(detail or "Agent Worker 请求失败")))
                    else:
                        future.set_result(message.result if isinstance(message.result, Mapping) else {})
                elif isinstance(message, AgentRpcNotification) and message.method == "agent.event":
                    event = AgentEvent.from_mapping(message.params)
                    queue = self._turn_events.get(event.turn_id, self._events)
                    if queue.full():
                        # 丢弃普通进度事件，保持取消和审批响应可用
                        try:
                            dropped = queue.get_nowait()
                            if isinstance(dropped, BaseException):
                                raise dropped
                        except asyncio.QueueEmpty:
                            pass
                    await queue.put(event)
        except Exception as error:  # noqa: BLE001
            failure = error if isinstance(error, AgentClientError) else AgentClientError("Agent Worker 读取失败")
            self._failure = failure
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(failure)
            self._pending.clear()
            for queue in self._turn_events.values():
                if not queue.full():
                    queue.put_nowait(failure)
            self._turn_events.clear()
            if not self._events.full():
                self._events.put_nowait(failure)
