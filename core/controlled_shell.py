"""默认关闭且只允许固定程序与参数的受控 Shell 工具"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from .cancellation import CancellationToken, CancelledError
from .path_security import PathSecurityError, ScopedPathResolver
from .policy import (
    ConcurrencyPolicy,
    JsonValue,
    RiskLevel,
    ToolManifest,
)
from .process_runner import AsyncSubprocessRunner, ProcessOutcome
from .tool_types import ToolExecutionError, ToolExecutionResult, ToolExecutionStatus


class ControlledShellTool:
    """通过 exec-only runner 执行严格校验的开发者命令"""

    _FORBIDDEN_ARGUMENT = re.compile(
        r"[\r\n;|<>`]|&&|\|\||\$\(|\$env:|%[^%]+%",
        re.IGNORECASE,
    )

    def __init__(
        self,
        programs: Mapping[str, Path],
        resolver: ScopedPathResolver,
        *,
        runner: AsyncSubprocessRunner | None = None,
        enabled: bool = False,
        max_timeout: float = 30.0,
        output_limit: int = 65536,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if max_timeout <= 0 or output_limit <= 0:
            raise ValueError("受控 Shell 限制必须大于零")
        normalized_programs: dict[str, Path] = {}
        for name, raw_path in programs.items():
            path = Path(raw_path)
            if not name or not path.is_absolute():
                raise ValueError("受控 Shell 程序白名单无效")
            try:
                resolved = path.resolve(strict=True)
            except OSError as error:
                raise ValueError("受控 Shell 白名单程序不存在") from error
            if not resolved.is_file():
                raise ValueError("受控 Shell 白名单程序必须是文件")
            normalized_programs[name] = resolved
        self._programs = MappingProxyType(normalized_programs)
        self._resolver = resolver
        self._runner = runner or AsyncSubprocessRunner()
        self._enabled = enabled
        self._max_timeout = max_timeout
        self._output_limit = output_limit
        self._environment = (
            None if environment is None else MappingProxyType(dict(environment))
        )
        self.manifest = ToolManifest(
            name="controlled_shell",
            description="执行受控开发者命令",
            input_schema={
                "type": "object",
                "maxProperties": 4,
                "properties": {
                    "program": {"type": "string", "maxLength": 64},
                    "args": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {"type": "string", "maxLength": 512},
                    },
                    "cwd": {"type": "string", "maxLength": 32767},
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": max_timeout,
                    },
                },
                "required": ["program", "args", "cwd", "timeout"],
                "additionalProperties": False,
            },
            required_permissions=frozenset({"developer.shell"}),
            base_risk=RiskLevel.R3,
            timeout=max_timeout,
            concurrency_policy=ConcurrencyPolicy.SERIAL,
            supports_cancel=True,
            sensitive_fields=("args", "cwd"),
            risk_evaluator=self._risk,
            impact_summarizer=self._summary,
        )

    async def execute(
        self,
        arguments: Mapping[str, JsonValue],
        token: CancellationToken,
    ) -> ToolExecutionResult:
        if not self._enabled:
            return ToolExecutionResult(
                ToolExecutionStatus.DENIED,
                {},
                "受控 Shell 未启用",
            )
        token.throw_if_cancelled()
        try:
            program, args, cwd, timeout = self._parse_request(arguments)
        except (PathSecurityError, TypeError, ValueError):
            return ToolExecutionResult(
                ToolExecutionStatus.DENIED,
                {},
                "受控 Shell 请求未通过本地校验",
            )
        try:
            outcome = await self._runner.run(
                program=program,
                args=args,
                cwd=cwd,
                timeout=timeout,
                output_limit=self._output_limit,
                token=token,
                environment=self._environment,
            )
        except CancelledError:
            return ToolExecutionResult(
                ToolExecutionStatus.CANCELLED,
                {},
                "受控命令已取消",
            )
        except ToolExecutionError as error:
            return ToolExecutionResult(
                ToolExecutionStatus.FAILED,
                {"error_type": type(error).__name__},
                "受控命令启动失败",
            )
        return self._result_from_outcome(outcome)

    def _parse_request(
        self,
        arguments: Mapping[str, JsonValue],
    ) -> tuple[Path, tuple[str, ...], Path, float]:
        if set(arguments) != {"program", "args", "cwd", "timeout"}:
            raise ValueError("request fields are invalid")
        program_name = arguments.get("program")
        raw_args = arguments.get("args")
        raw_cwd = arguments.get("cwd")
        raw_timeout = arguments.get("timeout")
        if not isinstance(program_name, str) or program_name not in self._programs:
            raise ValueError("program is not allowed")
        if not isinstance(raw_args, (list, tuple)):
            raise TypeError("args must be a list")
        args = tuple(raw_args)
        if len(args) > 32 or any(
            not isinstance(argument, str)
            or len(argument) > 512
            or self._FORBIDDEN_ARGUMENT.search(argument)
            for argument in args
        ):
            raise ValueError("arguments are not allowed")
        if not isinstance(raw_cwd, str):
            raise TypeError("cwd must be a string")
        cwd = self._resolver.directory(raw_cwd)
        if (
            isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, (int, float))
            or raw_timeout <= 0
            or raw_timeout > self._max_timeout
        ):
            raise ValueError("timeout is outside local limits")
        return self._programs[program_name], args, cwd, float(raw_timeout)

    def _risk(self, arguments: Mapping[str, JsonValue]) -> RiskLevel:
        if not self._enabled:
            raise ValueError("controlled shell is disabled")
        self._parse_request(arguments)
        return RiskLevel.R3

    def _summary(self, arguments: Mapping[str, JsonValue]) -> str:
        program, args, cwd, _ = self._parse_request(arguments)
        command = subprocess.list2cmdline([str(program), *args])
        return f"在 {cwd} 执行命令 {command}"

    @staticmethod
    def _result_from_outcome(outcome: ProcessOutcome) -> ToolExecutionResult:
        if outcome.timed_out:
            status = ToolExecutionStatus.TIMEOUT
            message = "受控命令执行超时"
        elif outcome.truncated:
            status = ToolExecutionStatus.PARTIAL
            message = "受控命令输出已截断"
        elif outcome.exit_code == 0:
            status = ToolExecutionStatus.SUCCESS
            message = "受控命令执行成功"
        else:
            status = ToolExecutionStatus.FAILED
            message = "受控命令执行失败"
        return ToolExecutionResult(
            status,
            {
                "exit_code": outcome.exit_code,
                "stdout": outcome.stdout,
                "stderr": outcome.stderr,
                "truncated": outcome.truncated,
            },
            message,
        )
