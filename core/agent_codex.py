"""Codex SDK 的异步适配与 VoicePet 事件翻译"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from .agent_types import AgentApprovalMode, AgentEvent, AgentEventType, AgentTurnRequest

CODEX_SDK_VERSION = "0.147.0"
AGENT_TOOL_INSTRUCTIONS = """使用 Codex 原生文件工具和命令工具完成用户要求的操作
创建和修改文本文件必须直接调用原生 apply_patch 工具，使用绝对路径也可以
不要通过 shell_command、exec_command、PowerShell Set-Content、重定向或 Python 脚本写入文本文件
查询路径和读取内容与写入分开执行，不要把读取、写入和命令执行合并为一条 shell 命令
删除、移动文件和运行程序仍使用原生命令工具
所有原生工具请求都会由 VoicePet 按当前审批模式处理
不要因为当前工作目录或 Windows 沙箱提示而拒绝执行用户要求的操作
"""
AGENT_MODE_INSTRUCTIONS = {
    AgentApprovalMode.SUGGEST: "当前为建议模式，文件编辑和命令执行需要用户确认",
    AgentApprovalMode.AUTO_EDIT: "当前为自动编辑模式，原生 apply_patch 文本创建和修改自动批准，shell 命令仍需用户确认，不要为普通文本编辑选择 shell 工具",
    AgentApprovalMode.FULL_AUTO: "当前为全自动模式，原生工具按用户要求直接执行",
}
try:
    from openai_codex import __version__ as _SDK_VERSION
    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.client import CodexConfig
except ImportError:
    _SDK_VERSION = None; AsyncCodexClient = None; CodexConfig = None
class AgentCodexCompatibilityError(RuntimeError):
    code = "agent.codex_compatibility"
def require_supported_sdk() -> None:
    if _SDK_VERSION != CODEX_SDK_VERSION: raise AgentCodexCompatibilityError(f"需要 openai-codex {CODEX_SDK_VERSION}，当前为 {_SDK_VERSION or '未安装'}")
def dynamic_tool_specs() -> list[dict[str, Any]]:
    return [{"type":"function","name":"voicepet_file_change","description":"在当前电脑上创建、编辑、替换或删除文本文件","inputSchema":{"type":"object","properties":{"operation":{"type":"string","enum":["create","edit","replace","delete"]},"path":{"type":"string","maxLength":32767},"expected_sha256":{"type":["string","null"]},"content":{"type":"string","maxLength":524288},"edits":{"type":"array","maxItems":64,"items":{"type":"object","properties":{"old_text":{"type":"string","maxLength":131072},"new_text":{"type":"string","maxLength":131072},"replace_all":{"type":"boolean"}},"required":["old_text","new_text","replace_all"],"additionalProperties":False}}},"required":["operation","path","expected_sha256","content","edits"],"additionalProperties":False}},{"type":"function","name":"voicepet_shell","description":"使用参数数组在指定目录运行一个程序","inputSchema":{"type":"object","properties":{"program":{"type":"string","maxLength":32767},"args":{"type":"array","maxItems":128,"items":{"type":"string","maxLength":8192}},"cwd":{"type":"string","maxLength":32767},"timeout":{"type":"integer","minimum":1,"maximum":1800}},"required":["program","args","cwd","timeout"],"additionalProperties":False}}]
def codex_thread_options(mode: AgentApprovalMode) -> dict[str, Any]:
    if not isinstance(mode, AgentApprovalMode): raise ValueError("Agent 审批模式无效")  # noqa: TRY004
    return {"sandbox":"danger-full-access","approvalPolicy":"never" if mode is AgentApprovalMode.FULL_AUTO else "untrusted"}
def codex_turn_options(mode: AgentApprovalMode) -> dict[str, Any]:
    if not isinstance(mode, AgentApprovalMode): raise ValueError("Agent 审批模式无效")  # noqa: TRY004
    return {"sandboxPolicy":{"type":"dangerFullAccess"},"approvalPolicy":"never" if mode is AgentApprovalMode.FULL_AUTO else "untrusted"}
class CodexAgentAdapter:
    """Worker 内拥有 Codex 客户端，主进程只接收安全事件"""
    def __init__(self, *, api_key: str, data_directory: str | Path, model: str, base_url: str = "", reasoning_effort: str = "low", system_prompt: str = "") -> None:
        require_supported_sdk()
        if not api_key or "\n" in api_key or "\r" in api_key: raise ValueError("Agent API Key 无效")
        from .config import validate_llm_base_url
        validate_llm_base_url(base_url)
        self._base_url = base_url
        self._active_mode = AgentApprovalMode.SUGGEST
        self._interaction_waiters: dict[str, tuple[threading.Event, dict[str, str]]] = {}
        self._interaction_callback: Any = None
        self._prepared_actions: dict[str, Any] = {}
        self._capabilities = None
        self._api_key=api_key; self._data_directory=Path(data_directory).resolve(); self._model=model; self._reasoning_effort=reasoning_effort; self._system_prompt=system_prompt; self._client=None; self._turns={}; self._cancelled=set()
    async def initialize(self) -> dict[str, Any]:
        if self._capabilities is not None:
            return self._capabilities
        self._data_directory.mkdir(parents=True,exist_ok=True)
        env={key:value for key,value in os.environ.items() if key.upper() in {"PATH","SYSTEMROOT","WINDIR","TEMP","TMP","LANG","LC_ALL"}}; env["CODEX_HOME"]=str(self._data_directory)
        overrides = [
            'cli_auth_credentials_store="ephemeral"',
        ]
        if self._base_url:
            # 自定义服务使用独立提供方，地址保留调用方配置的路径
            overrides.extend([
                'model_provider="voicepet_custom"',
                'model_providers.voicepet_custom.name="VoicePet Custom"',
                "model_providers.voicepet_custom.base_url=" + json.dumps(self._base_url),
                'model_providers.voicepet_custom.wire_api="responses"',
                "model_providers.voicepet_custom.requires_openai_auth=true",
            ])
        config=CodexConfig(cwd=str(self._data_directory),env=env,config_overrides=tuple(overrides),client_name="voicepet",client_title="VoicePet",client_version=CODEX_SDK_VERSION)
        # Codex CLI 是 Agent Worker 的内部子进程，不能继承出新的控制台窗口
        if os.name == "nt":
            import openai_codex.client as codex_client
            original_popen = codex_client.subprocess.Popen

            def hidden_popen(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
                return original_popen(*args, **kwargs)

            codex_client.subprocess.Popen = hidden_popen
        self._client = AsyncCodexClient(config)
        self._client._sync._approval_handler = self._handle_server_request
        try:
            await self._client.start()
            await self._client.initialize()
            # API Key 登录同步完成，不存在浏览器登录使用的 loginId
            await self._client.account_login_start({"type": "apiKey", "apiKey": self._api_key})
        except BaseException:
            await self.close()
            raise
        self._capabilities = {"protocol_version":1,"sdk_version":CODEX_SDK_VERSION,"runtime_version":CODEX_SDK_VERSION,"supports_approvals":False,"supports_full_access":True}
        return self._capabilities

    def _handle_server_request(self, method: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """同步回答 Codex 的审批和动态工具请求，避免返回无效响应"""
        payload = params or {}
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            if self._active_mode is AgentApprovalMode.FULL_AUTO:
                return {"decision": "accept"}
            if self._active_mode is AgentApprovalMode.AUTO_EDIT and method == "item/fileChange/requestApproval":
                return {"decision": "accept"}
            if self._active_mode is AgentApprovalMode.AUTO_EDIT and method == "item/commandExecution/requestApproval" and self._is_text_edit_command(payload):
                return {"decision": "accept"}
            request_id = str(payload.get("itemId") or payload.get("callId") or "approval")
            event = threading.Event()
            result: dict[str, str] = {}
            self._interaction_waiters[request_id] = (event, result)
            published = self._publish_interaction(request_id, "Agent 请求执行操作", "是否允许 Codex 执行这项操作？")
            if published:
                event.wait(300)
            self._interaction_waiters.pop(request_id, None)
            return {"decision": result.get("decision", "decline")}
        if method == "item/tool/call":
            arguments = payload.get("arguments")
            if not isinstance(arguments, Mapping):
                return {"contentItems": [{"type": "inputText", "text": "参数无效"}], "success": False}
            request_id = str(payload.get("callId") or payload.get("itemId") or "tool")
            tool = str(payload.get("tool", ""))
            try:
                from .agent_actions import AgentActionExecutor
                action = AgentActionExecutor().prepare(tool, arguments)
                self._prepared_actions[request_id] = action
            except Exception:  # noqa: BLE001 工具参数只返回安全失败
                return {"contentItems": [{"type": "inputText", "text": "工具参数无效"}], "success": False}
            auto_execute = self._active_mode is AgentApprovalMode.FULL_AUTO or (
                self._active_mode is AgentApprovalMode.AUTO_EDIT
                and action.kind in {"file_create", "file_patch"}
            )
            if auto_execute:
                try:
                    output = asyncio.run(AgentActionExecutor().execute(action))
                    self._prepared_actions.pop(request_id, None)
                    return {"contentItems": [{"type": "inputText", "text": json.dumps(output, ensure_ascii=False)}], "success": True}
                except Exception as error:  # noqa: BLE001 工具执行只返回安全失败
                    self._prepared_actions.pop(request_id, None)
                    return {"contentItems": [{"type": "inputText", "text": str(error)}], "success": False}
            event = threading.Event()
            result: dict[str, str] = {}
            self._interaction_waiters[request_id] = (event, result)
            published = self._publish_interaction(request_id, "Agent 请求调用工具", "是否允许执行文件或命令操作？")
            if published:
                event.wait(300)
            self._interaction_waiters.pop(request_id, None)
            accepted = result.get("decision") == "accept"
            action = self._prepared_actions.pop(request_id, None)
            if not accepted or action is None:
                return {"contentItems": [{"type": "inputText", "text": "已拒绝"}], "success": False}
            try:
                output = asyncio.run(AgentActionExecutor().execute(action))
                text = json.dumps(output, ensure_ascii=False)
                return {"contentItems": [{"type": "inputText", "text": text}], "success": True}
            except Exception:  # noqa: BLE001 工具执行只返回安全失败
                return {"contentItems": [{"type": "inputText", "text": "工具执行失败"}], "success": False}
        if method.endswith(("/requestUserInput", "/elicitation")):
            request_id = str(payload.get("id") or payload.get("requestId") or payload.get("callId") or "input")
            event = threading.Event()
            result: dict[str, str] = {"kind": "input"}
            self._interaction_waiters[request_id] = (event, result)
            options = payload.get("options", payload.get("choices", ()))
            if not isinstance(options, (list, tuple)):
                options = ()
            published = self._publish_interaction(request_id, "Agent 需要你的选择", str(payload.get("message", "请选择一个选项")), kind="input", options=options)
            if published:
                event.wait(300)
            self._interaction_waiters.pop(request_id, None)
            answer = result.get("answer", "")
            return {"answers": {"answer": answer} if answer else {}, "cancelled": not bool(answer)}
        return {}

    @staticmethod
    def _is_text_edit_command(payload: Mapping[str, Any]) -> bool:
        """识别 Codex 偶尔用 shell 代替补丁执行的普通文本写入"""
        command = payload.get("command", "")
        if isinstance(command, (list, tuple)):
            command = " ".join(str(part) for part in command)
        if not isinstance(command, str):
            return False
        lowered = command.casefold()
        if not lowered or any(token in lowered for token in (
            "remove-item", "del ", " erase ", " move-item", "copy-item",
            "start-process", " stop-process", " invoke-", " git ",
            " npm ", " pip ", " chmod ", " reg ",
        )):
            return False
        return any(token in lowered for token in (
            "set-content", "add-content", "out-file", "writealltext",
            "writeallbytes", "apply_patch", "git apply",
        ))

    def resolve_interaction(self, request_id: str, decision: str) -> bool:
        waiter = self._interaction_waiters.get(request_id)
        if waiter is None or not isinstance(decision, str) or not decision:
            return False
        event, result = waiter
        if result.get("kind") == "input":
            if decision in {"cancel", "decline"}:
                result["answer"] = ""
            else:
                result["answer"] = decision
        else:
            result["decision"] = "accept" if decision == "accept" else "decline"
        event.set()
        return True

    def set_interaction_callback(self, callback: Any) -> None:
        self._interaction_callback = callback

    def _publish_interaction(self, request_id: str, title: str, message: str, *, kind: str = "approval", options: object = ()) -> bool:
        if self._interaction_callback is None:
            return False
        return bool(self._interaction_callback({
            "request_id": request_id,
            "session_id": self._active_request.session_id,
            "turn_id": self._active_request.turn_id,
            "kind": kind,
            "title": title,
            "message": message,
            "options": options if isinstance(options, (list, tuple)) else (),
        }))
    async def run_turn(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]:
        if self._client is None: raise RuntimeError("Agent 尚未初始化")
        self._active_mode = request.approval_mode
        self._active_request = request
        thread_id=request.thread_id
        instructions = "\n\n".join(item for item in (
            self._system_prompt.strip(), AGENT_TOOL_INSTRUCTIONS,
            AGENT_MODE_INSTRUCTIONS[request.approval_mode],
        ) if item)
        thread_options = {**codex_thread_options(request.approval_mode), "baseInstructions": instructions}
        if thread_id is None:
            thread_id=(await self._client.thread_start({**thread_options,"model":self._model,"cwd":str(self._data_directory),"ephemeral":False})).thread.id
        else:
            await self._client.thread_resume(thread_id,thread_options)
        inputs: list[dict[str, Any]] = [{"type":"text","text":request.input}]
        for attachment in request.attachments:
            if attachment["kind"] == "image":
                inputs.append({"type":"localImage","path":attachment["path"]})
            else:
                inputs.append({"type":"text","text":f"附件路径：{attachment['path']}，文件名：{attachment['name']}"})
        started=await self._client.turn_start(thread_id,inputs,{**codex_turn_options(request.approval_mode),"model":self._model,"effort":self._reasoning_effort,"summary":"concise"}); codex_turn_id=started.turn.id; self._turns[request.turn_id]=(thread_id,codex_turn_id); seq=0
        active_progress = False
        while True:
            notification = await self._client.next_turn_notification(codex_turn_id)
            method=notification.method; payload=notification.payload; kind=None; body={}
            if method=="item/agentMessage/delta":
                if active_progress:
                    yield AgentEvent(request.session_id,request.turn_id,seq,AgentEventType.COMMAND_COMPLETED,{"message":"命令执行完成"},thread_id,codex_turn_id); seq+=1; active_progress=False
                kind=AgentEventType.TEXT_DELTA; body={"text":getattr(payload,"delta","")}
            elif method=="item/commandExecution/outputDelta":
                if not active_progress:
                    yield AgentEvent(request.session_id,request.turn_id,seq,AgentEventType.COMMAND_STARTED,{"message":"正在执行命令"},thread_id,codex_turn_id); seq+=1; active_progress=True
                kind=AgentEventType.COMMAND_OUTPUT_DELTA; body={"text":getattr(payload,"delta","")}
            elif method=="item/fileChange/outputDelta":
                if not active_progress:
                    yield AgentEvent(request.session_id,request.turn_id,seq,AgentEventType.COMMAND_STARTED,{"message":"正在修改文件"},thread_id,codex_turn_id); seq+=1; active_progress=True
                kind=AgentEventType.COMMAND_OUTPUT_DELTA; body={"text":getattr(payload,"delta","")}
            elif method in {"item/commandExecution/started", "item/fileChange/started"}:
                kind=AgentEventType.COMMAND_STARTED
                body={"message": "正在执行命令" if "commandExecution" in method else "正在修改文件"}
            elif method == "item/commandExecution/completed":
                kind=AgentEventType.COMMAND_COMPLETED; body={"message": "命令执行完成"}
            elif method=="turn/completed": kind=AgentEventType.TURN_COMPLETED; status=getattr(getattr(payload,"turn",None),"status","completed"); body={"status":str(getattr(status,"value",status)).lower()}
            if kind is not None: yield AgentEvent(request.session_id,request.turn_id,seq,kind,body,thread_id,codex_turn_id); seq+=1
            if method=="turn/completed" or request.turn_id in self._cancelled: break
    async def cancel(self, turn_id: str) -> None:
        self._cancelled.add(turn_id)
        if self._client is not None and turn_id in self._turns: await self._client.turn_interrupt(*self._turns[turn_id])
    async def close(self) -> None:
        self._capabilities = None
        if self._client is not None: await self._client.close(); self._client=None

class DynamicToolReview:
    """动态工具在执行前返回接受或等待用户决定"""
    def __init__(self, *, accepted: bool, waiting_for_user: bool = False, result: Mapping[str, Any] | None = None):
        self.accepted = accepted
        self.waiting_for_user = waiting_for_user
        self.result = result or {}

class ApprovalBridge:
    def __init__(self):
        self._pending: dict[str, Any] = {}

    def request(self, approval_id: str) -> asyncio.Future[str]:
        future = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        return future

    def resolve(self, approval_id: str, decision: str) -> bool:
        future = self._pending.pop(approval_id, None)
        if future is None or future.done() or decision not in {"accept", "decline", "cancel"}:
            return False
        future.set_result(decision)
        return True

    def cancel_turn(self, turn_id: str) -> None:
        for approval_id, future in list(self._pending.items()):
            if approval_id.startswith(turn_id + ":") and not future.done():
                future.set_result("cancel")
                self._pending.pop(approval_id, None)


def classify_dynamic_action(tool: str, arguments: Mapping[str, object]) -> str:
    if tool == "voicepet_shell":
        return "command"
    if tool != "voicepet_file_change":
        raise ValueError("Agent 动态工具无效")
    try:
        return {"create": "file_create", "edit": "file_patch", "replace": "file_replace", "delete": "file_delete"}[arguments.get("operation")]
    except (KeyError, TypeError):
        raise ValueError("Agent 文件操作无效") from None

