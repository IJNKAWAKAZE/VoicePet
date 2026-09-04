import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from core.cancellation import CancellationSource, CancelledError
from core.controlled_shell import ControlledShellTool
from core.path_security import ScopedPathResolver
from core.policy import (
    ConfirmationMode,
    PolicyEngine,
    PolicySettings,
    RiskLevel,
    ToolProposal,
    ToolRegistry,
)
from core.process_runner import ProcessOutcome, ProcessStartError
from core.tool_types import ToolExecutionStatus


class FakeRunner:
    def __init__(self, result=None, error=None) -> None:
        self.result = result or ProcessOutcome(0, "ok", "", False, False)
        self.error = error
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def shell_tool(tmp_path, *, enabled=True, runner=None):
    return ControlledShellTool(
        {"python": Path(sys.executable)},
        ScopedPathResolver([tmp_path]),
        runner=runner or FakeRunner(),
        enabled=enabled,
        max_timeout=10,
        output_limit=1024,
    )


def request(tmp_path, **overrides):
    values = {
        "program": "python",
        "args": ["--version"],
        "cwd": str(tmp_path),
        "timeout": 5,
    }
    values.update(overrides)
    return values


def test_controlled_shell_is_disabled_by_default(tmp_path):
    runner = FakeRunner()
    tool = ControlledShellTool(
        {"python": Path(sys.executable)},
        ScopedPathResolver([tmp_path]),
        runner=runner,
    )

    result = asyncio.run(
        tool.execute(request(tmp_path), CancellationSource().token)
    )

    assert result.status is ToolExecutionStatus.DENIED
    assert runner.calls == []


def test_controlled_shell_manifest_is_r3_and_requires_ui_confirmation(tmp_path):
    tool = shell_tool(tmp_path)
    decision = PolicyEngine(
        ToolRegistry([tool.manifest]),
        PolicySettings(),
    ).evaluate(
        ToolProposal("call-1", "controlled_shell", request(tmp_path))
    )

    assert tool.manifest.base_risk is RiskLevel.R3
    assert decision.allowed is True
    assert decision.confirmation is ConfirmationMode.UI


def test_controlled_shell_passes_only_validated_values_to_runner(tmp_path):
    runner = FakeRunner(ProcessOutcome(0, "Python", "", False, False))
    tool = shell_tool(tmp_path, runner=runner)
    source = CancellationSource()

    result = asyncio.run(tool.execute(request(tmp_path), source.token))

    assert result.status is ToolExecutionStatus.SUCCESS
    assert result.payload["stdout"] == "Python"
    assert runner.calls == [
        {
            "program": Path(sys.executable).resolve(),
            "args": ("--version",),
            "cwd": tmp_path.resolve(),
            "timeout": 5.0,
            "output_limit": 1024,
            "token": source.token,
            "environment": None,
        }
    ]


@pytest.mark.parametrize(
    "argument",
    [
        "one;two",
        "one&&two",
        "one||two",
        "one|two",
        "one>file",
        "one<input",
        "line\nbreak",
        "`command`",
        "$(command)",
        "%TEMP%",
        "$env:TEMP",
    ],
)
def test_controlled_shell_rejects_shell_syntax(tmp_path, argument):
    runner = FakeRunner()
    tool = shell_tool(tmp_path, runner=runner)

    result = asyncio.run(
        tool.execute(
            request(tmp_path, args=[argument]),
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.DENIED
    assert runner.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"program": "cmd"},
        {"args": ["a"] * 33},
        {"args": ["a" * 513]},
        {"timeout": 11},
    ],
)
def test_controlled_shell_rejects_unapproved_program_or_limits(
    tmp_path,
    overrides,
):
    runner = FakeRunner()
    tool = shell_tool(tmp_path, runner=runner)

    result = asyncio.run(
        tool.execute(
            request(tmp_path, **overrides),
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.DENIED
    assert runner.calls == []


def test_controlled_shell_rejects_cwd_outside_scope(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    runner = FakeRunner()
    tool = ControlledShellTool(
        {"python": Path(sys.executable)},
        ScopedPathResolver([allowed]),
        runner=runner,
        enabled=True,
    )

    result = asyncio.run(
        tool.execute(
            request(outside),
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.DENIED
    assert runner.calls == []


def test_controlled_shell_summary_uses_windows_command_rendering(tmp_path):
    tool = shell_tool(tmp_path)
    arguments = request(tmp_path, args=["-c", "print('hello world')"])

    decision = PolicyEngine(
        ToolRegistry([tool.manifest]),
        PolicySettings(),
    ).evaluate(ToolProposal("call-1", "controlled_shell", arguments))

    expected = subprocess.list2cmdline(
        [str(Path(sys.executable).resolve()), "-c", "print('hello world')"]
    )
    assert expected in decision.summary


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ProcessOutcome(0, "ok", "", False, False), ToolExecutionStatus.SUCCESS),
        (ProcessOutcome(1, "", "bad", False, False), ToolExecutionStatus.FAILED),
        (ProcessOutcome(-1, "", "", True, False), ToolExecutionStatus.TIMEOUT),
        (ProcessOutcome(0, "cut", "", False, True), ToolExecutionStatus.PARTIAL),
    ],
)
def test_controlled_shell_maps_process_outcomes(tmp_path, outcome, expected):
    tool = shell_tool(tmp_path, runner=FakeRunner(outcome))

    result = asyncio.run(
        tool.execute(request(tmp_path), CancellationSource().token)
    )

    assert result.status is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CancelledError("interrupt"), ToolExecutionStatus.CANCELLED),
        (ProcessStartError("private command"), ToolExecutionStatus.FAILED),
    ],
)
def test_controlled_shell_maps_runner_errors_safely(tmp_path, error, expected):
    tool = shell_tool(tmp_path, runner=FakeRunner(error=error))

    result = asyncio.run(
        tool.execute(request(tmp_path), CancellationSource().token)
    )

    assert result.status is expected
    assert "private command" not in result.safe_message
