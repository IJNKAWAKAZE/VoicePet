import asyncio

import pytest

from core.audit import AuditContext
from core.cancellation import CancellationSource
from core.policy import (
    AuthorizationVerifier,
    ConcurrencyPolicy,
    PolicyEngine,
    PolicySettings,
    RiskLevel,
    ToolManifest,
    ToolProposal,
)
from core.tool_client import ToolClientError, ToolWorkerClient
from core.tool_rpc import RpcRequest, decode_response, encode_request
from core.tool_types import (
    RegisteredTool,
    ToolCatalog,
    ToolExecutionResult,
    ToolExecutionStatus,
)
from core.tool_worker import ToolWorkerServer, ToolWorkerService


class EchoHandler:
    def __init__(self) -> None:
        self.wait = False
        self.started = asyncio.Event()

    async def execute(self, arguments, token):
        self.started.set()
        if self.wait:
            await token.wait()
            token.throw_if_cancelled()
        return ToolExecutionResult(
            ToolExecutionStatus.SUCCESS,
            {"echo": arguments["value"]},
            "执行成功",
        )


def create_worker(*, max_concurrency=4):
    handler = EchoHandler()
    manifest = ToolManifest(
        name="echo_tool",
        description="回显测试值",
        input_schema={
            "type": "object",
            "maxProperties": 1,
            "properties": {
                "value": {"type": "string", "maxLength": 32},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        base_risk=RiskLevel.R0,
        timeout=2,
        concurrency_policy=ConcurrencyPolicy.PARALLEL,
    )
    catalog = ToolCatalog([RegisteredTool(manifest, handler)])
    engine = PolicyEngine(catalog.policy_registry, PolicySettings())
    service = ToolWorkerService(
        catalog,
        engine,
        AuthorizationVerifier(b"k" * 32),
    )
    return ToolWorkerServer(service, max_concurrency=max_concurrency), engine, handler


async def open_worker(worker):
    tasks = set()

    async def accept(reader, writer):
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        try:
            await worker.serve(reader, writer)
        finally:
            tasks.discard(task)

    server = await asyncio.start_server(
        accept,
        "127.0.0.1",
        0,
        limit=1024 * 1024 + 1,
    )
    address = server.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(
        address[0],
        address[1],
        limit=1024 * 1024 + 1,
    )
    return server, tasks, reader, writer


async def close_worker(server, tasks):
    server.close()
    await server.wait_closed()
    if tasks:
        await asyncio.gather(*tuple(tasks))


def test_client_ping_and_concurrent_execute_keep_responses_correlated():
    async def scenario():
        worker, engine, _ = create_worker()
        server, tasks, reader, writer = await open_worker(worker)
        client = ToolWorkerClient(reader, writer)
        try:
            assert await client.ping() == {
                "protocol_version": 1,
                "status": "ready",
            }
            proposals = [
                ToolProposal(f"call-{index}", "echo_tool", {"value": value})
                for index, value in enumerate(("first", "second"))
            ]
            results = await asyncio.gather(
                *(
                    client.execute(
                        proposal,
                        engine.evaluate(proposal),
                        None,
                        CancellationSource().token,
                        audit_context=AuditContext(
                            "00000000-0000-0000-0000-000000000001",
                            "00000000-0000-0000-0000-000000000002",
                            proposal.call_id,
                        ),
                    )
                    for proposal in proposals
                )
            )

            assert [result.payload["echo"] for result in results] == [
                "first",
                "second",
            ]
        finally:
            await client.close()
            await close_worker(server, tasks)

    asyncio.run(scenario())


def test_client_cancellation_sends_cancel_and_receives_cancelled_result():
    async def scenario():
        worker, engine, handler = create_worker()
        handler.wait = True
        server, tasks, reader, writer = await open_worker(worker)
        client = ToolWorkerClient(reader, writer)
        source = CancellationSource()
        proposal = ToolProposal("call-1", "echo_tool", {"value": "wait"})
        try:
            task = asyncio.create_task(
                client.execute(
                    proposal,
                    engine.evaluate(proposal),
                    None,
                    source.token,
                )
            )
            await handler.started.wait()
            source.cancel("interrupt")
            result = await asyncio.wait_for(task, timeout=2)

            assert result.status is ToolExecutionStatus.CANCELLED
        finally:
            await client.close()
            await close_worker(server, tasks)

    asyncio.run(scenario())


def test_server_survives_malformed_request_on_same_connection():
    async def scenario():
        worker, _, _ = create_worker()
        server, tasks, reader, writer = await open_worker(worker)
        try:
            writer.write(b'{"private-secret": NaN}\n')
            await writer.drain()
            malformed = decode_response(await reader.readline())
            assert malformed.error is not None
            assert "private-secret" not in malformed.error.message

            writer.write(encode_request(RpcRequest("ping-1", "ping", {})))
            await writer.drain()
            healthy = decode_response(await reader.readline())
            assert healthy.result["status"] == "ready"
        finally:
            writer.close()
            await writer.wait_closed()
            await close_worker(server, tasks)

    asyncio.run(scenario())


def test_server_returns_busy_without_queueing_over_limit():
    async def scenario():
        worker, engine, handler = create_worker(max_concurrency=1)
        handler.wait = True
        server, tasks, reader, writer = await open_worker(worker)
        client = ToolWorkerClient(reader, writer)
        first_source = CancellationSource()
        first = ToolProposal("call-1", "echo_tool", {"value": "first"})
        second = ToolProposal("call-2", "echo_tool", {"value": "second"})
        try:
            first_task = asyncio.create_task(
                client.execute(
                    first,
                    engine.evaluate(first),
                    None,
                    first_source.token,
                )
            )
            await handler.started.wait()
            with pytest.raises(ToolClientError, match="繁忙"):
                await client.execute(
                    second,
                    engine.evaluate(second),
                    None,
                    CancellationSource().token,
                )
            first_source.cancel("cleanup")
            await first_task
        finally:
            await client.close()
            await close_worker(server, tasks)

    asyncio.run(scenario())


def test_client_close_fails_pending_requests_and_is_idempotent():
    async def scenario():
        worker, engine, handler = create_worker()
        handler.wait = True
        server, tasks, reader, writer = await open_worker(worker)
        client = ToolWorkerClient(reader, writer)
        proposal = ToolProposal("call-1", "echo_tool", {"value": "wait"})
        pending = asyncio.create_task(
            client.execute(
                proposal,
                engine.evaluate(proposal),
                None,
                CancellationSource().token,
            )
        )
        await handler.started.wait()

        await client.close()
        await client.close()

        with pytest.raises(ToolClientError, match="关闭"):
            await asyncio.wait_for(pending, timeout=2)
        await close_worker(server, tasks)

    asyncio.run(scenario())
