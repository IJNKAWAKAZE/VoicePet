import base64
import ctypes
import json
import subprocess

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


class _PowerCommandRecorder:
    """记录关机命令；Popen 直接失败，避免测试真的排上关机任务"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.reply = subprocess.CompletedProcess(["shutdown"], returncode, stdout, stderr)
        self.commands: list[list[str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(computer_use.subprocess, "run", self.run)
        monkeypatch.setattr(computer_use.subprocess, "Popen", self.popen)

    def run(self, command, **kwargs):
        self.commands.append(list(command))
        return self.reply

    def popen(self, *args, **kwargs):
        raise AssertionError("关机不应使用不检查结果的 Popen")


class _PowerApiStub:
    """电源与锁屏 API 的假实现，避免测试真的睡眠或锁定本机"""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: list[str] = []

    def LockWorkStation(self) -> bool:
        self.calls.append("lock")
        return self.accepted

    def SetSystemPowerState(self, suspend: bool, force: bool) -> bool:
        self.calls.append("suspend")
        return self.accepted


class _WindllStub:
    """替换 ctypes.windll，拦住绕过 computer_use 直接调用系统 API 的写法"""

    def __init__(self, api: _PowerApiStub) -> None:
        self.kernel32 = api
        self.user32 = api


def enable_full_auto(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEPET_MODE_FILE", str(tmp_path / "mode.json"))
    assert write_approval_mode(tmp_path / "mode.json", AgentApprovalMode.FULL_AUTO)


def stub_power_apis(monkeypatch, accepted=True):
    api = _PowerApiStub(accepted)
    monkeypatch.setattr(computer_use, "_kernel32", lambda: api)
    monkeypatch.setattr(computer_use, "_user32", lambda: api)
    monkeypatch.setattr(ctypes, "windll", _WindllStub(api))
    return api


def test_shutdown_computer_reports_scheduled_request(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    recorder = _PowerCommandRecorder()
    recorder.install(monkeypatch)

    payload = mcp_server.call("shutdown_computer", {"action": "restart", "delay_seconds": "90"})

    assert payload["isError"] is False
    assert json.loads(payload["content"][0]["text"]) == {"action": "restart", "delay_seconds": 90}
    assert recorder.commands == [["shutdown", "/r", "/t", "90"]]


def test_shutdown_computer_reports_request_rejected_by_system(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    recorder = _PowerCommandRecorder(returncode=1, stdout="Usage: shutdown [/i | /l | /s]")
    recorder.install(monkeypatch)

    payload = mcp_server.call("shutdown_computer", {"action": "shutdown", "delay_seconds": 60})

    assert payload["isError"] is True
    assert "关机请求未被系统接受" in payload["content"][0]["text"]
    assert recorder.commands == [["shutdown", "/s", "/t", "60"]]


@pytest.mark.parametrize("delay", [999999999, -1, "一分钟"])
def test_shutdown_computer_rejects_invalid_delay(monkeypatch, tmp_path, delay):
    enable_full_auto(monkeypatch, tmp_path)
    recorder = _PowerCommandRecorder()
    recorder.install(monkeypatch)

    payload = mcp_server.call("shutdown_computer", {"action": "shutdown", "delay_seconds": delay})

    assert payload["isError"] is True
    assert "delay_seconds" in payload["content"][0]["text"]
    assert recorder.commands == []


def test_shutdown_computer_reports_cancel_without_pending_task(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    recorder = _PowerCommandRecorder(returncode=1116, stdout="Unable to abort the system shutdown because no shutdown was in progress.(1116)")
    recorder.install(monkeypatch)

    payload = mcp_server.call("shutdown_computer", {"action": "cancel"})

    assert payload["isError"] is False
    assert json.loads(payload["content"][0]["text"]) == {"cancelled": False, "reason": "当前没有等待执行的关机或重启任务"}


def test_shutdown_computer_reports_cancel_failure(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    recorder = _PowerCommandRecorder(returncode=5, stderr="拒绝访问。")
    recorder.install(monkeypatch)

    payload = mcp_server.call("shutdown_computer", {"action": "cancel"})

    assert payload["isError"] is True
    assert "拒绝访问" in payload["content"][0]["text"]


def test_shutdown_computer_rejects_unknown_action(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    recorder = _PowerCommandRecorder()
    recorder.install(monkeypatch)

    payload = mcp_server.call("shutdown_computer", {"action": "reboot"})

    assert payload["isError"] is True
    assert recorder.commands == []


def test_sleep_computer_reports_sleeping(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    api = stub_power_apis(monkeypatch)

    payload = mcp_server.call("sleep_computer", {})

    assert payload["isError"] is False
    assert json.loads(payload["content"][0]["text"]) == {"sleeping": True}
    assert api.calls == ["suspend"]


def test_lock_screen_reports_refusal(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    api = stub_power_apis(monkeypatch, accepted=False)

    payload = mcp_server.call("lock_screen", {})

    assert payload["isError"] is True
    assert api.calls == ["lock"]


@pytest.mark.parametrize("mode", [AgentApprovalMode.SUGGEST, AgentApprovalMode.AUTO_EDIT])
@pytest.mark.parametrize("tool,args", [("shutdown_computer", {"action": "shutdown"}), ("sleep_computer", {}), ("lock_screen", {})])
def test_power_tools_are_blocked_outside_full_auto(monkeypatch, tmp_path, mode, tool, args):
    monkeypatch.setenv("VOICEPET_MODE_FILE", str(tmp_path / "mode.json"))
    assert write_approval_mode(tmp_path / "mode.json", mode)
    api = stub_power_apis(monkeypatch)
    recorder = _PowerCommandRecorder()
    recorder.install(monkeypatch)

    payload = mcp_server.call(tool, args)

    assert payload["isError"] is True
    assert "全自动模式" in payload["content"][0]["text"]
    assert api.calls == []
    assert recorder.commands == []


def test_respond_answers_known_methods_and_ignores_notifications():
    initialize = mcp_server.respond('{"jsonrpc":"2.0","id":1,"method":"initialize"}')
    assert initialize["id"] == 1
    assert initialize["result"]["serverInfo"]["name"] == "voicepet"

    listed = mcp_server.respond('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
    assert [tool["name"] for tool in listed["result"]["tools"]] == [tool["name"] for tool in mcp_server.TOOLS]

    assert mcp_server.respond('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
    assert mcp_server.respond('{"jsonrpc":"2.0","id":3,"method":"no/such/method"}')["error"]["code"] == -32601
    assert mcp_server.respond('{"jsonrpc":"2.0","method":"no/such/method"}') is None


def test_respond_survives_broken_lines_and_odd_parameters(monkeypatch, tmp_path):
    enable_full_auto(monkeypatch, tmp_path)
    recorder = _PowerCommandRecorder()
    recorder.install(monkeypatch)

    assert mcp_server.respond("{不是 JSON")["error"]["code"] == -32700
    assert mcp_server.respond("[1, 2, 3]")["error"]["code"] == -32600
    assert mcp_server.respond('{"jsonrpc":"2.0","id":4,"method":"tools/call","params":[]}')["result"]["isError"] is True

    info = mcp_server.respond('{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"get_system_info","arguments":"坏参数"}}')
    assert info["result"]["isError"] is False
    assert "system" in json.loads(info["result"]["content"][0]["text"])

    cancelled = mcp_server.respond('{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"shutdown_computer","arguments":{"action":"cancel"}}}')
    assert cancelled["result"]["isError"] is False
    assert recorder.commands == [["shutdown", "/a"]]
