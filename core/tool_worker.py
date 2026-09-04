"""Tool Worker 内部的策略复核与固定 handler 调度"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any, BinaryIO

from .audit import AuditContext
from .cancellation import CancellationSource, CancellationToken, CancelledError
from .policy import (
    AuthorizationError,
    AuthorizationVerifier,
    ConcurrencyPolicy,
    JsonValue,
    PolicyDecision,
    PolicyEngine,
    PolicyError,
    RiskLevel,
    ToolProposal,
)
from .tool_rpc import (
    DEFAULT_MAX_LINE_BYTES,
    RpcError,
    RpcProtocolError,
    RpcRequest,
    RpcResponse,
    decode_request,
    encode_response,
)
from .tool_types import (
    RegisteredTool,
    ToolCatalog,
    ToolExecutionResult,
    ToolExecutionStatus,
)


class ToolWorkerService:
    """在执行前独立复核策略和一次性授权"""

    def __init__(
        self,
        catalog: ToolCatalog,
        policy_engine: PolicyEngine,
        authorization_verifier: AuthorizationVerifier,
        *,
        audit_store: Any | None = None,
    ) -> None:
        self._catalog = catalog
        self._policy_engine = policy_engine
        self._authorization_verifier = authorization_verifier
        self._audit_store = audit_store
        self._serial_lock = asyncio.Lock()
        self._tool_locks: dict[str, asyncio.Lock] = {}

    @property
    def catalog(self) -> ToolCatalog:
        return self._catalog

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._policy_engine

    async def execute(
        self,
        proposal_data: Mapping[str, JsonValue],
        decision_data: Mapping[str, JsonValue],
        authorization: str | None,
        token: CancellationToken,
        *,
        audit_context: AuditContext | None = None,
    ) -> ToolExecutionResult:
        started = time.monotonic()
        result = await self._execute_core(
            proposal_data,
            decision_data,
            authorization,
            token,
        )
        if self._audit_store is None or audit_context is None:
            return result
        try:
            proposal = self._parse_proposal(proposal_data)
            decision = self._policy_engine.evaluate(proposal)
            registered = self._catalog.get(proposal.tool_name)
            if registered is None:
                return result
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            await asyncio.to_thread(
                self._audit_store.write,
                context=audit_context,
                manifest=registered.manifest,
                arguments=proposal.arguments,
                decision=decision,
                result=result,
                duration_ms=duration_ms,
            )
        except Exception as error:  # noqa: BLE001 审计失败必须按风险安全收敛
            _ = error
            try:
                risk = decision.risk
            except UnboundLocalError:
                return result
            if risk >= RiskLevel.R2:
                return ToolExecutionResult(
                    ToolExecutionStatus.FAILED,
                    {},
                    "高风险工具审计写入失败",
                )
        return result

    async def _execute_core(
        self,
        proposal_data: Mapping[str, JsonValue],
        decision_data: Mapping[str, JsonValue],
        authorization: str | None,
        token: CancellationToken,
    ) -> ToolExecutionResult:
        token.throw_if_cancelled()
        try:
            proposal = self._parse_proposal(proposal_data)
            local_decision = self._policy_engine.evaluate(proposal)
            if not self._matches_decision(local_decision, decision_data):
                return self._denied("父进程策略决策与 Worker 不一致")
        except (PolicyError, TypeError, ValueError):
            return self._denied("工具提议或策略决策无效")

        if not local_decision.allowed:
            return self._denied(local_decision.reason)
        if local_decision.requires_authorization:
            if not isinstance(authorization, str):
                return self._denied("工具调用缺少有效授权")
            try:
                self._authorization_verifier.consume(
                    authorization,
                    proposal,
                    local_decision,
                )
            except AuthorizationError:
                return self._denied("工具授权无效或已消费")
        elif authorization is not None:
            return self._denied("自动执行工具不能携带授权令牌")

        registered = self._catalog.get(proposal.tool_name)
        if registered is None:
            return self._denied("Worker 中不存在该工具")
        lock = self._lock_for(registered)
        if lock is None:
            return await self._execute_handler(registered, proposal, token)
        async with lock:
            return await self._execute_handler(registered, proposal, token)

    async def _execute_handler(
        self,
        registered: RegisteredTool,
        proposal: ToolProposal,
        token: CancellationToken,
    ) -> ToolExecutionResult:
        token.throw_if_cancelled()
        try:
            async with asyncio.timeout(registered.manifest.timeout):
                result = await registered.handler.execute(
                    proposal.arguments,
                    token,
                )
        except TimeoutError:
            return ToolExecutionResult(
                ToolExecutionStatus.TIMEOUT,
                {},
                "工具执行超时",
            )
        except CancelledError:
            return ToolExecutionResult(
                ToolExecutionStatus.CANCELLED,
                {},
                "工具执行已取消",
            )
        except Exception as error:  # noqa: BLE001 handler 属于 Worker 的不可信边界
            return ToolExecutionResult(
                ToolExecutionStatus.FAILED,
                {"error_type": type(error).__name__},
                "工具执行失败",
            )
        if not isinstance(result, ToolExecutionResult):
            return ToolExecutionResult(
                ToolExecutionStatus.FAILED,
                {},
                "工具返回了无效结果",
            )
        return result

    def _lock_for(self, registered: RegisteredTool) -> asyncio.Lock | None:
        policy = registered.manifest.concurrency_policy
        if policy is ConcurrencyPolicy.SERIAL:
            return self._serial_lock
        if policy is ConcurrencyPolicy.PER_TOOL:
            return self._tool_locks.setdefault(
                registered.manifest.name,
                asyncio.Lock(),
            )
        return None

    @staticmethod
    def _parse_proposal(data: Mapping[str, JsonValue]) -> ToolProposal:
        if not isinstance(data, Mapping) or set(data) != {
            "call_id",
            "tool_name",
            "arguments",
        }:
            raise ValueError("proposal fields are invalid")
        arguments = data.get("arguments")
        if not isinstance(arguments, Mapping):
            raise TypeError("proposal arguments must be an object")
        call_id = data.get("call_id")
        tool_name = data.get("tool_name")
        if not isinstance(call_id, str) or not isinstance(tool_name, str):
            raise TypeError("proposal identifiers must be strings")
        return ToolProposal(call_id, tool_name, arguments)

    @staticmethod
    def _matches_decision(
        local: PolicyDecision,
        parent: Mapping[str, JsonValue],
    ) -> bool:
        if not isinstance(parent, Mapping) or set(parent) != {
            "allowed",
            "risk",
            "confirmation",
            "summary",
            "call_fingerprint",
            "policy_version",
        }:
            return False
        if type(parent.get("allowed")) is not bool:
            return False
        version = parent.get("policy_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return False
        return dict(parent) == {
            "allowed": local.allowed,
            "risk": local.risk.label,
            "confirmation": local.confirmation.value,
            "summary": local.summary,
            "call_fingerprint": local.call_fingerprint,
            "policy_version": local.policy_version,
        }

    @staticmethod
    def _denied(message: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            ToolExecutionStatus.DENIED,
            {},
            message,
        )


def execution_result_to_data(
    result: ToolExecutionResult,
) -> dict[str, JsonValue]:
    """把工具结果转换为 RPC 可编码对象"""

    return {
        "status": result.status.value,
        "payload": result.payload,
        "safe_message": result.safe_message,
        "undo_data": result.undo_data,
    }


class ToolWorkerServer:
    """在 NDJSON 流上并发处理有界 Tool Worker 请求"""

    def __init__(
        self,
        service: ToolWorkerService,
        *,
        max_concurrency: int = 4,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        if max_concurrency <= 0 or max_line_bytes <= 0:
            raise ValueError("Worker 并发和消息上限必须大于零")
        self._service = service
        self._max_concurrency = max_concurrency
        self._max_line_bytes = max_line_bytes
        self._active: dict[str, CancellationSource] = {}

    async def serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        write_lock = asyncio.Lock()
        response_tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._write_response(
                        writer,
                        write_lock,
                        RpcResponse(
                            "server-error",
                            error=RpcError(-32700, "RPC 消息超过大小上限"),
                        ),
                    )
                    continue
                if not line:
                    break
                try:
                    request = decode_request(
                        line,
                        max_line_bytes=self._max_line_bytes,
                    )
                except RpcProtocolError:
                    await self._write_response(
                        writer,
                        write_lock,
                        RpcResponse(
                            "server-error",
                            error=RpcError(-32700, "RPC 请求无效"),
                        ),
                    )
                    continue
                task = asyncio.create_task(
                    self._respond(request, writer, write_lock)
                )
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)
        finally:
            for source in tuple(self._active.values()):
                source.cancel("rpc_eof")
            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            writer.close()
            await writer.wait_closed()

    async def serve_stdio(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
    ) -> None:
        """在匿名标准管道上运行相同的并发 RPC 服务"""

        write_lock = asyncio.Lock()
        response_tasks: set[asyncio.Task[None]] = set()
        while True:
            line = await asyncio.to_thread(
                input_stream.readline,
                self._max_line_bytes + 1,
            )
            if not line:
                break
            try:
                request = decode_request(
                    line,
                    max_line_bytes=self._max_line_bytes,
                )
            except RpcProtocolError:
                await self._write_stdio_response(
                    output_stream,
                    write_lock,
                    RpcResponse(
                        "server-error",
                        error=RpcError(-32700, "RPC 请求无效"),
                    ),
                )
                continue
            task = asyncio.create_task(
                self._respond_stdio(request, output_stream, write_lock)
            )
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)
        for source in tuple(self._active.values()):
            source.cancel("rpc_eof")
        if response_tasks:
            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)

    async def _respond_stdio(
        self,
        request: RpcRequest,
        output_stream: BinaryIO,
        write_lock: asyncio.Lock,
    ) -> None:
        try:
            response = await self._handle(request)
        except Exception as error:  # noqa: BLE001 单个请求异常不能关闭匿名管道
            _ = error
            response = RpcResponse(
                request.request_id,
                error=RpcError(-32603, "Tool Worker 内部错误"),
            )
        await self._write_stdio_response(output_stream, write_lock, response)

    async def _write_stdio_response(
        self,
        output_stream: BinaryIO,
        lock: asyncio.Lock,
        response: RpcResponse,
    ) -> None:
        encoded = encode_response(
            response,
            max_line_bytes=self._max_line_bytes,
        )
        async with lock:
            await asyncio.to_thread(output_stream.write, encoded)
            await asyncio.to_thread(output_stream.flush)

    async def _respond(
        self,
        request: RpcRequest,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
    ) -> None:
        try:
            response = await self._handle(request)
        except Exception as error:  # noqa: BLE001 单个 RPC 请求不能终止 Worker
            _ = error
            response = RpcResponse(
                request.request_id,
                error=RpcError(-32603, "Tool Worker 内部错误"),
            )
        await self._write_response(writer, write_lock, response)

    async def _handle(self, request: RpcRequest) -> RpcResponse:
        if request.method == "ping":
            return RpcResponse(
                request.request_id,
                result={"protocol_version": 1, "status": "ready"},
            )
        if request.method == "cancel":
            target_id = request.params["request_id"]
            assert isinstance(target_id, str)
            source = self._active.get(target_id)
            if source is not None:
                source.cancel("rpc_cancel")
            return RpcResponse(
                request.request_id,
                result={"cancelled": source is not None},
            )
        if request.request_id in self._active:
            return RpcResponse(
                request.request_id,
                error=RpcError(-32600, "RPC request ID 正在使用"),
            )
        if len(self._active) >= self._max_concurrency:
            return RpcResponse(
                request.request_id,
                error=RpcError(-32001, "Tool Worker 繁忙"),
            )
        proposal = request.params["proposal"]
        decision = request.params["decision"]
        authorization = request.params["authorization"]
        context_data = request.params["context"]
        assert isinstance(proposal, Mapping)
        assert isinstance(decision, Mapping)
        assert authorization is None or isinstance(authorization, str)
        assert isinstance(context_data, Mapping)
        try:
            context = AuditContext(
                context_data.get("turn_id"),
                context_data.get("correlation_id"),
                context_data.get("tool_call_id"),
            )
        except (TypeError, ValueError):
            return RpcResponse(
                request.request_id,
                error=RpcError(-32602, "工具审计上下文无效"),
            )
        source = CancellationSource()
        self._active[request.request_id] = source
        try:
            result = await self._service.execute(
                proposal,
                decision,
                authorization,
                source.token,
                audit_context=context,
            )
            return RpcResponse(
                request.request_id,
                result=execution_result_to_data(result),
            )
        finally:
            self._active.pop(request.request_id, None)

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        lock: asyncio.Lock,
        response: RpcResponse,
    ) -> None:
        encoded = encode_response(
            response,
            max_line_bytes=self._max_line_bytes,
        )
        async with lock:
            writer.write(encoded)
            await writer.drain()
