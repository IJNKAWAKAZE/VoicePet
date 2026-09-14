"""核对固定 SDK 的参数和原生工具审批回调"""

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

sdk = pytest.importorskip("openai_codex")
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import ThreadStartParams, TurnStartParams

from core.agent_codex import (
    APPROVAL_MODE_FILENAME,
    CODEX_SDK_VERSION,
    CodexAgentAdapter,
    codex_thread_options,
    codex_turn_options,
    require_supported_sdk,
    voicepet_mcp_overrides,
    write_approval_mode,
)
from core.agent_types import AgentApprovalMode, AgentEventType, AgentTurnRequest


def test_pinned_sdk_and_runtime():
    from codex_cli_bin import bundled_codex_path
    assert sdk.__version__ == CODEX_SDK_VERSION == "0.147.0"
    assert CodexConfig().experimental_api is True
    assert bundled_codex_path().is_file()
    require_supported_sdk()


@pytest.mark.parametrize("mode", list(AgentApprovalMode))
def test_options_match_generated_protocol(mode):
    thread = codex_thread_options(mode)
    turn = codex_turn_options(mode)
    ThreadStartParams.model_validate(thread)
    TurnStartParams.model_validate({**turn, "threadId": "thread", "input": []})
    expected_policy = "never" if mode is AgentApprovalMode.FULL_AUTO else "untrusted"
    assert thread["approvalPolicy"] == turn["approvalPolicy"] == expected_policy
    assert "dynamicTools" not in turn
    assert "dynamicTools" not in thread
    assert thread["sandbox"] == "danger-full-access"
    assert turn["sandboxPolicy"] == {"type": "dangerFullAccess"}


def test_auto_edit_accepts_native_file_change_but_keeps_shell_confirmation():
    adapter = object.__new__(CodexAgentAdapter)
    adapter._active_mode = AgentApprovalMode.AUTO_EDIT
    adapter._interaction_callback = None
    adapter._interaction_waiters = {}
    assert adapter._handle_server_request("item/fileChange/requestApproval", {"itemId": "file"}) == {"decision": "accept"}
    assert adapter._handle_server_request("item/commandExecution/requestApproval", {"command": "Set-Content -LiteralPath file.txt -Value test"}) == {"decision": "accept"}
    assert adapter._handle_server_request("item/commandExecution/requestApproval", {"command": "Remove-Item file.txt"}) == {"decision": "decline"}


def test_voicepet_mcp_overrides_keep_tools_visible_and_gated(tmp_path):
    mode_file = tmp_path / APPROVAL_MODE_FILENAME
    overrides = voicepet_mcp_overrides(mode_file)

    # required 缺失时 Codex 不会再等待服务就绪，voicepet 工具会静默消失
    assert "mcp_servers.voicepet.required=true" in overrides
    assert 'mcp_servers.voicepet.default_tools_approval_mode="approve"' in overrides
    assert "mcp_servers.voicepet.env.VOICEPET_MODE_FILE=" + json.dumps(str(mode_file)) in overrides
    assert any(override.startswith("mcp_servers.voicepet.args=") for override in overrides)


@pytest.mark.parametrize("mode", list(AgentApprovalMode))
def test_write_approval_mode_publishes_current_mode(tmp_path, mode):
    mode_file = tmp_path / APPROVAL_MODE_FILENAME

    assert write_approval_mode(mode_file, mode) is True
    assert json.loads(mode_file.read_text(encoding="utf-8")) == {"version": 1, "mode": mode.value}


def test_write_approval_mode_ignores_invalid_input_and_unwritable_path(tmp_path):
    assert write_approval_mode(tmp_path / "mode.json", "full_auto") is False
    assert write_approval_mode(tmp_path / "missing" / "mode.json", AgentApprovalMode.FULL_AUTO) is False


class FakeTurnClient:
    """只回应一轮 turn，用于观察适配器真正发出的参数"""

    def __init__(self):
        self.turn_params = None

    async def thread_start(self, params):
        return SimpleNamespace(thread=SimpleNamespace(id="thread"))

    async def turn_start(self, thread_id, inputs, params):
        self.turn_params = params
        return SimpleNamespace(turn=SimpleNamespace(id="turn"))

    async def next_turn_notification(self, turn_id):
        return SimpleNamespace(
            method="turn/completed",
            payload=SimpleNamespace(turn=SimpleNamespace(status="completed")),
        )


async def collect_turn(adapter, request):
    return [event async for event in adapter.run_turn(request)]


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_adapter_sends_the_configured_reasoning_effort(tmp_path, effort):
    client = FakeTurnClient()
    adapter = object.__new__(CodexAgentAdapter)
    adapter._data_directory = tmp_path
    adapter._system_prompt = ""
    adapter._model = "gpt-test"
    adapter._reasoning_effort = effort
    adapter._client = client
    adapter._turns = {}
    adapter._cancelled = set()
    adapter._active_mode = AgentApprovalMode.SUGGEST
    adapter._active_request = None
    adapter._interaction_callback = None
    adapter._interaction_waiters = {}
    request = AgentTurnRequest(
        session_id="session",
        thread_id=None,
        turn_id="turn",
        input="你好",
        approval_mode=AgentApprovalMode.SUGGEST,
    )

    events = asyncio.run(collect_turn(adapter, request))

    assert events[-1].type is AgentEventType.TURN_COMPLETED
    assert client.turn_params["summary"] == "concise"
    assert client.turn_params["effort"] == effort


def test_fake_server_waits_for_native_file_approval(tmp_path):
    marker = tmp_path / "completed.json"
    server = tmp_path / "server.py"
    server.write_text('''import json, sys
def send(data):
    print(json.dumps(data), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        send({"id": request["id"], "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        send({"id": request["id"], "result": {
            "approvalPolicy": "never", "approvalsReviewer": "user", "cwd": "C:/", "model": "fake", "modelProvider": "openai",
            "sandbox": {"type": "readOnly", "access": {"type": "fullAccess"}},
            "thread": {"id": "thread", "sessionId": "session", "cliVersion": "0.147.0", "createdAt": 1, "updatedAt": 1, "cwd": "C:/", "ephemeral": True, "modelProvider": "openai", "preview": "", "source": "appServer", "status": {"type": "idle"}, "turns": []}}})
    elif method == "turn/start":
        send({"id": request["id"], "result": {"turn": {"id": "turn", "items": [], "status": "inProgress"}}})
        send({"id": "approval", "method": "item/fileChange/requestApproval", "params": {"threadId": "thread", "turnId": "turn", "itemId": "item", "reason": "创建文件"}})
    elif request.get("id") == "approval":
        with open(sys.argv[1], "w") as output:
            json.dump(request["result"], output)
        send({"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "items": [], "status": "completed"}}})
''', encoding="utf-8")
    entered, release = Event(), Event()

    def handler(method, params):
        assert method == "item/fileChange/requestApproval"
        assert params["itemId"] == "item"
        entered.set()
        assert release.wait(5)
        return {"decision": "decline"}

    client = CodexClient(CodexConfig(launch_args_override=(sys.executable, str(server), str(marker))), approval_handler=handler)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            def run():
                client.start()
                client.initialize()
                thread = client.thread_start(codex_thread_options(AgentApprovalMode.SUGGEST))
                turn = client.turn_start(thread.thread.id, "测试", codex_turn_options(AgentApprovalMode.SUGGEST))
                return client.next_turn_notification(turn.turn.id)
            result = pool.submit(run)
            assert entered.wait(5)
            assert not marker.exists()
            release.set()
            assert result.result(timeout=5).method == "turn/completed"
            assert json.loads(marker.read_text()) == {"decision": "decline"}
        finally:
            release.set()
            client.close()
