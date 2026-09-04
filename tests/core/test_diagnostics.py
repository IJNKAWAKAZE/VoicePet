import asyncio
import json
import zipfile

from core.diagnostics import (
    DiagnosticCheck,
    DiagnosticError,
    DiagnosticExporter,
    DiagnosticRunner,
    DiagnosticService,
    DiagnosticStatus,
)


def test_runner_isolates_checks_orders_results_and_maps_timeout():
    async def healthy():
        return {"device_count": 2}

    async def failing():
        raise RuntimeError("secret request body")

    async def slow():
        await asyncio.sleep(1)
        return {}

    runner = DiagnosticRunner(
        [
            DiagnosticCheck("tts", healthy, timeout=1),
            DiagnosticCheck("llm", failing, timeout=1),
            DiagnosticCheck("asr", slow, timeout=0.01),
        ]
    )

    report = asyncio.run(runner.run())

    assert [item.component for item in report.results] == ["tts", "llm", "asr"]
    assert [item.status for item in report.results] == [
        DiagnosticStatus.HEALTHY,
        DiagnosticStatus.UNAVAILABLE,
        DiagnosticStatus.UNAVAILABLE,
    ]
    assert "secret" not in report.results[1].safe_message
    assert all(item.duration_ms >= 0 for item in report.results)
    assert report.overall_status is DiagnosticStatus.UNAVAILABLE


def test_runner_accepts_explicit_degraded_result():
    async def degraded():
        return DiagnosticStatus.DEGRADED, "正在使用 CPU", {"backend": "cpu"}

    report = asyncio.run(
        DiagnosticRunner([DiagnosticCheck("asr", degraded)]).run()
    )

    assert report.results[0].status is DiagnosticStatus.DEGRADED
    assert report.overall_status is DiagnosticStatus.DEGRADED


def test_exporter_writes_fixed_entries_and_redacts_sensitive_values(tmp_path):
    async def healthy():
        return {"model": "small"}

    report = asyncio.run(
        DiagnosticRunner([DiagnosticCheck("asr", healthy)]).run()
    )
    destination = tmp_path / "diagnostics.zip"
    log = (
        "user=C:\\Users\\Alice\\Desktop email=a@example.com "
        "Bearer abcdefghijklmnopqrstuvwxyz ip=192.168.1.8 "
        "id=123e4567-e89b-12d3-a456-426614174000"
    )

    DiagnosticExporter().export(
        destination,
        report,
        config_summary={"version": 1, "api_key": "must-not-export"},
        log_text=log,
    )

    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {
            "diagnostics.json",
            "config-summary.json",
            "logs-tail.txt",
        }
        combined = b"".join(archive.read(name) for name in archive.namelist()).decode()
        assert "Alice" not in combined
        assert "a@example.com" not in combined
        assert "192.168.1.8" not in combined
        assert "must-not-export" not in combined
        assert "[REDACTED]" in combined
        parsed = json.loads(archive.read("diagnostics.json"))
        assert parsed["overall_status"] == "healthy"


def test_exporter_rejects_existing_destination_and_caps_log_tail(tmp_path):
    report = asyncio.run(DiagnosticRunner([]).run())
    destination = tmp_path / "diagnostics.zip"
    destination.write_bytes(b"existing")

    import pytest

    with pytest.raises(DiagnosticError):
        DiagnosticExporter(max_log_bytes=16).export(
            destination,
            report,
            config_summary={},
            log_text="x" * 100,
        )
    assert destination.read_bytes() == b"existing"


def test_diagnostic_service_requires_fresh_report_before_local_export(tmp_path):
    async def healthy():
        return {"running": True}

    service = DiagnosticService(
        DiagnosticRunner([DiagnosticCheck("runtime", healthy)]),
        DiagnosticExporter(),
        config_summary=lambda: {"config_version": 1},
        log_text=lambda: "local log",
    )

    import pytest

    with pytest.raises(DiagnosticError, match="运行"):
        asyncio.run(service.export(tmp_path / "before.zip"))

    report = asyncio.run(service.run())
    destination = tmp_path / "diagnostics.zip"
    asyncio.run(service.export(destination))

    assert report.results[0].component == "runtime"
    assert destination.is_file()
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("logs-tail.txt").decode() == "local log"
