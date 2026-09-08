# VoicePet Codex Agent Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the primary chat execution path with a resumable OpenAI Codex computer Agent while preserving VoicePet voice, sessions, memory, cancellation, audit summaries, and three user-selectable approval modes.

**Architecture:** Add a dedicated Agent Worker process and a bounded bidirectional RPC protocol. The Worker owns the pinned Codex runtime and translates app-server threads, turns, approval requests, and stream notifications into stable VoicePet events; the main process owns credentials, session metadata, QML state, and approval UI. Keep the current Responses/Chat Completions path as an explicitly selected pure-chat fallback.

For Suggest and Auto Edit, Codex receives full-computer read access in a read-only sandbox while VoicePet dynamic tools are the only command and mutation boundary. Suggest prompts for both dynamic tools; Auto Edit automatically accepts text-file create/edit and prompts for commands, replacement, and deletion. Full Auto uses `dangerFullAccess`; its dynamic tools execute immediately and native file changes remain allowed.

**Tech Stack:** Python 3.12, `openai-codex==0.147.0`, Codex app-server JSON-RPC through the Python SDK, asyncio, SQLite, PySide6/QML, Windows Job Objects, PyInstaller, pytest, ruff.

---

## File map

New core files:

- `core/agent_types.py`: approval modes, turn requests, events, terminal states, and strict validation.
- `core/agent_rpc.py`: bounded JSONL request/response/notification codec for the Agent Worker.
- `core/agent_store.py`: SQLite bindings for VoicePet sessions, Codex threads, turns, and event summaries.
- `core/agent_codex.py`: pinned SDK compatibility adapter, API-key login, approval bridge, and Codex event translation.
- `core/agent_actions.py`: dynamic full-computer file and command operations used as the approval boundary.
- `core/agent_actions.py`: dynamic full-computer file and command operations used as the approval boundary.
- `core/agent_worker.py`: Worker request loop and active-turn lifecycle.
- `core/agent_client.py`: main-process RPC client, event routing, and approval resolution.
- `core/agent_process.py`: Worker subprocess, restart limits, and Windows process-tree ownership.
- `core/agent_gateway.py`: Coordinator-facing Agent interface and session/memory/input assembly.
- `core/windows_job.py`: minimal Windows Job Object wrapper with kill-on-close behavior.

Modified core files:

- `core/config.py`: Agent backend, default approval mode, timeout, and version-3 migration.
- `core/memory_schema.py`: versioned Agent tables.
- `core/events.py`: UI-safe Agent progress and approval events.
- `core/coordinator.py`: route normal input through the Agent gateway and isolate late events.
- `core/runtime.py`: runtime APIs for approval mode and approval decisions.
- `core/runtime_factory.py`: construct Agent store/process/gateway from DPAPI-loaded API Key.
- `core/session_data.py`: per-session mode override and binding cleanup.
- `app.py`: `--agent-worker` entry mode.
- `pyproject.toml`, `VoicePet.spec`, `packaging/build_release.ps1`: pinned dependency and frozen runtime packaging.

Modified UI files:

- `ui/viewmodels/chat.py`: effective mode, per-session override, progress items, and approval decisions.
- `ui/viewmodels/settings.py`: saved default Agent mode.
- `ui/qml/components/ChatComposer.qml`: three-mode toolbar selector.
- `ui/qml/screens/ChatPage.qml`: bind selector and show next-turn semantics.
- `ui/qml/screens/settings/AiSettings.qml`: default mode setting and Codex backend status.
- `ui/qml/windows/ToolConfirmationWindow.qml`: render command/file Agent approvals.

New tests follow the matching production filenames under `tests/core/` and `tests/ui/qml/`. Existing tests are modified only where a public constructor or config schema changes.

## Task 1: Lock and prove the Codex SDK contract

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/core/test_agent_codex_contract.py`
- Create: `core/agent_codex.py`

- [ ] **Step 1: Add the pinned optional dependency**

Add a dedicated extra so existing lightweight test installs do not require the runtime:

```toml
agent = [
    "openai-codex==0.147.0",
]
```

Install the locked extra before importing it in contract tests:

```powershell
python -m pip install -e ".[agent]"
```

Expected: pip installs `openai-codex 0.147.0` and its pinned `openai-codex-cli-bin` dependency.

- [ ] **Step 2: Write contract tests for the SDK surface used by VoicePet**

```python
from openai_codex import __version__
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import AskForApprovalValue

from core.agent_codex import CODEX_SDK_VERSION, codex_thread_options, codex_turn_options
from core.agent_types import AgentApprovalMode


def test_pinned_codex_sdk_contract_is_available():
    assert __version__ == CODEX_SDK_VERSION == "0.147.0"
    assert callable(CodexClient)
    assert CodexConfig().experimental_api is True


def test_full_auto_maps_to_full_access_without_approval():
    thread = codex_thread_options(AgentApprovalMode.FULL_AUTO)
    turn = codex_turn_options(AgentApprovalMode.FULL_AUTO)
    assert thread["sandbox"] == "dangerFullAccess"
    assert turn["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert turn["approvalPolicy"] == AskForApprovalValue.never.value


def test_interactive_modes_use_controlled_dynamic_tools():
    for mode in (AgentApprovalMode.SUGGEST, AgentApprovalMode.AUTO_EDIT):
        thread = codex_thread_options(mode)
        turn = codex_turn_options(mode)
        assert thread["sandbox"] == "readOnly"
        assert turn["sandboxPolicy"] == {
            "type": "readOnly",
            "access": {"type": "fullAccess"},
        }
        assert turn["approvalPolicy"] == AskForApprovalValue.never.value
        assert "dynamicTools" not in turn
        assert {tool["name"] for tool in thread["dynamicTools"]} == {
            "voicepet_file_change",
            "voicepet_shell",
        }
```

- [ ] **Step 3: Run the contract tests and verify the expected first failure**

Run: `python -m pytest tests/core/test_agent_codex_contract.py -q`

Expected: collection fails because `core.agent_codex` and `core.agent_types` do not exist.

- [ ] **Step 4: Add the minimal SDK compatibility seam**

Start `core/agent_codex.py` with a version check and a single mapping function:

```python
from __future__ import annotations

from typing import Any

from openai_codex import __version__

from .agent_types import AgentApprovalMode

CODEX_SDK_VERSION = "0.147.0"


class AgentCodexCompatibilityError(RuntimeError):
    code = "agent.codex_compatibility"


def require_supported_sdk() -> None:
    if __version__ != CODEX_SDK_VERSION:
        raise AgentCodexCompatibilityError(
            f"需要 openai-codex {CODEX_SDK_VERSION}，当前为 {__version__}"
        )


def dynamic_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "voicepet_file_change",
            "description": "Create, edit, replace, or delete one text file on this computer",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["create", "edit", "replace", "delete"]},
                    "path": {"type": "string", "maxLength": 32767},
                    "expected_sha256": {"type": ["string", "null"]},
                    "content": {"type": "string", "maxLength": 524288},
                    "edits": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string", "maxLength": 131072},
                                "new_text": {"type": "string", "maxLength": 131072},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["old_text", "new_text", "replace_all"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["operation", "path", "expected_sha256", "content", "edits"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "voicepet_shell",
            "description": "Run one executable with an argument vector and working directory",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "program": {"type": "string", "maxLength": 32767},
                    "args": {"type": "array", "maxItems": 128, "items": {"type": "string", "maxLength": 8192}},
                    "cwd": {"type": "string", "maxLength": 32767},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 1800},
                },
                "required": ["program", "args", "cwd", "timeout"],
                "additionalProperties": False,
            },
        },
    ]


def codex_thread_options(mode: AgentApprovalMode) -> dict[str, Any]:
    return {
        "sandbox": "dangerFullAccess" if mode is AgentApprovalMode.FULL_AUTO else "readOnly",
        "approvalPolicy": "never",
        "dynamicTools": dynamic_tool_specs(),
    }


def codex_turn_options(mode: AgentApprovalMode) -> dict[str, Any]:
    sandbox_policy = (
        {"type": "dangerFullAccess"}
        if mode is AgentApprovalMode.FULL_AUTO
        else {"type": "readOnly", "access": {"type": "fullAccess"}}
    )
    return {"sandboxPolicy": sandbox_policy, "approvalPolicy": "never"}
```

Create `core/agent_types.py` with the enum below; Task 2 adds the remaining domain types without changing these values:

```python
from enum import Enum


class AgentApprovalMode(str, Enum):
    SUGGEST = "suggest"
    AUTO_EDIT = "auto_edit"
    FULL_AUTO = "full_auto"
```

- [ ] **Step 5: Run a local fake app-server contract test**

Add a fake executable fixture that emits `initialize`, `thread/start`, `turn/start`, an `item/tool/call` request for `voicepet_file_change`, and `turn/completed`. Instantiate `CodexClient(CodexConfig(launch_args_override=(sys.executable, str(fake_server))), approval_handler=handler)` and assert the handler returns the dynamic-tool result before the fake server records completion. No OpenAI API call and no real file mutation is allowed in this test.

Run: `python -m pytest tests/core/test_agent_codex_contract.py -q`

Expected: all contract tests pass. If the pinned SDK cannot route a blocking dynamic-tool request, stop implementation and mark Suggest and Auto Edit unavailable rather than silently accepting operations.

- [ ] **Step 6: Add and run the real runtime approval probe**

Create `tests/integration/test_codex_approval_probe.py`, gated by `VOICEPET_CODEX_APPROVAL_PROBE=1`. Read the existing key from `DpapiCredentialStore(default_config_path().parent / "data" / "credentials.bin")`, create a unique directory under pytest's `tmp_path`, and ask the pinned runtime to propose one file add, one file update, one delete, and one harmless command in each interactive mode. The dynamic-tool handler declines every request and records `(tool, itemId)`; assert no proposed file exists and the command marker was not created. Then ask the Agent to bypass the dynamic tools with a built-in shell or patch operation and assert the read-only sandbox plus disabled built-in shell prevents every effect. Never print the key or include it in an assertion message.

Run: `$env:VOICEPET_CODEX_APPROVAL_PROBE='1'; python -m pytest tests/integration/test_codex_approval_probe.py -q; Remove-Item Env:VOICEPET_CODEX_APPROVAL_PROBE`

Expected: both interactive modes emit dynamic-tool requests before every tested operation, all declined effects remain absent, and direct bypass attempts fail. If this fails, stop before Task 5 because the selected runtime cannot enforce the approved modes.

- [ ] **Step 7: Commit the dependency contract**

```powershell
git add pyproject.toml core/agent_types.py core/agent_codex.py tests/core/test_agent_codex_contract.py tests/integration/test_codex_approval_probe.py
git commit -m "build: lock Codex agent runtime contract"
```

## Task 2: Define stable Agent domain types and mode policy

**Files:**
- Modify: `core/agent_types.py`
- Create: `tests/core/test_agent_types.py`

- [ ] **Step 1: Write strict mode and event validation tests**

```python
import pytest

from core.agent_types import (
    AgentApprovalMode,
    AgentEvent,
    AgentEventType,
    AgentTurnRequest,
    approval_action,
)


def test_mode_policy_matches_product_semantics():
    assert approval_action(AgentApprovalMode.SUGGEST, "file_patch") == "prompt"
    assert approval_action(AgentApprovalMode.AUTO_EDIT, "file_patch") == "accept"
    assert approval_action(AgentApprovalMode.AUTO_EDIT, "file_delete") == "prompt"
    assert approval_action(AgentApprovalMode.AUTO_EDIT, "command") == "prompt"
    assert approval_action(AgentApprovalMode.FULL_AUTO, "command") == "accept"


def test_turn_request_freezes_mode_and_bounds_input():
    request = AgentTurnRequest("s", None, "t", "你好", AgentApprovalMode.AUTO_EDIT)
    assert request.approval_mode is AgentApprovalMode.AUTO_EDIT
    with pytest.raises(ValueError, match="输入过长"):
        AgentTurnRequest("s", None, "t", "x" * 200_001, AgentApprovalMode.SUGGEST)


def test_event_requires_non_negative_monotonic_sequence_value():
    with pytest.raises(ValueError, match="事件序号"):
        AgentEvent("s", "t", -1, AgentEventType.TEXT_DELTA, {"text": "x"})
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python -m pytest tests/core/test_agent_types.py -q`

Expected: import or missing-name failures.

- [ ] **Step 3: Implement the stable types**

Use string enums `suggest`, `auto_edit`, `full_auto`; event types `text_delta`, `command_started`, `command_output_delta`, `command_completed`, `file_change`, `tool_started`, `tool_completed`, `plan_updated`, `usage_updated`, `approval_request`, `turn_completed`, `cancelled`, and `error`. Implement `approval_action()` exactly as this table:

```python
_MODE_ACTIONS = {
    AgentApprovalMode.SUGGEST: {
        "file_patch": "prompt", "file_create": "prompt",
        "file_delete": "prompt", "file_replace": "prompt", "command": "prompt",
    },
    AgentApprovalMode.AUTO_EDIT: {
        "file_patch": "accept", "file_create": "accept",
        "file_delete": "prompt", "file_replace": "prompt", "command": "prompt",
    },
    AgentApprovalMode.FULL_AUTO: {
        "file_patch": "accept", "file_create": "accept",
        "file_delete": "accept", "file_replace": "accept", "command": "accept",
    },
}


def approval_action(mode: AgentApprovalMode, operation: str) -> str:
    try:
        return _MODE_ACTIONS[mode][operation]
    except KeyError as error:
        raise ValueError("Agent 操作类别无效") from error
```

Use frozen slot dataclasses for `AgentCapabilities`, `AgentTurnRequest`, `AgentEvent`, `AgentApprovalRequest`, and `AgentTerminalResult`. `AgentCapabilities.from_mapping()` requires protocol version 1, non-empty SDK/runtime version strings, and booleans for approval and full-access support. `AgentTurnRequest.to_mapping()` emits only `session_id`, `thread_id`, `turn_id`, `input`, `approval_mode`, and bounded attachment metadata. Copy event payload mappings into `MappingProxyType` and enforce bounded identifiers, input length, sequence, and payload size before RPC encoding.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/core/test_agent_types.py tests/core/test_agent_codex_contract.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add core/agent_types.py tests/core/test_agent_types.py tests/core/test_agent_codex_contract.py
git commit -m "feat: define Codex agent modes and events"
```

## Task 3: Add versioned configuration and per-session mode persistence

**Files:**
- Modify: `core/config.py`
- Modify: `core/memory_schema.py`
- Create: `core/agent_store.py`
- Modify: `core/session_data.py`
- Modify: `tests/core/test_config.py`
- Modify: `tests/core/test_memory_schema.py`
- Create: `tests/core/test_agent_store.py`

- [ ] **Step 1: Write config migration tests**

Add assertions that config version 2 migrates to version 3 with:

```python
assert migrated.agent.enabled is True
assert migrated.agent.default_approval_mode == "auto_edit"
assert migrated.agent.max_turn_minutes == 30
```

Also assert invalid mode names and timeouts outside `1..240` raise `ConfigError`.

- [ ] **Step 2: Write store lifecycle tests**

```python
def test_agent_store_binds_thread_and_snapshots_mode(tmp_path):
    store = AgentStore(tmp_path / "assistant.db")
    store.bind_thread("session-1", "thread-1", "runtime-1")
    store.set_session_mode("session-1", AgentApprovalMode.SUGGEST)
    store.start_turn("turn-1", "session-1", AgentApprovalMode.SUGGEST)
    assert store.binding("session-1").codex_thread_id == "thread-1"
    assert store.session_mode("session-1") is AgentApprovalMode.SUGGEST
    assert store.turn("turn-1").approval_mode is AgentApprovalMode.SUGGEST


def test_agent_event_sequence_is_idempotent(tmp_path):
    store = AgentStore(tmp_path / "assistant.db")
    event = AgentEvent("session-1", "turn-1", 1, AgentEventType.TEXT_DELTA, {"text": "a"})
    assert store.append_event_summary(event, "text delta") is True
    assert store.append_event_summary(event, "duplicate") is False
```

- [ ] **Step 3: Run tests and verify failures**

Run: `python -m pytest tests/core/test_config.py tests/core/test_memory_schema.py tests/core/test_agent_store.py -q`

Expected: failures for config version, missing `AgentConfig`, missing schema tables, and missing `AgentStore`.

- [ ] **Step 4: Implement config version 3**

Add:

```python
CURRENT_CONFIG_VERSION = 3


@dataclass(frozen=True, slots=True)
class AgentConfig:
    enabled: bool = True
    default_approval_mode: str = "auto_edit"
    max_turn_minutes: int = 30

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ConfigError("Agent 开关类型无效")
        if self.default_approval_mode not in {"suggest", "auto_edit", "full_auto"}:
            raise ConfigError("Agent 审批模式无效")
        if type(self.max_turn_minutes) is not int or not 1 <= self.max_turn_minutes <= 240:
            raise ConfigError("Agent 单轮时限必须在一到二百四十分钟之间")
```

Add `agent` to `AppConfig`, `_FIELDS`, serialization, and `_section`. Migrate versions 1 and 2 sequentially; version 2 receives `AgentConfig()` without changing existing LLM settings.

- [ ] **Step 5: Add Agent schema tables**

Increment `MEMORY_SCHEMA_VERSION` and add DDL for:

```sql
CREATE TABLE agent_session_bindings(
    session_id TEXT PRIMARY KEY,
    codex_thread_id TEXT NOT NULL,
    runtime_id TEXT NOT NULL,
    approval_mode_override TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE agent_turns(
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    codex_turn_id TEXT,
    approval_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    safe_summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE agent_events(
    turn_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    safe_summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(turn_id, seq)
);
```

Implement `AgentStore` with one connection per operation, `BEGIN IMMEDIATE` for writes, enum validation on reads, idempotent `(turn_id, seq)` inserts, and terminal-state compare-and-set so a turn completes once.

- [ ] **Step 6: Wire session deletion**

Give `SessionDataManager` an optional `agent_store`. On successful session deletion, call `agent_store.delete_session(session_id)` after the active turn has been stopped by the caller. Keep existing behavior unchanged when no store is provided.

- [ ] **Step 7: Run focused tests**

Run: `python -m pytest tests/core/test_config.py tests/core/test_memory_schema.py tests/core/test_agent_store.py tests/core/test_session_data.py -q`

Expected: pass.

- [ ] **Step 8: Commit**

```powershell
git add core/config.py core/memory_schema.py core/agent_store.py core/session_data.py tests/core/test_config.py tests/core/test_memory_schema.py tests/core/test_agent_store.py tests/core/test_session_data.py
git commit -m "feat: persist Codex agent configuration and sessions"
```

## Task 4: Implement the bounded bidirectional Agent RPC

**Files:**
- Create: `core/agent_rpc.py`
- Create: `tests/core/test_agent_rpc.py`

- [ ] **Step 1: Write codec tests**

Test exact fields for `initialize`, `turn.start`, `turn.cancel`, `approval.resolve`, `shutdown`, responses, and `agent.event` notifications. Include duplicate JSON keys, non-finite numbers, unknown methods, oversized lines, invalid IDs, and notifications that incorrectly contain an ID.

```python
def test_event_notification_round_trip():
    message = AgentRpcNotification(
        "agent.event",
        {"session_id": "s", "turn_id": "t", "seq": 1,
         "type": "text_delta", "payload": {"text": "你"}},
    )
    assert decode_message(encode_message(message)) == message
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/core/test_agent_rpc.py -q`

Expected: import failure for `core.agent_rpc`.

- [ ] **Step 3: Implement the codec**

Use `DEFAULT_MAX_LINE_BYTES = 1024 * 1024`, canonical compact JSON, duplicate-key rejection, `_freeze_json`, and three dataclasses: `AgentRpcRequest`, `AgentRpcResponse`, `AgentRpcNotification`. Validate method-specific params with exact key sets. Use names `agent.initialize`, `agent.turn.start`, `agent.turn.cancel`, `agent.approval.resolve`, `agent.shutdown`, and `agent.event`.

Do not modify `core/tool_rpc.py`; its strict three-method contract remains intact.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/core/test_agent_rpc.py tests/core/test_tool_rpc.py -q`

Expected: both protocols pass independently.

- [ ] **Step 5: Commit**

```powershell
git add core/agent_rpc.py tests/core/test_agent_rpc.py
git commit -m "feat: add bounded Agent worker protocol"
```

## Task 5: Implement Codex authentication, turns, approvals, and event translation

**Files:**
- Modify: `core/agent_codex.py`
- Create: `core/agent_actions.py`
- Create: `tests/core/test_agent_codex.py`
- Create: `tests/core/test_agent_actions.py`

- [ ] **Step 1: Write adapter tests with a fake SDK client**

Cover API-key login once, thread create/resume, turn option snapshots, text deltas, command output, file changes, usage, completion, interrupt, and error mapping. Verify the key is absent from exception strings and event payloads.

```python
async def test_auto_edit_accepts_patch_but_prompts_for_command():
    approvals = ApprovalBridge()
    executor = FakeActionExecutor()
    adapter = CodexAgentAdapter(FakeCodexClient(), executor, approvals)
    edit = {
        "itemId": "item-file",
        "tool": "voicepet_file_change",
        "arguments": {"operation": "edit", "path": "C:/x.txt"},
    }
    await adapter.review_dynamic_tool(edit, AgentApprovalMode.AUTO_EDIT)
    assert executor.calls == [edit["arguments"]]
    command = {
        "itemId": "item-cmd",
        "tool": "voicepet_shell",
        "arguments": {"program": "pytest", "args": [], "cwd": "C:/", "timeout": 30},
    }
    pending = await adapter.review_dynamic_tool(command, AgentApprovalMode.AUTO_EDIT)
    assert pending.waiting_for_user is True
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/core/test_agent_codex.py -q`

Expected: missing adapter classes.

- [ ] **Step 3: Implement a public VoicePet adapter around the pinned low-level SDK client**

Construct `openai_codex.client.CodexClient` with `CodexConfig(cwd=codex_data_dir, env=sanitized_env, config_overrides=('cli_auth_credentials_store="keyring"', 'features.shell_tool=false', 'features.unified_exec=false'), client_name="voicepet", client_title="VoicePet", client_version=app_version)` and a custom `approval_handler`. Set the child process `CODEX_HOME` to the VoicePet-owned Codex data directory, call `start()`, `initialize()`, and `account_login_start({"type": "apiKey", "apiKey": key})` on the Worker side. Build `sanitized_env` from an allowlist and remove `OPENAI_API_KEY` before launch so Agent-created commands cannot inherit the secret. Never put the key in argv, events, errors, or plaintext runtime config.

After login, verify the runtime directory does not contain the key or a file-based `auth.json`; fail Agent initialization with `agent.credential_store` if the OS keyring is unavailable. Add a test double for this verification and a Windows integration test using a sentinel key without making an API request.

Run blocking SDK calls with `asyncio.to_thread`. The SDK reader thread calls the approval handler synchronously; bridge it to the Worker event loop with `asyncio.run_coroutine_threadsafe()` and wait on that future. The handler returns only documented decisions.

Pass `codex_thread_options(mode)` as a raw JSON mapping when creating a thread. Dynamic tools are registered only on `thread/start`, because the pinned protocol does not accept `dynamicTools` on `turn/start`; they persist in the Codex thread metadata. When resuming a binding created by an older VoicePet version, send the same dynamic-tool definitions through the supported `thread/resume` raw mapping before starting the next turn. Apply only `codex_turn_options(mode)` at each turn so toolbar mode changes take effect on the next turn without re-registering tools.

- [ ] **Step 4: Implement the dynamic action executor**

`AgentActionExecutor` validates the exact schemas from Task 1. Resolve and display absolute paths without restricting their drive. File actions accept regular text files up to 512 KiB, reject directories and reparse-point surprises, compare `expected_sha256` immediately before mutation, and write through a sibling temporary file followed by `os.replace`. `edit` applies ordered `{old_text, new_text, replace_all}` replacements and requires each non-global `old_text` to occur exactly once. `create` requires a missing path, `replace` requires an existing regular file, and `delete` unlinks one regular file. Directory and recursive deletion must go through the prompted shell action.

Shell actions resolve `program` with `shutil.which` or an existing absolute path, validate cwd, and call `asyncio.create_subprocess_exec(program, *args, cwd=cwd)` without `shell=True`. Enforce the requested timeout capped at 1800 seconds, capture at most 1 MiB per stream, and verify the subprocess inherits the Worker's Job Object/process group. Return structured exit code, truncated stdout/stderr, duration, and timeout state.

Write tests for successful create/edit/replace/delete, stale hashes, ambiguous edits, reparse paths, directory rejection, argv preservation, timeout, cancellation, output limits, and secret-free errors.

- [ ] **Step 5: Implement operation classification**

Classify dynamic actions before approval:

```python
def classify_dynamic_action(tool: str, arguments: Mapping[str, object]) -> str:
    if tool == "voicepet_shell":
        return "command"
    if tool != "voicepet_file_change":
        raise ValueError("Agent 动态工具无效")
    operation = arguments.get("operation")
    try:
        return {
            "create": "file_create",
            "edit": "file_patch",
            "replace": "file_replace",
            "delete": "file_delete",
        }[operation]
    except (KeyError, TypeError) as error:
        raise ValueError("Agent 文件操作无效") from error
```

Unknown tools or operations are declined. The approval handler sends an `approval_request` event and blocks for the main-process decision before invoking `AgentActionExecutor`; Auto Edit bypasses the UI only for create and edit; Full Auto bypasses it for every dynamic action. Built-in command approval requests are always declined because built-in shell tools are disabled. Built-in file changes in interactive modes remain blocked by the read-only sandbox.

Encode a successful dynamic-tool response as `{"contentItems": [{"type": "inputText", "text": result_json}], "success": true}` and a declined or failed response with `success: false` plus a bounded user-safe text item. The fake app-server contract in Task 1 verifies this response shape against the pinned runtime protocol.

- [ ] **Step 6: Translate Codex notifications**

Map typed notifications and unknown-but-recognized method dictionaries into `AgentEvent`. Emit proposed dynamic file changes before approval and completed changes only after the executor succeeds. Continue translating native `fileChange` items in Full Auto for display. Bound command output chunks to 32 KiB and safe error text to 512 characters. Track a monotonically increasing sequence per VoicePet turn.

- [ ] **Step 7: Run adapter and action tests**

Run: `python -m pytest tests/core/test_agent_codex.py tests/core/test_agent_actions.py tests/core/test_agent_codex_contract.py -q`

Expected: pass without an OpenAI API call.

- [ ] **Step 8: Commit**

```powershell
git add core/agent_codex.py core/agent_actions.py tests/core/test_agent_codex.py tests/core/test_agent_actions.py
git commit -m "feat: adapt Codex threads approvals and events"
```

## Task 6: Build the Agent Worker and main-process client

**Files:**
- Create: `core/agent_worker.py`
- Create: `core/agent_client.py`
- Modify: `app.py`
- Create: `tests/core/test_agent_worker.py`
- Create: `tests/core/test_agent_client.py`
- Modify: `tests/test_app_entry.py`

- [ ] **Step 1: Write Worker lifecycle tests**

Use in-memory asyncio streams or subprocess pipes with a fake Codex adapter. Verify initialize precedes turn start, only one active turn is accepted, duplicate turn IDs are rejected, approval IDs are consumed once, cancel interrupts the matching Codex turn, and shutdown drains terminal events.

- [ ] **Step 2: Write client routing and backpressure tests**

Verify request futures and notifications are read by one reader task, event order is preserved, a bounded normal-event queue cannot block cancellation or approval responses, EOF fails every pending future, and late events from a cancelled turn are ignored after its terminal sequence.

- [ ] **Step 3: Run tests and verify failures**

Run: `python -m pytest tests/core/test_agent_worker.py tests/core/test_agent_client.py tests/test_app_entry.py -q`

Expected: missing worker/client and missing `--agent-worker` dispatch.

- [ ] **Step 4: Implement the Worker server**

`AgentWorkerServer` owns one adapter and one active task. Its request handlers return acceptance responses immediately; turn output is emitted as notifications. Use a priority write queue with approval/cancel/shutdown responses above command-output deltas. Reject a second active turn with code `-32001`. Redact caught exceptions through a fixed error mapper before encoding.

- [ ] **Step 5: Implement the main-process client**

`AgentWorkerClient` exposes these concrete methods:

```python
async def initialize(self) -> AgentCapabilities:
    result = await self._call("agent.initialize", {"protocol_version": 1})
    return AgentCapabilities.from_mapping(result)

async def start_turn(self, request: AgentTurnRequest) -> None:
    await self._call("agent.turn.start", request.to_mapping())

async def cancel_turn(self, turn_id: str) -> None:
    await self._call("agent.turn.cancel", {"turn_id": turn_id, "reason": "user_cancelled"})

async def resolve_approval(self, approval_id: str, decision: str) -> None:
    await self._call("agent.approval.resolve", {"approval_id": approval_id, "decision": decision})

async def next_event(self) -> AgentEvent:
    return await self._events.get()

async def close(self) -> None:
    if self._closed:
        return
    self._closed = True
    await self._call_while_closing("agent.shutdown", {})
    self._writer.close()
    await self._writer.wait_closed()
    await self._reader_task
```

Define `AgentCapabilities.from_mapping()` and `AgentTurnRequest.to_mapping()` in `core/agent_types.py`. `_call()` allocates a UUID request ID, writes one encoded request under `_write_lock`, and awaits the matching future populated by the sole reader task. `close()` must reject new calls before sending shutdown through a private `_call_while_closing()` helper so the closed-state guard does not reject its own shutdown request.

- [ ] **Step 6: Add the app entry mode**

Add `_run_agent_worker()` and `--agent-worker` to the mutually exclusive parser group. Dispatch before UI imports, matching the existing Tool Worker pattern.

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/core/test_agent_worker.py tests/core/test_agent_client.py tests/test_app_entry.py -q`

Expected: pass.

- [ ] **Step 8: Commit**

```powershell
git add core/agent_worker.py core/agent_client.py app.py tests/core/test_agent_worker.py tests/core/test_agent_client.py tests/test_app_entry.py
git commit -m "feat: run Codex Agent through an isolated worker"
```

## Task 7: Own and terminate the Windows process tree

**Files:**
- Create: `core/windows_job.py`
- Create: `core/agent_process.py`
- Create: `tests/core/test_windows_job.py`
- Create: `tests/core/test_agent_process.py`

- [ ] **Step 1: Write platform-neutral process-manager tests**

Inject a fake job owner and fake subprocess. Verify the manager sends bootstrap credentials through stdin, never argv, allows 30 seconds for initialization, interrupts for 5 seconds, closes the runtime, then waits 2 seconds before killing the owned tree. Verify accepted turns are never automatically replayed after a crash.

- [ ] **Step 2: Write Windows Job Object tests guarded by `sys.platform == "win32"`**

Start a harmless child Python process that starts a long-lived grandchild. Assign the child to the Job Object, close the job, and assert both PIDs exit within five seconds. Use only processes created by the test.

- [ ] **Step 3: Implement `WindowsJob`**

Use `ctypes.WinDLL("kernel32", use_last_error=True)` with `CreateJobObjectW`, `SetInformationJobObject`, `AssignProcessToJobObject`, `GetCurrentProcess`, and `CloseHandle`. Configure `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. `Agent Worker` creates the Job Object and assigns its own process before it initializes the SDK, so no Codex child can start before ownership exists. Raise `WindowsJobError` with operation names and Win32 codes, never raw environment or command data.

- [ ] **Step 4: Implement `AgentWorkerProcessManager`**

Follow `ToolWorkerProcessManager` lifecycle patterns but start `app.py --agent-worker`, send a bounded bootstrap containing API Key, data directory, model, system prompt, and runtime ID over stdin, then create `AgentWorkerClient`. The first Worker action on Windows is `WindowsJob.own_current_process()`; only after that succeeds may it construct `CodexClient`. On other platforms, start the Worker in a new process session and terminate that process group during forced shutdown.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/core/test_windows_job.py tests/core/test_agent_process.py -q`

Expected: pass; the Windows process-tree test must prove both owned PIDs exit.

- [ ] **Step 6: Commit**

```powershell
git add core/windows_job.py core/agent_process.py tests/core/test_windows_job.py tests/core/test_agent_process.py
git commit -m "feat: manage the Codex Agent process tree"
```

## Task 8: Integrate Agent turns with sessions, memory, attachments, and Coordinator

**Files:**
- Create: `core/agent_gateway.py`
- Modify: `core/coordinator.py`
- Modify: `core/events.py`
- Modify: `core/runtime.py`
- Create: `tests/core/test_agent_gateway.py`
- Create: `tests/core/test_coordinator_agent.py`
- Modify: `tests/core/test_runtime.py`

- [ ] **Step 1: Write gateway tests**

Verify a session without a binding creates a Codex thread and persists it, a bound session resumes without replaying all history, a legacy session sends one bounded summary plus recent turns, relevant long-term memory is included once, image attachments become local-image inputs, and text/file metadata remain bounded.

- [ ] **Step 2: Write Coordinator routing tests**

Test text and transcribed voice use the same gateway, only user-facing text deltas are spoken, progress events reach the EventBus, one app-wide turn prevents a second start, mode is frozen at start, cancellation ignores late events, and a turn accepted by Agent is never replayed through the legacy LLM.

- [ ] **Step 3: Add stable runtime events**

Add frozen events `AgentProgressReady`, `AgentApprovalRequested`, `AgentApprovalResolved`, and `AgentTurnFinished`. Payloads contain IDs, kind, safe label, display details, and terminal status. Add `agent.*` error codes to `_RUNTIME_ERROR_COMPONENTS`.

- [ ] **Step 4: Implement `AgentGateway`**

Expose `run_turn(request, token) -> AsyncIterator[AgentEvent]`, `resolve_approval()`, and `close()`. The gateway owns event consumption, store writes, thread binding, mode snapshots, legacy-context bootstrap, and safe summaries. Stop reading a turn after the first terminal event and mark gaps or disconnects `outcome_unknown`.

- [ ] **Step 5: Route Coordinator through the gateway**

Add optional `agent_gateway` and `agent_enabled`. In `_run_llm`, branch before building `LlmRequest`; translate Agent events to existing `TextDelta`, tool/progress events, archive completion, and TTS. Keep the old path in a named `_run_legacy_llm` method so an explicit fallback can call it before an Agent turn is accepted.

- [ ] **Step 6: Extend RuntimeHost**

Add thread-safe submitted methods for `set_session_agent_mode(session_id, mode_or_none)`, `resolve_agent_approval(approval_id, decision)`, and `agent_capabilities()`. These call the gateway or session service on the runtime asyncio loop.

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/core/test_agent_gateway.py tests/core/test_coordinator_agent.py tests/core/test_runtime.py tests/core/test_coordinator.py -q`

Expected: pass.

- [ ] **Step 8: Commit**

```powershell
git add core/agent_gateway.py core/coordinator.py core/events.py core/runtime.py tests/core/test_agent_gateway.py tests/core/test_coordinator_agent.py tests/core/test_runtime.py
git commit -m "feat: route VoicePet conversations through Codex Agent"
```

## Task 9: Assemble runtime authentication and explicit fallback

**Files:**
- Modify: `core/runtime_factory.py`
- Modify: `core/runtime.py`
- Modify: `ui/application.py`
- Modify: `tests/core/test_runtime_factory.py`
- Modify: `tests/ui/test_application.py`

- [ ] **Step 1: Write assembly tests**

Assert that Agent-enabled official OpenAI configuration with a non-empty DPAPI-provided key constructs `AgentStore`, `AgentWorkerProcessManager`, and `AgentGateway`; a missing key reports Agent unconfigured; a custom compatible endpoint remains available only to the legacy backend; and the key appears in neither process command nor service repr.

- [ ] **Step 2: Run and verify failures**

Run: `python -m pytest tests/core/test_runtime_factory.py tests/ui/test_application.py -q`

Expected: missing Agent assembly behavior.

- [ ] **Step 3: Build the Agent services**

Create `%LOCALAPPDATA%\VoicePet\data\codex`, instantiate the Agent store on `assistant.db`, and pass the API Key only to the Worker bootstrap object. Add the process manager as a `RuntimeServices` lifecycle member and the gateway as a closer. Pass `config.llm.model`, reasoning effort, system prompt, and `config.agent.max_turn_minutes`.

- [ ] **Step 4: Define fallback behavior**

Expose backend availability as `agent`, `legacy`, or `unconfigured`. Before Agent accepts a turn, a startup error can return a UI action to use legacy chat. After acceptance, surface the Agent error and require a new user action; do not resubmit automatically.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/core/test_runtime_factory.py tests/ui/test_application.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```powershell
git add core/runtime_factory.py core/runtime.py ui/application.py tests/core/test_runtime_factory.py tests/ui/test_application.py
git commit -m "feat: assemble the authenticated Codex Agent runtime"
```

## Task 10: Add settings and the chat-toolbar mode selector

**Files:**
- Modify: `ui/viewmodels/settings.py`
- Modify: `ui/viewmodels/chat.py`
- Modify: `ui/qml/screens/settings/AiSettings.qml`
- Modify: `ui/qml/components/ChatComposer.qml`
- Modify: `ui/qml/screens/ChatPage.qml`
- Modify: `tests/ui/qml/test_settings_viewmodel.py`
- Modify: `tests/ui/qml/test_chat_viewmodel.py`
- Create: `tests/ui/qml/test_agent_mode_selector.py`

- [ ] **Step 1: Write SettingsViewModel tests**

Set `agent.enabled` and `agent.default_approval_mode` through the draft, save, and assert the stored `AppConfig` contains the enabled state and `suggest`, `auto_edit`, or `full_auto`. Assert invalid values create a field error. Disabling Agent is the explicit pure-chat fallback and must not discard session bindings.

- [ ] **Step 2: Write ChatViewModel mode tests**

Assert the effective mode starts at the saved default, a session override calls `runtime.set_session_agent_mode`, switching during processing updates the UI label to “下一轮生效”, switching sessions loads that session's override, and clearing the override restores the current default.

- [ ] **Step 3: Implement view-model properties**

Add `agentModeChanged`, `agentMode`, `agentModeLabel`, `agentModePending`, `agentModeOptions`, `set_agent_mode(str)`, and `use_default_agent_mode()`. Keep serialized values in Python and expose localized labels separately:

```python
AGENT_MODE_LABELS = {
    "suggest": "建议模式",
    "auto_edit": "自动编辑",
    "full_auto": "全自动",
}
```

- [ ] **Step 4: Add the settings selector**

In `AiSettings.qml`, add an `AppToggle` labeled “使用 Codex Agent” and an `AppComboBox` labeled “默认 Agent 模式” with the three Chinese labels and values. Disabling the toggle selects the existing pure-chat backend. Under the selector, explain that the input toolbar can override the mode per session and that Full Auto executes without VoicePet approval.

- [ ] **Step 5: Add the composer selector**

Add a compact left-aligned toolbar button after the attachment button. Its popup contains three rows with title, description, selected checkmark, keyboard focus, and accessible names. Bind `mode`, `modeOptions`, `modePending`, and `modeRequested` properties on `ChatComposer`; wire them in `ChatPage.qml` to `ChatViewModel`.

- [ ] **Step 6: Run UI tests**

Run: `python -m pytest tests/ui/qml/test_settings_viewmodel.py tests/ui/qml/test_chat_viewmodel.py tests/ui/qml/test_agent_mode_selector.py -q`

Expected: pass for all three modes, per-session overrides, and next-turn status.

- [ ] **Step 7: Commit**

```powershell
git add ui/viewmodels/settings.py ui/viewmodels/chat.py ui/qml/screens/settings/AiSettings.qml ui/qml/components/ChatComposer.qml ui/qml/screens/ChatPage.qml tests/ui/qml/test_settings_viewmodel.py tests/ui/qml/test_chat_viewmodel.py tests/ui/qml/test_agent_mode_selector.py
git commit -m "feat: add per-session Agent mode controls"
```

## Task 11: Present Agent progress and resolve approvals

**Files:**
- Modify: `ui/viewmodels/chat.py`
- Modify: `ui/viewmodels/dialogs.py`
- Modify: `ui/qml/windows/ToolConfirmationWindow.qml`
- Modify: `ui/qml/components/TaskCenter.qml`
- Modify: `tests/ui/qml/test_confirmation_presentation.py`
- Create: `tests/ui/qml/test_agent_progress.py`

- [ ] **Step 1: Write approval presentation tests**

Verify command requests display command and cwd; file requests display operation, absolute paths, and bounded diff; Accept and Decline call exactly one runtime resolution; closing the window resolves `cancel`; Full Auto never creates the dialog.

- [ ] **Step 2: Write progress tests**

Feed plan, command start/output/completion, file proposal/completion, usage, error, and terminal events. Assert `TaskCenter` uses stable item IDs, proposed changes are not labeled completed, output is bounded, and terminal status clears `processing` once.

- [ ] **Step 3: Extend dialog coordination**

Add an Agent-specific approval model carrying `approvalId`, `kind`, `title`, `summary`, `command`, `cwd`, `changes`, and allowed decisions. Reuse the existing confirmation window shell, but render command and diff blocks according to kind. Never log the display payload.

- [ ] **Step 4: Extend TaskCenter**

Represent plans and tool work as status rows linked by `turn_id/item_id`. Keep assistant text in message bubbles; command output and diffs remain collapsed task details and are never sent to TTS.

- [ ] **Step 5: Run UI tests**

Run: `python -m pytest tests/ui/qml/test_confirmation_presentation.py tests/ui/qml/test_agent_progress.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```powershell
git add ui/viewmodels/chat.py ui/viewmodels/dialogs.py ui/qml/windows/ToolConfirmationWindow.qml ui/qml/components/TaskCenter.qml tests/ui/qml/test_confirmation_presentation.py tests/ui/qml/test_agent_progress.py
git commit -m "feat: show Codex progress and approval requests"
```

## Task 12: Package the pinned runtime and verify release behavior

**Files:**
- Modify: `VoicePet.spec`
- Modify: `packaging/build_release.ps1`
- Modify: `tests/test_packaging_spec.py`
- Create: `tests/core/test_agent_redaction.py`
- Modify: `README.md`

- [ ] **Step 1: Write packaging and redaction tests**

Assert the spec collects `openai_codex`, `codex_cli_bin`, its runtime binary/data, and Pydantic models. Scan encoded bootstrap errors, structured logs, diagnostics, Agent events, and process commands using a sentinel `sk-voicepet-secret`; assert the sentinel never appears outside the private bootstrap byte buffer used by the test.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_packaging_spec.py tests/core/test_agent_redaction.py -q`

Expected: missing runtime collection and/or redaction behavior.

- [ ] **Step 3: Update PyInstaller collection**

Use `collect_all("openai_codex")` and `collect_all("codex_cli_bin")`, merge their data, binaries, and hidden imports, and include the `--agent-worker` path in frozen entry tests. Build script installs `.[audio,asr,wake,llm,tts,tools,ui,agent,build]`.

- [ ] **Step 4: Extend smoke testing**

Make `--smoke-test` verify the bundled Codex binary exists and can print a version without starting a model turn. Add an opt-in `VOICEPET_AGENT_API_SMOKE=1` path in the build script that uses the configured DPAPI key to run a text-only turn, a harmless current-directory command, and a file edit inside a newly created temporary directory; it must never use arbitrary user files.

- [ ] **Step 5: Document runtime behavior**

Update README setup, official OpenAI-only Agent authentication, three approval modes, toolbar override semantics, full-computer access, UAC boundary, local Codex data location, cancellation limits, and explicit legacy fallback.

- [ ] **Step 6: Run the complete verification suite**

Run:

```powershell
python -m pytest -q
python -m ruff check .
```

Expected: both commands exit 0.

- [ ] **Step 7: Build and smoke-test the frozen application**

Run:

```powershell
.\packaging\build_release.ps1 -Python (Resolve-Path '.\venv\Scripts\python.exe').Path
.\dist\VoicePet\VoicePet.exe --smoke-test
```

Expected: build completes and smoke test exits 0. Inspect Task Manager during the temporary process-tree integration test and confirm no VoicePet-owned Codex runtime or command child remains after cancellation and application exit.

- [ ] **Step 8: Commit**

```powershell
git add VoicePet.spec packaging/build_release.ps1 tests/test_packaging_spec.py tests/core/test_agent_redaction.py README.md
git commit -m "build: package and verify the Codex Agent runtime"
```

## Final acceptance pass

- [ ] Start VoicePet with a dedicated test API Key and confirm API-key authentication without ChatGPT login.
- [ ] In Suggest mode, request one patch and one harmless command; approve the patch, decline the command, and verify only the patch completes.
- [ ] In Auto Edit, create and patch a file under a temporary directory without prompts, then verify delete and command requests prompt.
- [ ] In Full Auto, create, replace, delete, and run a harmless command under the temporary directory without VoicePet prompts.
- [ ] Switch from Full Auto to Suggest during a running turn; verify the current turn keeps its snapshot and the next turn prompts.
- [ ] Cancel a long-running harmless child process and verify the Worker/runtime/child process tree exits.
- [ ] Resume the same VoicePet session after restart and verify the Codex thread continues without replaying the entire old history.
- [ ] Delete the test VoicePet session and verify only its VoicePet-owned Codex records are removed.
- [ ] Search logs and diagnostics for the test key and test file contents; verify neither appears.
- [ ] Run `git status --short` and review only intended changes and commits.
