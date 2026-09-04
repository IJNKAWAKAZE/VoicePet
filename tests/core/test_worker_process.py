import asyncio
import sys
from pathlib import Path

import pytest

import core
from core.audit import AuditStore
from core.builtin_tools import SystemInfoTool
from core.cancellation import CancellationSource
from core.policy import PolicyEngine, PolicySettings, ToolProposal, ToolRegistry
from core.tool_client import ToolClientError
from core.tool_types import ToolExecutionStatus
from core.worker_bootstrap import WorkerBootstrap
from core.worker_process import ToolWorkerProcessManager, WorkerProcessError


def test_worker_process_types_are_publicly_exported():
    assert core.WorkerBootstrap is WorkerBootstrap
    assert core.WorkerBootstrapError.__name__ == "WorkerBootstrapError"
    assert core.ToolWorkerProcessManager is ToolWorkerProcessManager
    assert core.WorkerProcessError is WorkerProcessError


def manager_case(tmp_path, *, restart_limit=3, auto_restart=False):
    root = tmp_path / "allowed"
    root.mkdir()
    bootstrap = WorkerBootstrap(
        secret=b"k" * 32,
        audit_database=(tmp_path / "audit.db").resolve(),
        allowed_roots=(root.resolve(),),
        policy_version=1,
    )
    command = (sys.executable, str(Path("app.py").resolve()), "--tool-worker")
    manager = ToolWorkerProcessManager(
        bootstrap,
        command=command,
        startup_timeout=10,
        shutdown_timeout=2,
        restart_limit=restart_limit,
        auto_restart=auto_restart,
        restart_backoff=0.01,
    )
    return manager, bootstrap


def parent_decision(proposal):
    manifest = SystemInfoTool().manifest
    engine = PolicyEngine(ToolRegistry([manifest]), PolicySettings())
    return engine.evaluate(proposal)


def test_real_worker_process_ping_execute_audit_and_private_command(tmp_path):
    async def scenario():
        manager, bootstrap = manager_case(tmp_path)
        await manager.start()
        try:
            assert await manager.ping() == {
                "protocol_version": 1,
                "status": "ready",
            }
            proposal = ToolProposal("call-1", "system_info", {})
            result = await manager.execute(
                proposal,
                parent_decision(proposal),
                None,
                CancellationSource().token,
            )

            assert result.status is ToolExecutionStatus.SUCCESS
            assert result.payload["system"] == "Windows"
            assert bootstrap.secret.hex() not in " ".join(manager.command)
            assert manager.is_running is True
        finally:
            await manager.stop()

        store = AuditStore(bootstrap.audit_database)
        records = store.list_recent(limit=10)
        assert len(records) == 1
        assert records[0].tool_name == "system_info"
        assert records[0].tool_call_id == "call-1"
        store.close()
        assert manager.is_running is False
        await manager.stop()

    asyncio.run(scenario())


def test_worker_crash_fails_current_client_without_retry_then_explicitly_restarts(tmp_path):
    async def scenario():
        manager, _ = manager_case(tmp_path)
        await manager.start()
        old_pid = manager.pid
        manager.terminate_for_test()
        await manager.wait_for_exit()

        with pytest.raises((ToolClientError, WorkerProcessError)):
            await manager.ping()
        assert manager.crash_count == 1

        await manager.restart()
        assert manager.pid != old_pid
        assert await manager.ping()
        await manager.stop()

    asyncio.run(scenario())


def test_worker_restart_limit_enters_unavailable_state(tmp_path):
    async def scenario():
        manager, _ = manager_case(tmp_path, restart_limit=1)
        await manager.start()
        manager.terminate_for_test()
        await manager.wait_for_exit()

        with pytest.raises(WorkerProcessError, match="次数"):
            await manager.restart()
        assert manager.is_available is False
        await manager.stop()

    asyncio.run(scenario())


def test_worker_manager_rejects_secret_in_command(tmp_path):
    _, bootstrap = manager_case(tmp_path)
    secret_text = bootstrap.secret.hex()

    with pytest.raises(WorkerProcessError, match="密钥"):
        ToolWorkerProcessManager(
            bootstrap,
            command=(sys.executable, secret_text),
        )


def test_worker_automatically_restarts_after_idle_crash_with_limit(tmp_path):
    async def scenario():
        manager, _ = manager_case(tmp_path, auto_restart=True)
        await manager.start()
        old_pid = manager.pid
        manager.terminate_for_test()

        for _ in range(300):
            if manager.pid != old_pid and manager.is_running:
                break
            await asyncio.sleep(0.01)

        assert manager.pid != old_pid
        assert manager.is_running is True
        assert manager.crash_count == 1
        assert (await manager.ping())["status"] == "ready"
        await manager.stop()

    asyncio.run(scenario())


def test_worker_auto_restart_retries_transient_startup_failure(tmp_path):
    script = tmp_path / "transient_worker.py"
    launches = tmp_path / "launches.txt"
    script.write_text(
        "import json, pathlib, sys\n"
        "counter = pathlib.Path(sys.argv[1])\n"
        "value = int(counter.read_text() or '0') + 1 if counter.exists() else 1\n"
        "counter.write_text(str(value))\n"
        "sys.stdin.buffer.readline()\n"
        "if value == 2: raise SystemExit(7)\n"
        "for line in sys.stdin.buffer:\n"
        " request = json.loads(line)\n"
        " response = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'protocol_version': 1, 'status': 'ready'}}\n"
        " sys.stdout.write(json.dumps(response) + '\\n'); sys.stdout.flush()\n",
        encoding="utf-8",
    )
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    bootstrap = WorkerBootstrap(
        b"k" * 32,
        (tmp_path / "audit.db").resolve(),
        (allowed.resolve(),),
    )
    manager = ToolWorkerProcessManager(
        bootstrap,
        command=(sys.executable, str(script), str(launches)),
        startup_timeout=0.5,
        shutdown_timeout=1,
        restart_limit=3,
        auto_restart=True,
        restart_backoff=0.01,
    )

    async def scenario():
        await manager.start()
        manager.terminate_for_test()
        try:
            for _ in range(500):
                if launches.exists() and launches.read_text() == "3" and manager.is_running:
                    break
                await asyncio.sleep(0.01)

            assert launches.read_text() == "3"
            assert manager.is_running is True
            assert manager.crash_count == 2
            assert (await manager.ping())["status"] == "ready"
        finally:
            await manager.stop()

    asyncio.run(scenario())
