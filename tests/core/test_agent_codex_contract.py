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
    approval_message,
    codex_thread_options,
    codex_turn_options,
    file_change_details,
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
    adapter._active_modes = {"native": AgentApprovalMode.AUTO_EDIT}
    adapter._file_changes = {}
    adapter._turn_requests = {}
    adapter._turns = {}
    adapter._interaction_callback = None
    adapter._interaction_waiters = {}
    assert adapter._handle_server_request("item/fileChange/requestApproval", {"itemId": "file"}) == {"decision": "accept"}
    assert adapter._handle_server_request("item/commandExecution/requestApproval", {"command": "Set-Content -LiteralPath file.txt -Value test"}) == {"decision": "accept"}
    assert adapter._handle_server_request("item/commandExecution/requestApproval", {"command": "Remove-Item file.txt"}) == {"decision": "decline"}


def test_approval_detail_shows_the_command_and_working_directory():
    detail = approval_message(
        "item/commandExecution/requestApproval",
        {"command": "npm run build", "cwd": "D:\\LD\\VoicePet", "reason": "需要构建产物"},
    )

    assert "npm run build" in detail
    assert "工作目录：D:\\LD\\VoicePet" in detail
    assert "原因：需要构建产物" in detail


def test_approval_detail_lists_pending_files_and_says_when_unknown():
    listed = approval_message(
        "item/fileChange/requestApproval",
        {"reason": "需要写入权限"},
        ({"path": "D:\\LD\\VoicePet\\README.md", "kind": "update", "summary": "（+12 −3）"},),
    )

    assert "修改 D:\\LD\\VoicePet\\README.md（+12 −3）" in listed
    assert "原因：需要写入权限" in listed
    assert "没有给出文件清单" in approval_message("item/fileChange/requestApproval", {})


def test_file_change_details_summarize_the_pending_patch():
    item = SimpleNamespace(
        changes=(
            SimpleNamespace(
                path="D:\\LD\\VoicePet\\core\\agent_codex.py",
                kind=SimpleNamespace(root=SimpleNamespace(type="add")),
                diff="@@ -1 +1,3 @@\n+第一行\n+第二行\n-旧行\n 上下文\n",
            ),
            SimpleNamespace(path="", kind=None, diff=""),
        )
    )

    assert file_change_details(item) == (
        {
            "path": "D:\\LD\\VoicePet\\core\\agent_codex.py",
            "kind": "add",
            "summary": "（+2 −1）",
        },
    )


def test_approval_request_publishes_the_concrete_operation():
    adapter = object.__new__(CodexAgentAdapter)
    adapter._active_modes = {"native": AgentApprovalMode.SUGGEST}
    adapter._file_changes = {}
    adapter._turn_requests = {"turn-1": SimpleNamespace(session_id="s1", turn_id="turn-1")}
    adapter._turns = {}
    adapter._interaction_waiters = {}
    published = []

    def publish(payload):
        published.append(payload)
        adapter.resolve_interaction(str(payload["request_id"]), "accept")
        return True

    adapter.set_interaction_callback(publish)
    decision = adapter._handle_server_request(
        "item/commandExecution/requestApproval",
        {"itemId": "item-1", "command": "npm run build", "cwd": "D:\\LD\\VoicePet"},
    )

    assert decision == {"decision": "accept"}
    assert [payload["message"] for payload in published] == [
        "Codex 想执行命令：\nnpm run build\n工作目录：D:\\LD\\VoicePet"
    ]


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


def test_codex_items_turn_into_chat_visible_activity_events():
    from openai_codex.generated.v2_all import (
        ItemCompletedNotification,
        ItemStartedNotification,
    )

    from core.agent_codex import (
        MAX_OUTPUT_DELTA_CHARS,
        bounded_text,
        codex_item_events,
        turn_status,
    )

    started = ItemStartedNotification.model_validate(
        {
            "threadId": "thread",
            "turnId": "turn",
            "startedAtMs": 1,
            "item": {
                "type": "commandExecution",
                "id": "item-1",
                "command": "go vet ./...",
                "cwd": "D:/LD",
                "commandActions": [],
                "status": "inProgress",
            },
        }
    )
    assert codex_item_events(started.item, started=True) == [
        (AgentEventType.COMMAND_STARTED, {"message": "正在执行命令", "command": "go vet ./..."})
    ]

    failed = ItemCompletedNotification.model_validate(
        {
            "threadId": "thread",
            "turnId": "turn",
            "completedAtMs": 2,
            "item": {
                "type": "commandExecution",
                "id": "item-1",
                "command": "go vet ./...",
                "cwd": "D:/LD",
                "commandActions": [],
                "status": "failed",
                "exitCode": 2,
                "aggregatedOutput": "boom",
            },
        }
    )
    assert codex_item_events(failed.item, started=False) == [
        (
            AgentEventType.COMMAND_COMPLETED,
            {"message": "命令执行失败", "status": "failed", "command": "go vet ./..."},
        )
    ]

    tool = ItemCompletedNotification.model_validate(
        {
            "threadId": "thread",
            "turnId": "turn",
            "completedAtMs": 3,
            "item": {
                "type": "mcpToolCall",
                "id": "item-2",
                "server": "voicepet",
                "tool": "list_windows",
                "arguments": {},
                "status": "completed",
            },
        }
    )
    assert codex_item_events(tool.item, started=False) == [
        (
            AgentEventType.TOOL_COMPLETED,
            {"tool": "voicepet/list_windows", "status": "completed", "message": "voicepet/list_windows 调用完成"},
        )
    ]

    changed = ItemCompletedNotification.model_validate(
        {
            "threadId": "thread",
            "turnId": "turn",
            "completedAtMs": 4,
            "item": {
                "type": "fileChange",
                "id": "item-3",
                "status": "completed",
                "changes": [{"path": "a.go", "kind": {"type": "update"}, "diff": "@@"}],
            },
        }
    )
    assert codex_item_events(changed.item, started=False) == [
        (
            AgentEventType.FILE_CHANGE,
            {"status": "completed", "paths": ("a.go",), "message": "文件已修改"},
        )
    ]

    # 中断在 VoicePet 里按取消处理，命令输出按帧上限截断后才进入主进程
    assert turn_status("interrupted") == "cancelled"
    assert turn_status("completed") == "completed"
    assert len(bounded_text("x" * (MAX_OUTPUT_DELTA_CHARS + 1), MAX_OUTPUT_DELTA_CHARS)) == MAX_OUTPUT_DELTA_CHARS


def test_reasoning_items_end_with_an_explicit_completion():
    from openai_codex.generated.v2_all import (
        ItemCompletedNotification,
        ItemStartedNotification,
    )

    from core.agent_codex import codex_item_events

    payload = {
        "threadId": "thread",
        "turnId": "turn",
        "item": {"type": "reasoning", "id": "item-9", "content": [], "summary": []},
    }
    started = ItemStartedNotification.model_validate({**payload, "startedAtMs": 1})
    completed = ItemCompletedNotification.model_validate({**payload, "completedAtMs": 2})

    assert codex_item_events(started.item, started=True) == []
    assert codex_item_events(completed.item, started=False) == [
        (AgentEventType.REASONING_COMPLETED, {})
    ]


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


def build_adapter(client, tmp_path, *, effort="low"):
    """只装配通知循环需要的字段，避免真的启动 Codex 子进程"""

    adapter = object.__new__(CodexAgentAdapter)
    adapter._data_directory = tmp_path
    adapter._system_prompt = ""
    adapter._model = "gpt-test"
    adapter._reasoning_effort = effort
    adapter._client = client
    adapter._turns = {}
    adapter._cancelled = set()
    adapter._active_modes = {}
    adapter._turn_requests = {}
    adapter._interaction_callback = None
    adapter._interaction_waiters = {}
    return adapter


class FakeNotificationClient(FakeTurnClient):
    """按顺序回放通知，用于观察适配器把哪些通知转成了事件"""

    def __init__(self, notifications):
        super().__init__()
        self._notifications = list(notifications)

    async def next_turn_notification(self, turn_id):
        if self._notifications:
            return self._notifications.pop(0)
        return await super().next_turn_notification(turn_id)


def notification(method, payload):
    return SimpleNamespace(method=method, payload=payload)


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_adapter_sends_the_configured_reasoning_effort(tmp_path, effort):
    client = FakeTurnClient()
    adapter = build_adapter(client, tmp_path, effort=effort)
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


def test_adapter_sends_reasoning_and_commentary_as_process_text(tmp_path):
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        ItemCompletedNotification,
        ItemStartedNotification,
        ReasoningSummaryTextDeltaNotification,
        ReasoningTextDeltaNotification,
    )

    def message_started(item_id, phase):
        return ItemStartedNotification.model_validate(
            {
                "threadId": "thread",
                "turnId": "turn",
                "startedAtMs": 1,
                "item": {"type": "agentMessage", "id": item_id, "text": "", "phase": phase},
            }
        )

    client = FakeNotificationClient(
        [
            notification(
                "item/reasoning/summaryTextDelta",
                ReasoningSummaryTextDeltaNotification.model_validate(
                    {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "r1",
                        "summaryIndex": 0,
                        "delta": "先看目录",
                    }
                ),
            ),
            notification(
                "item/reasoning/textDelta",
                ReasoningTextDeltaNotification.model_validate(
                    {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "r1",
                        "contentIndex": 0,
                        "delta": "有摘要时原始思考不重复展示",
                    }
                ),
            ),
            notification("item/started", message_started("m1", "commentary")),
            notification(
                "item/agentMessage/delta",
                AgentMessageDeltaNotification.model_validate(
                    {"threadId": "thread", "turnId": "turn", "itemId": "m1", "delta": "顺手说明"}
                ),
            ),
            notification("item/started", message_started("m2", "final_answer")),
            notification(
                "item/agentMessage/delta",
                AgentMessageDeltaNotification.model_validate(
                    {"threadId": "thread", "turnId": "turn", "itemId": "m2", "delta": "最终答复"}
                ),
            ),
            notification(
                "item/completed",
                ItemCompletedNotification.model_validate(
                    {
                        "threadId": "thread",
                        "turnId": "turn",
                        "completedAtMs": 9,
                        "item": {"type": "reasoning", "id": "r1", "content": [], "summary": []},
                    }
                ),
            ),
        ]
    )
    adapter = build_adapter(client, tmp_path)
    request = AgentTurnRequest(
        session_id="session",
        thread_id=None,
        turn_id="turn",
        input="你好",
        approval_mode=AgentApprovalMode.SUGGEST,
    )

    events = asyncio.run(collect_turn(adapter, request))

    assert [
        (event.type, event.payload.get("text"))
        for event in events
        if event.type is not AgentEventType.TURN_COMPLETED
    ] == [
        (AgentEventType.REASONING_DELTA, "先看目录"),
        (AgentEventType.REASONING_DELTA, "顺手说明"),
        (AgentEventType.TEXT_DELTA, "最终答复"),
        (AgentEventType.REASONING_COMPLETED, None),
    ]


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
