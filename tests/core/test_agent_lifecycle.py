"""验证轮次结束后立刻续聊和会话绑定的生命周期"""

import asyncio

import pytest

from core.agent_gateway import AgentGateway
from core.agent_store import AgentStore
from core.agent_types import AgentEvent, AgentEventType


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TurnClient:
    def __init__(self):
        self.requests = []
        self.events = asyncio.Queue()

    async def initialize(self):
        return None

    async def start_turn(self, request):
        self.requests.append(request)
        thread = request.thread_id or f"thread-{request.session_id}"
        self.events.put_nowait(AgentEvent(
            request.session_id, request.turn_id, 0, AgentEventType.TURN_COMPLETED,
            {"status": "completed"}, thread, f"codex-{request.turn_id}",
        ))

    async def next_event(self):
        return await self.events.get()


@pytest.mark.anyio
async def test_terminal_event_releases_turn_before_consumer_stops(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    client = TurnClient()

    async def factory():
        return client

    gateway = AgentGateway(factory, store)
    first = gateway.run_turn("session", "创建文件", turn_id="first")
    second = gateway.run_turn("session", "修改刚才的文件", turn_id="second")
    try:
        # 消费者收到终态就停止迭代，保持生成器未关闭来复现实际调用方式
        assert (await anext(first)).type is AgentEventType.TURN_COMPLETED
        assert store.turn("first").status == "completed"
        assert (await anext(second)).type is AgentEventType.TURN_COMPLETED
        assert client.requests[1].thread_id == "thread-session"
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.anyio
async def test_binding_survives_restart_and_sessions_remain_isolated(tmp_path):
    path = tmp_path / "agent.db"
    client = TurnClient()

    async def factory():
        return client

    gateway = AgentGateway(factory, AgentStore(path))
    _ = [event async for event in gateway.run_turn("one", "创建文件")]
    gateway = AgentGateway(factory, AgentStore(path))
    _ = [event async for event in gateway.run_turn("two", "你好")]
    _ = [event async for event in gateway.run_turn("one", "修改刚才的文件")]
    assert [item.thread_id for item in client.requests] == [None, None, "thread-one"]

