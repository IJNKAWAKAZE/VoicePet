"""Runtime 使用的异步 Tool Worker JSON-RPC 客户端"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import uuid4

from .audit import AuditContext
from .cancellation import CancellationToken
from .policy import JsonValue, PolicyDecision, ToolProposal
from .tool_rpc import (
    DEFAULT_MAX_LINE_BYTES,
    RpcRequest,
    decode_response,
    encode_request,
)
from .tool_types import ToolExecutionResult, ToolExecutionStatus


class ToolClientError(RuntimeError):
    """Tool Worker 连接、协议或远端错误"""

    code = "tool.client"


class ToolWorkerClient:
    """按 request ID 关联并发响应并转发协作取消"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_line_bytes = max_line_bytes
        self._pending: dict[str, asyncio.Future[Mapping[str, JsonValue]]] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_responses())

    async def ping(self) -> Mapping[str, JsonValue]:
        return await self._call("ping", {})

    async def execute(
        self,
        proposal: ToolProposal,
        decision: PolicyDecision,
        authorization: str | None,
        token: CancellationToken,
        *,
        audit_context: AuditContext | None = None,
    ) -> ToolExecutionResult:
        token.throw_if_cancelled()
        audit_context = audit_context or AuditContext(
            str(uuid4()),
            str(uuid4()),
            proposal.call_id,
        )
        request_id = uuid4().hex
        future = await self._send(
            RpcRequest(
                request_id,
                "execute",
                {
                    "proposal": self._proposal_data(proposal),
                    "decision": self._decision_data(decision),
                    "authorization": authorization,
                    "context": {
                        "turn_id": audit_context.turn_id,
                        "correlation_id": audit_context.correlation_id,
                        "tool_call_id": audit_context.tool_call_id,
                    },
                },
            )
        )
        cancel_task = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                (future, cancel_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if future not in done:
                await self._call("cancel", {"request_id": request_id})
            data = await future
            return self._execution_result(data)
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failure = ToolClientError("Tool Worker client 已关闭")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._pending.clear()
        self._writer.close()
        await self._writer.wait_closed()
        await self._reader_task

    async def _call(
        self,
        method: str,
        params: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        future = await self._send(RpcRequest(uuid4().hex, method, params))
        return await future

    async def _send(
        self,
        request: RpcRequest,
    ) -> asyncio.Future[Mapping[str, JsonValue]]:
        if self._closed:
            raise ToolClientError("Tool Worker client 已关闭")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, JsonValue]] = loop.create_future()
        self._pending[request.request_id] = future
        try:
            encoded = encode_request(
                request,
                max_line_bytes=self._max_line_bytes,
            )
            async with self._write_lock:
                self._writer.write(encoded)
                await self._writer.drain()
        except Exception as error:
            self._pending.pop(request.request_id, None)
            raise ToolClientError(
                f"Tool Worker 请求发送失败: {type(error).__name__}"
            ) from error
        return future

    async def _read_responses(self) -> None:
        failure: ToolClientError | None = None
        try:
            while line := await self._reader.readline():
                response = decode_response(
                    line,
                    max_line_bytes=self._max_line_bytes,
                )
                future = self._pending.pop(response.request_id, None)
                if future is None or future.done():
                    continue
                if response.error is not None:
                    future.set_exception(ToolClientError(response.error.message))
                else:
                    assert response.result is not None
                    future.set_result(response.result)
        except Exception as error:  # noqa: BLE001 读取循环必须统一终结全部等待者
            failure = ToolClientError(
                f"Tool Worker 响应读取失败: {type(error).__name__}"
            )
        finally:
            if failure is None and not self._closed:
                failure = ToolClientError("Tool Worker 连接已关闭")
            if failure is not None:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(failure)
            self._pending.clear()

    @staticmethod
    def _proposal_data(proposal: ToolProposal) -> dict[str, JsonValue]:
        return {
            "call_id": proposal.call_id,
            "tool_name": proposal.tool_name,
            "arguments": proposal.arguments,
        }

    @staticmethod
    def _decision_data(decision: PolicyDecision) -> dict[str, JsonValue]:
        return {
            "allowed": decision.allowed,
            "risk": decision.risk.label,
            "confirmation": decision.confirmation.value,
            "summary": decision.summary,
            "call_fingerprint": decision.call_fingerprint,
            "policy_version": decision.policy_version,
        }

    @staticmethod
    def _execution_result(
        data: Mapping[str, JsonValue],
    ) -> ToolExecutionResult:
        if set(data) != {"status", "payload", "safe_message", "undo_data"}:
            raise ToolClientError("Tool Worker 结果字段无效")
        try:
            status = ToolExecutionStatus(data.get("status"))
        except (TypeError, ValueError) as error:
            raise ToolClientError("Tool Worker 结果状态无效") from error
        payload = data.get("payload")
        message = data.get("safe_message")
        undo_data = data.get("undo_data")
        if not isinstance(payload, Mapping) or not isinstance(message, str):
            raise ToolClientError("Tool Worker 结果内容无效")
        if undo_data is not None and not isinstance(undo_data, Mapping):
            raise ToolClientError("Tool Worker 撤销数据无效")
        return ToolExecutionResult(status, payload, message, undo_data)
