import base64
import json

import pytest

from core import computer_use, mcp_server
from core.agent_codex import write_approval_mode
from core.agent_types import AgentApprovalMode


def window(handle=7, title="记事本"):
    return computer_use.WindowInfo(handle, title, "notepad.exe", 10, 20, 800, 600, False, True)


def test_tools_expose_unique_names_and_required_properties():
    names = [entry["name"] for entry in mcp_server.TOOLS]

    assert len(names) == len(set(names))
    for entry in mcp_server.TOOLS:
        assert entry["description"]
        assert entry["inputSchema"]["type"] == "object"
        for field in entry["inputSchema"].get("required", []):
            assert field in entry["inputSchema"]["properties"]


def test_desktop_tools_are_listed_for_the_agent():
    names = {entry["name"] for entry in mcp_server.TOOLS}

    assert {"list_windows", "get_window_state", "click", "scroll", "drag", "press_key"} <= names


def test_list_windows_tool_serializes_window_metadata(monkeypatch):
    monkeypatch.setattr(computer_use, "list_windows", lambda **kwargs: [window()])

    payload = mcp_server.call("list_windows", {"limit": 5})

    assert payload["isError"] is False
    assert json.loads(payload["content"][0]["text"]) == {
        "count": 1,
        "windows": [
            {
                "handle": 7,
                "title": "记事本",
                "process": "notepad.exe",
                "left": 10,
                "top": 20,
                "width": 800,
                "height": 600,
                "minimized": False,
                "foreground": True,
            }
        ],
    }


def test_get_window_state_tool_returns_png_image_content(monkeypatch):
    monkeypatch.setattr(computer_use, "resolve_window", lambda reference=None: 0)
    monkeypatch.setattr(
        computer_use,
        "capture_window",
        lambda handle, max_width=0: computer_use.Capture(b"\x89PNG\r\n\x1a\nrest", 640, 360, 3),
    )

    payload = mcp_server.call("get_window_state", {"window": "screen", "max_width": 640})

    assert payload["isError"] is False
    text, image = payload["content"]
    assert json.loads(text["text"]) == {"window": 0, "width": 640, "height": 360, "scaled_by": 3}
    assert image["type"] == "image"
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == b"\x89PNG\r\n\x1a\nrest"


def test_desktop_control_tools_run_in_full_auto_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEPET_MODE_FILE", str(tmp_path / "mode.json"))
    monkeypatch.setattr(computer_use, "click", lambda *args, **kwargs: {"clicked": 1})
    assert write_approval_mode(tmp_path / "mode.json", AgentApprovalMode.FULL_AUTO)

    assert mcp_server.call("click", {"x": 1, "y": 2})["isError"] is False


@pytest.mark.parametrize("mode", [AgentApprovalMode.SUGGEST, AgentApprovalMode.AUTO_EDIT])
def test_desktop_control_tools_are_blocked_outside_full_auto(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("VOICEPET_MODE_FILE", str(tmp_path / "mode.json"))
    monkeypatch.setattr(computer_use, "press_key", lambda sequence: {"pressed": sequence})
    assert write_approval_mode(tmp_path / "mode.json", mode)

    payload = mcp_server.call("press_key", {"sequence": "enter"})

    assert payload["isError"] is True
    assert "全自动模式" in payload["content"][0]["text"]


def test_read_only_and_file_tools_stay_available_outside_full_auto(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEPET_MODE_FILE", str(tmp_path / "mode.json"))
    monkeypatch.setattr(computer_use, "list_windows", lambda **kwargs: [])
    assert write_approval_mode(tmp_path / "mode.json", AgentApprovalMode.SUGGEST)

    assert json.loads(mcp_server.call("list_windows", {})["content"][0]["text"])["count"] == 0


def test_approval_mode_falls_back_to_suggest_without_shared_file(monkeypatch, tmp_path):
    monkeypatch.delenv("VOICEPET_MODE_FILE", raising=False)
    assert mcp_server.approval_mode() == "suggest"

    monkeypatch.setenv("VOICEPET_MODE_FILE", str(tmp_path / "missing.json"))
    assert mcp_server.approval_mode() == "suggest"

    broken = tmp_path / "broken.json"
    broken.write_text('{"version": 1}', encoding="utf-8")
    monkeypatch.setenv("VOICEPET_MODE_FILE", str(broken))
    assert mcp_server.approval_mode() == "suggest"


def test_unknown_tool_reports_error():
    payload = mcp_server.call("no_such_tool", {})

    assert payload["isError"] is True
    assert "未知工具" in payload["content"][0]["text"]
