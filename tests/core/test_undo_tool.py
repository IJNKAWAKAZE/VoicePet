import asyncio
import shutil
from uuid import uuid4

from core.audit import AuditContext, AuditStore, AuditUndoState
from core.builtin_tools import MoveFileTool, MoveFileUndoTool
from core.cancellation import CancellationSource
from core.path_security import ScopedPathResolver
from core.policy import (
    ConfirmationMode,
    PolicyDecision,
    PolicyEngine,
    PolicySettings,
    RiskLevel,
    ToolProposal,
    ToolRegistry,
)
from core.tool_types import ToolExecutionResult, ToolExecutionStatus


def create_moved_record(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir(exist_ok=True)
    source = root / "source.txt"
    destination = root / "destination.txt"
    destination.write_text("content", encoding="utf-8")
    resolver = ScopedPathResolver([root])
    move_tool = MoveFileTool(resolver)
    store = AuditStore(tmp_path / "audit.db")
    call_id = "call-move"
    original = store.write(
        context=AuditContext(str(uuid4()), str(uuid4()), call_id),
        manifest=move_tool.manifest,
        arguments={"source": str(source), "destination": str(destination)},
        decision=PolicyDecision(
            call_id,
            "move_file",
            True,
            RiskLevel.R2,
            ConfirmationMode.VOICE,
            "移动文件",
            "a" * 64,
            1,
            "允许执行",
        ),
        result=ToolExecutionResult(
            ToolExecutionStatus.SUCCESS,
            {"moved": True},
            "移动完成",
            {"source": str(source), "destination": str(destination)},
        ),
        duration_ms=1,
    )
    return store, resolver, original, source, destination


def test_undo_tool_manifest_and_policy_summary_are_r2(tmp_path):
    store, resolver, original, source, destination = create_moved_record(tmp_path)
    tool = MoveFileUndoTool(store, resolver)
    proposal = ToolProposal(
        "call-undo",
        "undo_move_file",
        {"audit_id": original.audit_id},
    )

    decision = PolicyEngine(
        ToolRegistry([tool.manifest]),
        PolicySettings(),
    ).evaluate(proposal)

    assert tool.manifest.base_risk is RiskLevel.R2
    assert tool.manifest.supports_undo is True
    assert decision.allowed is True
    assert source.name in decision.summary
    assert destination.name in decision.summary
    store.close()


def test_undo_tool_moves_file_back_and_completes_original_record(tmp_path):
    store, resolver, original, source, destination = create_moved_record(tmp_path)
    tool = MoveFileUndoTool(store, resolver)

    result = asyncio.run(
        tool.execute(
            {"audit_id": original.audit_id},
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.SUCCESS
    assert source.read_text(encoding="utf-8") == "content"
    assert not destination.exists()
    assert result.payload["original_audit_id"] == original.audit_id
    assert result.payload["operation_id"]
    assert result.undo_data == {
        "source": str(destination),
        "destination": str(source),
    }
    loaded = store.get(original.audit_id)
    assert loaded.undo_state is AuditUndoState.COMPLETED
    assert loaded.undone_by == result.payload["operation_id"]
    store.close()


def test_undo_tool_denies_duplicate_missing_and_resource_drift(tmp_path):
    store, resolver, original, _, _ = create_moved_record(tmp_path)
    tool = MoveFileUndoTool(store, resolver)
    first = asyncio.run(
        tool.execute(
            {"audit_id": original.audit_id},
            CancellationSource().token,
        )
    )
    duplicate = asyncio.run(
        tool.execute(
            {"audit_id": original.audit_id},
            CancellationSource().token,
        )
    )
    missing = asyncio.run(
        tool.execute(
            {"audit_id": str(uuid4())},
            CancellationSource().token,
        )
    )

    assert first.status is ToolExecutionStatus.SUCCESS
    assert duplicate.status is ToolExecutionStatus.DENIED
    assert missing.status is ToolExecutionStatus.DENIED
    store.close()


def test_undo_tool_releases_reservation_after_move_failure(tmp_path):
    store, resolver, original, source, destination = create_moved_record(tmp_path)

    def fail(source_path, destination_path):
        raise OSError(f"private {source_path} {destination_path}")

    tool = MoveFileUndoTool(store, resolver, move_function=fail)

    result = asyncio.run(
        tool.execute(
            {"audit_id": original.audit_id},
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.FAILED
    assert "private" not in result.safe_message
    assert store.get(original.audit_id).undo_state is AuditUndoState.AVAILABLE
    assert destination.exists()
    assert not source.exists()
    store.close()


def test_undo_tool_reports_partial_when_cancelled_after_move(tmp_path):
    store, resolver, original, source, _ = create_moved_record(tmp_path)
    cancellation = CancellationSource()

    def move_and_cancel(source_path, destination_path):
        shutil.move(source_path, destination_path)
        cancellation.cancel("interrupt")

    tool = MoveFileUndoTool(
        store,
        resolver,
        move_function=move_and_cancel,
    )

    result = asyncio.run(
        tool.execute(
            {"audit_id": original.audit_id},
            cancellation.token,
        )
    )

    assert result.status is ToolExecutionStatus.PARTIAL
    assert source.exists()
    assert store.get(original.audit_id).undo_state is AuditUndoState.COMPLETED
    store.close()
