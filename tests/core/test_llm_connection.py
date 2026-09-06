import asyncio
import threading

import pytest

from core.diagnostics import (
    DiagnosticCheck,
    DiagnosticError,
    DiagnosticExporter,
    DiagnosticRunner,
    DiagnosticService,
    DiagnosticStatus,
)
from core.event_bus import EventBus
from core.runtime import RuntimeHost, RuntimeHostError, RuntimeServices


def make_service(checks):
    return DiagnosticService(
        DiagnosticRunner(checks), DiagnosticExporter(),
        config_summary=dict, log_text=lambda: "",
    )


def test_single_component_does_not_run_other_probes_or_replace_full_report():
    calls = []

    async def llm():
        calls.append("llm")
        return {}

    async def asr():
        calls.append("asr")
        return {}

    service = make_service([DiagnosticCheck("llm", llm), DiagnosticCheck("asr", asr)])
    report = asyncio.run(service.run())
    calls.clear()

    result = asyncio.run(service.run_component("llm"))

    assert result.component == "llm"
    assert result.status is DiagnosticStatus.HEALTHY
    assert calls == ["llm"]
    assert service.last_report is report


def test_unknown_component_is_rejected_without_running_probes():
    calls = []

    async def llm():
        calls.append("llm")
        return {}

    runner = DiagnosticRunner([DiagnosticCheck("llm", llm)])
    with pytest.raises(DiagnosticError, match="组件"):
        asyncio.run(runner.run_component("unknown"))
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "exception"])
def test_single_component_keeps_timeout_and_error_isolation(failure):
    async def probe():
        if failure == "timeout":
            await asyncio.sleep(1)
        raise RuntimeError("api_key=private-secret")

    runner = DiagnosticRunner([DiagnosticCheck("llm", probe, timeout=0.01)])
    result = asyncio.run(runner.run_component("llm"))
    assert result.status is DiagnosticStatus.UNAVAILABLE
    assert result.duration_ms >= 0
    assert "private-secret" not in result.safe_message
    assert ("超时" if failure == "timeout" else "失败") in result.safe_message


def test_llm_connection_runs_on_real_runtime_thread():
    calls = []

    async def llm():
        calls.append(threading.get_ident())
        return {}

    class Lifecycle:
        async def start(self):
            pass

        async def stop(self):
            pass

    service = make_service([DiagnosticCheck("llm", llm)])
    runtime = RuntimeHost(RuntimeServices(
        event_bus=EventBus(), coordinator=Lifecycle(), activation=object(),
        capture=Lifecycle(), diagnostics=service,
    ))
    runtime.start()
    try:
        result = runtime.test_llm_connection().result(timeout=2)
        assert result.component == "llm"
        assert result.status is DiagnosticStatus.HEALTHY
        assert calls == [runtime.thread_id]
        assert calls[0] != threading.get_ident()
        assert service.last_report is None
    finally:
        runtime.close()

    with pytest.raises(RuntimeHostError):
        runtime.test_llm_connection()
