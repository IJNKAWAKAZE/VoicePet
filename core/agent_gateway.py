"""Coordinator 与 Codex Agent 之间的会话和事件边界"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from .agent_client import AgentWorkerClient
from .agent_store import AgentStore
from .agent_types import AgentApprovalMode, AgentEvent, AgentEventType, AgentTurnRequest


class AgentGatewayError(RuntimeError):
    code="agent.gateway"
class AgentGateway:
    def __init__(self, client_factory, store: AgentStore, *, default_mode: AgentApprovalMode=AgentApprovalMode.AUTO_EDIT):
        self._client_factory=client_factory; self._store=store; self._default_mode=default_mode; self._global_mode=store.global_mode() or default_mode; self._client:AgentWorkerClient|None=None; self._active_turn: str|None=None; self._active_task: asyncio.Task|None=None; self._mode_overrides={}; self._capabilities=None
    async def initialize(self):
        if self._client is None:
            self._client=await self._client_factory()
            self._capabilities=await self._client.initialize()
        return self._capabilities
    async def run_turn(self, session_id:str, text:str, *, mode:AgentApprovalMode|None=None, thread_id:str|None=None, turn_id:str|None=None, attachments=(), context:str="")->AsyncIterator[AgentEvent]:
        if self._active_turn is not None: raise AgentGatewayError("已有 Agent 轮次运行")
        chosen=mode or self._global_mode; current=turn_id or uuid4().hex
        if thread_id is None:
            binding = self._store.binding(session_id)
            thread_id = binding.codex_thread_id if binding else None
        await self.initialize()
        # 轮次使用全局模式快照并复用当前会话的原生线程
        request=AgentTurnRequest(session_id,thread_id,current,text,chosen,attachments=attachments,context=context); self._store.start_turn(current,session_id,chosen); self._active_turn=current
        try:
            if self._client is None: await self.initialize()
            assert self._client is not None; await self._client.start_turn(request)
            while True:
                event=await self._client.next_event()
                if event.turn_id != current: continue
                self._store.append_event_summary(event)
                if event.thread_id:
                    self._store.bind_thread(session_id, event.thread_id, "codex")
                terminal = event.type in {AgentEventType.TURN_COMPLETED,AgentEventType.CANCELLED,AgentEventType.ERROR}
                if terminal:
                    status=event.payload.get("status", "failed") if event.type is AgentEventType.TURN_COMPLETED else ("cancelled" if event.type is AgentEventType.CANCELLED else "failed")
                    # 消费者可在收到终态后立即停止迭代，必须先完成记录和释放状态
                    self._store.finish_turn(current,status)
                    self._active_turn = None
                yield event
                if terminal:
                    break
        except asyncio.CancelledError:
            try:
                if self._client is not None: await self._client.cancel_turn(current)
            finally:
                self._store.finish_turn(current,"cancelled")
            raise
        except Exception:
            self._store.finish_turn(current,"outcome_unknown"); raise
        finally:
            # 旧生成器延迟关闭时不能清除新轮次的运行状态
            if self._active_turn == current:
                self._active_turn = None
    async def cancel(self):
        if self._active_turn and self._client: await self._client.cancel_turn(self._active_turn)
    async def resolve_approval(self, approval_id:str, decision:str):
        if self._client is None: raise AgentGatewayError("Agent 尚未启动")
        await self._client.resolve_approval(approval_id,decision)
    def set_session_mode(self, session_id:str, mode:AgentApprovalMode|None):
        self._store.set_session_mode(session_id,mode)
        if mode is None: self._mode_overrides.pop(session_id,None)
        else: self._mode_overrides[session_id]=mode

    def session_mode(self, session_id: str) -> AgentApprovalMode:
        """按会话覆盖、持久化值和应用默认值计算当前模式"""
        override = self._mode_overrides.get(session_id)
        if override is not None:
            return override
        stored = self._store.session_mode(session_id)
        if stored is not None:
            self._mode_overrides[session_id] = stored
            return stored
        return self._default_mode

    def set_global_mode(self, mode: AgentApprovalMode) -> None:
        """设置跨会话沿用的当前 Agent 模式"""
        if not isinstance(mode, AgentApprovalMode):
            raise TypeError("Agent 审批模式无效")
        self._global_mode = mode
        self._store.set_global_mode(mode)

    def global_mode(self) -> AgentApprovalMode:
        """读取跨会话沿用的当前 Agent 模式"""
        return self._global_mode

    def reset_global_mode(self) -> None:
        """恢复应用启动时的默认 Agent 模式"""
        self._global_mode = self._default_mode
        self._store.set_global_mode(None)
    async def close(self):
        if self._client: await self._client.close(); self._client=None

