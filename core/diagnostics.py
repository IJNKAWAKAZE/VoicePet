"""并发组件健康检查与默认不上传的脱敏诊断包"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType


class DiagnosticError(RuntimeError):
    """诊断检查或本地导出失败"""

    code = "diagnostic.error"


class DiagnosticStatus(str, Enum):
    """组件健康程度的稳定状态"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


CheckOutput = (
    Mapping[str, object]
    | tuple[DiagnosticStatus, str, Mapping[str, object]]
)


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """命名且有独立超时的异步健康检查"""

    component: str
    check: Callable[[], Awaitable[CheckOutput]]
    timeout: float = 5.0

    def __post_init__(self) -> None:
        if not self.component.strip() or self.timeout <= 0:
            raise DiagnosticError("诊断检查配置无效")


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """不含敏感正文的单组件诊断结果"""

    component: str
    status: DiagnosticStatus
    safe_message: str
    duration_ms: int
    context: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """一次并发诊断运行的不可变汇总"""

    created_at_utc: datetime
    results: tuple[DiagnosticResult, ...]

    @property
    def overall_status(self) -> DiagnosticStatus:
        statuses = {result.status for result in self.results}
        if DiagnosticStatus.UNAVAILABLE in statuses:
            return DiagnosticStatus.UNAVAILABLE
        if DiagnosticStatus.DEGRADED in statuses:
            return DiagnosticStatus.DEGRADED
        return DiagnosticStatus.HEALTHY


class DiagnosticRunner:
    """保持注册顺序并发运行且隔离组件失败"""

    def __init__(self, checks: Sequence[DiagnosticCheck]) -> None:
        names = [check.component for check in checks]
        if len(names) != len(set(names)):
            raise DiagnosticError("诊断组件名称重复")
        self._checks = tuple(checks)

    async def run(self) -> DiagnosticReport:
        results = await asyncio.gather(
            *(self._run_one(check) for check in self._checks)
        )
        return DiagnosticReport(datetime.now(UTC), tuple(results))

    async def run_component(self, component: str) -> DiagnosticResult:
        """仅运行指定组件并保留独立超时和失败隔离"""
        for check in self._checks:
            if check.component == component:
                return await self._run_one(check)
        raise DiagnosticError("诊断组件未注册")

    @staticmethod
    async def _run_one(check: DiagnosticCheck) -> DiagnosticResult:
        started = time.monotonic()
        try:
            async with asyncio.timeout(check.timeout):
                output = await check.check()
            if isinstance(output, tuple):
                status, message, context = output
            else:
                status = DiagnosticStatus.HEALTHY
                message = "组件检查正常"
                context = output
        except TimeoutError:
            status = DiagnosticStatus.UNAVAILABLE
            message = "组件检查超时"
            context = {}
        except Exception as error:  # noqa: BLE001 每项诊断必须隔离外部组件失败
            status = DiagnosticStatus.UNAVAILABLE
            message = f"组件检查失败: {type(error).__name__}"
            context = {}
        duration = max(0, round((time.monotonic() - started) * 1000))
        return DiagnosticResult(check.component, status, message, duration, context)


class DiagnosticExporter:
    """创建固定条目且二次脱敏的本地诊断 ZIP"""

    _REDACTIONS = (
        re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~-]{12,}"),
        re.compile(r"(?i)(?:api[_-]?key|access[_-]?key)\s*[:=]\s*\S+"),
        re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s]+"),
        re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,36}\b"),
    )
    _CONFIG_ALLOWLIST = frozenset(
        {"version", "config_version", "platform", "python", "provider", "model"}
    )

    def __init__(self, *, max_log_bytes: int = 256 * 1024) -> None:
        if max_log_bytes <= 0:
            raise DiagnosticError("诊断日志上限必须大于零")
        self._max_log_bytes = max_log_bytes

    def export(
        self,
        destination: str | Path,
        report: DiagnosticReport,
        *,
        config_summary: Mapping[str, object],
        log_text: str,
    ) -> None:
        destination = Path(destination)
        if destination.exists():
            raise DiagnosticError("诊断包目标文件已存在")
        if not destination.parent.exists():
            raise DiagnosticError("诊断包目标目录不存在")
        diagnostics = {
            "created_at_utc": report.created_at_utc.isoformat(),
            "overall_status": report.overall_status.value,
            "results": [
                {
                    "component": item.component,
                    "status": item.status.value,
                    "safe_message": self._redact(item.safe_message),
                    "duration_ms": item.duration_ms,
                    "context": dict(item.context),
                }
                for item in report.results
            ],
        }
        safe_config = {
            key: value
            for key, value in config_summary.items()
            if key in self._CONFIG_ALLOWLIST
        }
        encoded_log = self._redact(log_text).encode("utf-8", errors="replace")
        encoded_log = encoded_log[-self._max_log_bytes :]
        entries = {
            "diagnostics.json": json.dumps(diagnostics, ensure_ascii=False, indent=2),
            "config-summary.json": json.dumps(safe_config, ensure_ascii=False, indent=2),
            "logs-tail.txt": encoded_log.decode("utf-8", errors="replace"),
        }
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".voicepet-diagnostics-",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(raw_path)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in entries.items():
                    archive.writestr(name, content)
            os.replace(temporary, destination)
        except OSError as error:
            raise DiagnosticError("诊断包写入失败") from error
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def _redact(cls, value: str) -> str:
        redacted = value
        for pattern in cls._REDACTIONS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted


class DiagnosticService:
    """保存最近一次报告并协调本地脱敏导出"""

    def __init__(
        self,
        runner: DiagnosticRunner,
        exporter: DiagnosticExporter,
        *,
        config_summary: Callable[[], Mapping[str, object]],
        log_text: Callable[[], str],
    ) -> None:
        self._runner = runner
        self._exporter = exporter
        self._config_summary = config_summary
        self._log_text = log_text
        self._last_report: DiagnosticReport | None = None

    @property
    def last_report(self) -> DiagnosticReport | None:
        return self._last_report

    async def run(self) -> DiagnosticReport:
        report = await self._runner.run()
        self._last_report = report
        return report

    async def run_component(self, component: str) -> DiagnosticResult:
        """单项检查不替换供导出使用的完整报告"""
        return await self._runner.run_component(component)

    async def export(self, destination: str | Path) -> None:
        report = self._last_report
        if report is None:
            raise DiagnosticError("请先运行诊断检查")
        config = self._config_summary()
        log = self._log_text()
        await asyncio.to_thread(
            self._exporter.export,
            destination,
            report,
            config_summary=config,
            log_text=log,
        )
