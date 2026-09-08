import asyncio
import base64

from core.attachments import inspect_attachment
from core.builtin_tools import ReadFileTool
from core.cancellation import CancellationSource
from core.tool_types import ToolExecutionStatus


def test_inspect_attachment_returns_metadata_without_loading_content(tmp_path):
    path = tmp_path / "large.txt"
    content = b"voicepet" * 100
    path.write_bytes(content)

    attachment = inspect_attachment(path)

    assert attachment.path == str(path.resolve())
    assert attachment.name == "large.txt"
    assert attachment.media_type == "text/plain"
    assert attachment.size == len(content)
    assert len(attachment.sha256) == 64
    assert attachment.sha256 == inspect_attachment(path).sha256


def test_inspect_attachment_accepts_file_url_and_metadata_changes_update_digest(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("one", encoding="utf-8")
    first = inspect_attachment(path)
    second = inspect_attachment(path.as_uri())
    assert second.path == first.path
    assert second.sha256 == first.sha256
    path.write_text("two-updated", encoding="utf-8")
    assert inspect_attachment(path).sha256 != first.sha256


def test_read_file_tool_returns_requested_chunk_only(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * (1024 * 1024 + 8))
    tool = ReadFileTool(allowed_paths={path})

    result = asyncio.run(
        tool.execute(
            {"path": str(path), "offset": 1024, "length": 2048},
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.SUCCESS
    assert result.payload["bytes_read"] == 2048
    assert base64.b64decode(result.payload["data"]) == b"a" * 2048


def test_read_file_tool_denies_path_outside_current_message(tmp_path):
    allowed = tmp_path / "allowed.txt"
    outside = tmp_path / "outside.txt"
    allowed.write_text("allowed", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")
    tool = ReadFileTool(allowed_paths={allowed})

    result = asyncio.run(
        tool.execute(
            {"path": str(outside), "offset": 0, "length": 32},
            CancellationSource().token,
        )
    )

    assert result.status is ToolExecutionStatus.DENIED
    assert "secret" not in result.safe_message
