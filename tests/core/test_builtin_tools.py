import asyncio
import shutil
import sys

from core.builtin_tools import MoveFileTool, OpenAppTool, SystemInfoTool
from core.cancellation import CancellationSource
from core.path_security import ScopedPathResolver
from core.policy import (
    PolicyEngine,
    PolicySettings,
    RiskLevel,
    ToolProposal,
    ToolRegistry,
)
from core.tool_types import ToolExecutionStatus


class FakeLauncher:
    def __init__(self, result=42, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    async def launch(self, argv, token):
        self.calls.append((argv, token))
        if self.error is not None:
            raise self.error
        return self.result


def test_system_info_tool_has_r0_manifest_and_deterministic_payload():
    tool = SystemInfoTool(
        info_provider=lambda: {
            "system": "Windows",
            "release": "11",
            "machine": "AMD64",
            "python": "3.12.14",
        }
    )
    source = CancellationSource()

    result = asyncio.run(tool.execute({}, source.token))

    assert tool.manifest.name == "system_info"
    assert tool.manifest.base_risk is RiskLevel.R0
    assert result.status is ToolExecutionStatus.SUCCESS
    assert dict(result.payload) == {
        "system": "Windows",
        "release": "11",
        "machine": "AMD64",
        "python": "3.12.14",
    }


def test_open_app_tool_uses_exact_local_allowlist_argv(tmp_path):
    launcher = FakeLauncher(321)
    executable = str(sys.executable)
    tool = OpenAppTool(
        {"calculator": (executable, "-c", "pass")},
        launcher=launcher,
    )
    source = CancellationSource()

    result = asyncio.run(
        tool.execute({"name": "calculator"}, source.token)
    )

    assert tool.manifest.base_risk is RiskLevel.R1
    assert launcher.calls[0][0] == (executable, "-c", "pass")
    assert result.status is ToolExecutionStatus.SUCCESS
    assert result.payload["application"] == "calculator"
    assert result.payload["pid"] == 321


def test_open_app_tool_denies_unknown_and_sanitizes_launch_failure():
    launcher = FakeLauncher(error=RuntimeError("secret command"))
    tool = OpenAppTool(
        {"calculator": (str(sys.executable),)},
        launcher=launcher,
    )
    source = CancellationSource()

    unknown = asyncio.run(tool.execute({"name": "other"}, source.token))
    failed = asyncio.run(
        tool.execute({"name": "calculator"}, source.token)
    )

    assert unknown.status is ToolExecutionStatus.DENIED
    assert launcher.calls[-1][0] == (str(sys.executable),)
    assert failed.status is ToolExecutionStatus.FAILED
    assert "secret command" not in failed.safe_message


def test_open_app_policy_denies_unknown_application_before_execution():
    tool = OpenAppTool(
        {"calculator": (str(sys.executable),)},
        launcher=FakeLauncher(),
    )
    engine = PolicyEngine(ToolRegistry([tool.manifest]), PolicySettings())

    decision = engine.evaluate(
        ToolProposal("call-1", "open_app", {"name": "other"})
    )

    assert decision.allowed is False


def test_move_file_tool_moves_without_overwrite_and_saves_undo_data(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    source_path = root / "source.txt"
    destination = root / "destination.txt"
    source_path.write_text("content", encoding="utf-8")
    tool = MoveFileTool(ScopedPathResolver([root]))
    source = CancellationSource()

    result = asyncio.run(
        tool.execute(
            {"source": str(source_path), "destination": str(destination)},
            source.token,
        )
    )

    assert tool.manifest.base_risk is RiskLevel.R2
    assert tool.manifest.supports_undo is True
    assert result.status is ToolExecutionStatus.SUCCESS
    assert not source_path.exists()
    assert destination.read_text(encoding="utf-8") == "content"
    assert result.undo_data == {
        "source": str(source_path.resolve()),
        "destination": str(destination.resolve()),
    }


def test_move_file_tool_denies_scope_escape_and_existing_destination(tmp_path):
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    source_path = root / "source.txt"
    source_path.write_text("content", encoding="utf-8")
    existing = root / "existing.txt"
    existing.write_text("existing", encoding="utf-8")
    tool = MoveFileTool(ScopedPathResolver([root]))
    source = CancellationSource()

    escaped = asyncio.run(
        tool.execute(
            {
                "source": str(source_path),
                "destination": str(outside / "target.txt"),
            },
            source.token,
        )
    )
    overwrite = asyncio.run(
        tool.execute(
            {"source": str(source_path), "destination": str(existing)},
            source.token,
        )
    )

    assert escaped.status is ToolExecutionStatus.DENIED
    assert overwrite.status is ToolExecutionStatus.DENIED
    assert source_path.exists()


def test_move_file_reports_partial_if_cancelled_after_side_effect(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    source_path = root / "source.txt"
    destination = root / "destination.txt"
    source_path.write_text("content", encoding="utf-8")
    cancellation = CancellationSource()

    def move_and_cancel(source, target):
        shutil.move(source, target)
        cancellation.cancel("interrupt")

    tool = MoveFileTool(
        ScopedPathResolver([root]),
        move_function=move_and_cancel,
    )

    result = asyncio.run(
        tool.execute(
            {"source": str(source_path), "destination": str(destination)},
            cancellation.token,
        )
    )

    assert result.status is ToolExecutionStatus.PARTIAL
    assert destination.exists()
    assert result.undo_data is not None


def test_move_file_sanitizes_operating_system_failure(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    source_path = root / "source.txt"
    destination = root / "destination.txt"
    source_path.write_text("content", encoding="utf-8")

    def fail(source, target):
        raise OSError(f"private paths {source} {target}")

    tool = MoveFileTool(
        ScopedPathResolver([root]),
        move_function=fail,
    )
    result = asyncio.run(
        tool.execute(
            {"source": str(source_path), "destination": str(destination)},
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.FAILED
    assert "private paths" not in result.safe_message
