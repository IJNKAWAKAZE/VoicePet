"""Tool Worker 进程模式的受控启动入口"""

from __future__ import annotations

import asyncio
import os

from .audit import AuditStore
from .policy import AuthorizationVerifier, PolicyEngine, PolicySettings
from .tool_worker import ToolWorkerServer, ToolWorkerService
from .worker_bootstrap import MAX_BOOTSTRAP_BYTES, decode_bootstrap
from .worker_catalog import build_worker_catalog


def run_worker() -> int:
    """从匿名标准管道读取私有引导并运行 Tool Worker"""

    try:
        input_stream = os.fdopen(os.dup(0), "rb", buffering=0)
        output_stream = os.fdopen(os.dup(1), "wb", buffering=0)
        line = input_stream.readline(MAX_BOOTSTRAP_BYTES + 1)
        bootstrap = decode_bootstrap(line)
    except (OSError, RuntimeError):
        return 2
    audit_store = None
    try:
        audit_store = AuditStore(bootstrap.audit_database)
        catalog = build_worker_catalog(bootstrap.allowed_roots, audit_store)
        policy = PolicyEngine(
            catalog.policy_registry,
            PolicySettings(version=bootstrap.policy_version),
        )
        service = ToolWorkerService(
            catalog,
            policy,
            AuthorizationVerifier(bootstrap.secret),
            audit_store=audit_store,
        )
        asyncio.run(
            ToolWorkerServer(service).serve_stdio(input_stream, output_stream)
        )
        return 0
    except Exception as error:  # noqa: BLE001 Worker 顶层只返回稳定退出码
        _ = error
        return 3
    finally:
        if audit_store is not None:
            audit_store.close()
        try:
            input_stream.close()
            output_stream.close()
        except OSError:
            pass
