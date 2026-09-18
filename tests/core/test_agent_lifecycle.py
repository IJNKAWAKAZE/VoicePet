"""验证轮次结束后立刻续聊和会话绑定的生命周期"""

import asyncio

import pytest

from core.agent_gateway import AgentGateway, AgentGatewayError
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
async def test_context_survives_worker_request_serialization(tmp_path):
    from core.agent_types import AgentTurnRequest

    client = TurnClient()

    async def factory():
        return client

    store = AgentStore(tmp_path / "agent.db")
    gateway = AgentGateway(factory, store)
    _ = [event async for event in gateway.run_turn("one", "我在哪个城市？", context="用户事实：我住在杭州")]
    request = AgentTurnRequest.from_mapping(client.requests[0].to_mapping())
    assert request.input == "我在哪个城市？"
    assert request.context == "用户事实：我住在杭州"
    assert "杭州" not in repr(request)


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


class ManualClient:
    """按轮次分队列，让测试自己决定每个会话何时推进"""

    def __init__(self):
        self.requests = []
        self.queues = {}
        self.cancelled = []

    async def initialize(self):
        return None

    async def start_turn(self, request):
        self.requests.append(request)
        self.queues[request.turn_id] = asyncio.Queue()

    async def next_turn_event(self, turn_id):
        return await self.queues[turn_id].get()

    async def cancel_turn(self, turn_id):
        self.cancelled.append(turn_id)

    async def close(self):
        return None

    def request(self, turn_id):
        return next(item for item in self.requests if item.turn_id == turn_id)

    async def emit(self, turn_id, type_, payload=None):
        request = self.request(turn_id)
        await self.queues[turn_id].put(
            AgentEvent(
                request.session_id,
                turn_id,
                0,
                type_,
                payload or {},
                f"thread-{request.session_id}",
                f"codex-{turn_id}",
            )
        )


async def wait_started(client, count):
    for _ in range(200):
        if len(client.requests) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError("并发轮次没有启动")


async def collect(stream):
    return [event async for event in stream]


@pytest.mark.anyio
async def test_sessions_run_together_while_one_session_stays_serial(tmp_path):
    client = ManualClient()
    store = AgentStore(tmp_path / "agent.db")

    async def factory():
        return client

    gateway = AgentGateway(factory, store)
    first = asyncio.create_task(collect(gateway.run_turn("one", "第一问", turn_id="t1")))
    second = asyncio.create_task(collect(gateway.run_turn("two", "第二问", turn_id="t2")))
    await wait_started(client, 2)

    # 同一会话的两个轮次仍然互斥，否则原生线程会被同时改写
    blocked = gateway.run_turn("one", "再来一问", turn_id="t3")
    with pytest.raises(AgentGatewayError, match="该会话已有轮次运行"):
        await anext(blocked)
    await blocked.aclose()
    assert store.turn("t3") is None

    await client.emit("t1", AgentEventType.TEXT_DELTA, {"text": "A"})
    await client.emit("t2", AgentEventType.TEXT_DELTA, {"text": "B"})
    await client.emit("t1", AgentEventType.TURN_COMPLETED, {"status": "completed"})
    await client.emit("t2", AgentEventType.TURN_COMPLETED, {"status": "completed"})

    assert [
        (event.turn_id, event.type) for event in await first
    ] == [("t1", AgentEventType.TEXT_DELTA), ("t1", AgentEventType.TURN_COMPLETED)]
    assert [
        (event.turn_id, event.type) for event in await second
    ] == [("t2", AgentEventType.TEXT_DELTA), ("t2", AgentEventType.TURN_COMPLETED)]
    assert [store.turn("t1").status, store.turn("t2").status] == [
        "completed",
        "completed",
    ]

    # 终态释放后同一会话可以继续复用原生线程
    follow = asyncio.create_task(collect(gateway.run_turn("one", "追问", turn_id="t4")))
    await wait_started(client, 3)
    assert client.request("t4").thread_id == "thread-one"
    await client.emit("t4", AgentEventType.TURN_COMPLETED, {"status": "completed"})
    assert [event.turn_id for event in await follow] == ["t4"]


@pytest.mark.anyio
async def test_cancel_targets_only_the_requested_turn(tmp_path):
    client = ManualClient()
    store = AgentStore(tmp_path / "agent.db")

    async def factory():
        return client

    gateway = AgentGateway(factory, store)
    first = asyncio.create_task(collect(gateway.run_turn("one", "第一问", turn_id="t1")))
    second = asyncio.create_task(collect(gateway.run_turn("two", "第二问", turn_id="t2")))
    await wait_started(client, 2)

    async def enter_maintenance():
        async with gateway.session_maintenance():
            pass

    with pytest.raises(AgentGatewayError, match="当前回复完成后才能删除会话"):
        await enter_maintenance()

    await gateway.cancel("t1")
    assert client.cancelled == ["t1"]

    await gateway.cancel()
    # 不带轮次号时取消所有活跃轮次，t1 尚未收到终态因此会再次下发
    assert client.cancelled == ["t1", "t1", "t2"]

    await client.emit("t1", AgentEventType.CANCELLED)
    await client.emit("t2", AgentEventType.TURN_COMPLETED, {"status": "completed"})
    assert (await first)[-1].type is AgentEventType.CANCELLED
    assert (await second)[-1].type is AgentEventType.TURN_COMPLETED
    assert store.turn("t1").status == "cancelled"

    async with gateway.session_maintenance():
        pass


@pytest.mark.anyio
async def test_session_maintenance_targets_only_the_deleted_session(tmp_path):
    client = ManualClient()
    store = AgentStore(tmp_path / "agent.db")
    stopped = []

    async def factory():
        return client

    async def stop_worker():
        stopped.append(True)

    gateway = AgentGateway(factory, store, stop_worker=stop_worker)
    running = asyncio.create_task(
        collect(gateway.run_turn("running", "第一问", turn_id="t1"))
    )
    await wait_started(client, 1)

    # 目标会话本身在跑时必须拒绝
    with pytest.raises(AgentGatewayError, match="当前回复完成后才能删除会话"):
        async with gateway.session_maintenance(("running",)):
            pass
    # 其它会话仍在执行时可以删除已结束的会话
    async with gateway.session_maintenance(("finished",)):
        pass
    # 但不能为了删除会话关掉仍被占用的 Worker
    assert stopped == []
    assert client.request("t1") is not None

    await client.emit("t1", AgentEventType.TURN_COMPLETED, {"status": "completed"})
    assert [event.turn_id for event in await running] == ["t1"]

    async with gateway.session_maintenance(("finished",)):
        pass
    assert stopped == [True]

