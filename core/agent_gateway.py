"""Coordinator 与 Codex Agent 之间的会话和事件边界"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from .agent_client import AgentWorkerClient
from .agent_store import AgentStore
from .agent_types import AgentApprovalMode, AgentEvent, AgentEventType, AgentTurnRequest


class AgentGatewayError(RuntimeError):
    code="agent.gateway"
class AgentGateway:
    def __init__(self, client_factory, store: AgentStore, *, default_mode: AgentApprovalMode=AgentApprovalMode.AUTO_EDIT, stop_worker=None):
        self._stop_worker = stop_worker
        self._session_maintenance = False
        self._client_factory=client_factory; self._store=store; self._default_mode=default_mode; self._global_mode=store.global_mode() or default_mode; self._client:AgentWorkerClient|None=None; self._active_turns:set[str]=set(); self._session_turns:dict[str,str]={}; self._mode_overrides={}; self._capabilities=None
    async def initialize(self):
        if self._client is None:
            self._client=await self._client_factory()
            self._capabilities=await self._client.initialize()
        return self._capabilities
    async def run_turn(self, session_id:str, text:str, *, mode:AgentApprovalMode|None=None, thread_id:str|None=None, turn_id:str|None=None, attachments=(), context:str="")->AsyncIterator[AgentEvent]:
        if self._session_maintenance: raise AgentGatewayError("正在清理会话，请稍后重试")
        # 不同会话可以并发执行，同一会话仍必须串行，否则原生线程会被两个轮次同时改写
        if session_id in self._session_turns: raise AgentGatewayError("该会话已有轮次运行")
        chosen=mode or self._global_mode; current=turn_id or uuid4().hex
        if thread_id is None:
            binding = self._store.binding(session_id)
            thread_id = binding.codex_thread_id if binding else None
        # 轮次使用全局模式快照并复用当前会话的原生线程
        request=AgentTurnRequest(session_id,thread_id,current,text,chosen,attachments=attachments,context=context); self._store.start_turn(current,session_id,chosen); self._active_turns.add(current); self._session_turns[session_id]=current
        try:
            if self._client is None: await self.initialize()
            assert self._client is not None; await self._client.start_turn(request)
            while True:
                event=await self._next_turn_event(current)
                if event.turn_id != current: continue
                self._store.append_event_summary(event)
                if event.thread_id:
                    self._store.bind_thread(session_id, event.thread_id, "codex")
                terminal = event.type in {AgentEventType.TURN_COMPLETED,AgentEventType.CANCELLED,AgentEventType.ERROR}
                if terminal:
                    status=event.payload.get("status", "failed") if event.type is AgentEventType.TURN_COMPLETED else ("cancelled" if event.type is AgentEventType.CANCELLED else "failed")
                    # 消费者可在收到终态后立即停止迭代，必须先完成记录和释放状态
                    self._store.finish_turn(current,status)
                    self._release_turn(session_id, current)
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
            self._release_turn(session_id, current)

    async def _next_turn_event(self, turn_id: str) -> AgentEvent:
        # 支持只实现全局事件流的旧客户端桩
        reader = getattr(self._client, "next_turn_event", None)
        if callable(reader):
            return await reader(turn_id)
        assert self._client is not None
        return await self._client.next_event()

    def _release_turn(self, session_id: str, turn_id: str) -> None:
        self._active_turns.discard(turn_id)
        if self._session_turns.get(session_id) == turn_id:
            self._session_turns.pop(session_id, None)
        release = getattr(self._client, "release_turn", None)
        if callable(release):
            release(turn_id)

    async def cancel(self, turn_id: str | None = None):
        if self._client is None: return
        targets = [turn_id] if turn_id else sorted(self._active_turns)
        for target in targets:
            if target: await self._client.cancel_turn(target)
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

    @asynccontextmanager
    async def session_maintenance(self, session_ids=None):
        """删除会话前释放线程持有者，只锁定本次要删除的会话"""
        targets = None if session_ids is None else {str(item) for item in session_ids}
        if self._session_maintenance:
            raise AgentGatewayError("正在清理会话，请稍后重试")
        if self._active_turns and (
            targets is None or set(self._session_turns) & targets
        ):
            raise AgentGatewayError("当前回复完成后才能删除会话")
        self._session_maintenance = True
        try:
            # 先关闭线程持有者，避免文件仍被占用或被缓冲写入重新创建。
            # 还有其它会话在跑时不能关闭它，否则会打断后台轮次。
            if not self._active_turns:
                if self._stop_worker is not None:
                    await self._stop_worker()
                    self._client = None
                    self._capabilities = None
                else:
                    await self.close()
            yield
            self._mode_overrides.clear()
        finally:
            self._session_maintenance = False

