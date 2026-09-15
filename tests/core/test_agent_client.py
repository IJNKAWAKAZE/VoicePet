import asyncio

import pytest

from core.agent_client import AgentClientError, AgentWorkerClient
from core.agent_rpc import (
    AgentRpcNotification,
    AgentRpcResponse,
    decode_message,
    encode_message,
)
from core.agent_types import (
    AgentApprovalMode,
    AgentEvent,
    AgentEventType,
    AgentTurnRequest,
)


class RecordingWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(decode_message(data))

    async def drain(self):
        pass


def test_worker_disconnect_releases_pending_event_and_rejects_new_calls():
    async def scenario():
        reader = asyncio.StreamReader()
        client = AgentWorkerClient(reader, None)
        pending = asyncio.create_task(client.next_event())
        await asyncio.sleep(0)
        reader.feed_eof()
        with pytest.raises(AgentClientError, match="断开"):
            await asyncio.wait_for(pending, 0.5)
        with pytest.raises(AgentClientError, match="断开"):
            await asyncio.wait_for(client.initialize(), 0.5)
        with pytest.raises(AgentClientError, match="断开"):
            await asyncio.wait_for(client.next_event(), 0.5)

    asyncio.run(scenario())


def test_cancelled_rpc_does_not_break_reader_on_late_response():
    from core.agent_rpc import AgentRpcResponse, decode_message, encode_message

    async def scenario():
        sent = []

        class Writer:
            def write(self, data):
                sent.append(decode_message(data))

            async def drain(self):
                pass

        reader = asyncio.StreamReader()
        client = AgentWorkerClient(reader, Writer())
        task = asyncio.create_task(client.initialize())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        reader.feed_data(encode_message(AgentRpcResponse(sent[0].request_id, result={})))
        await asyncio.sleep(0)
        assert not client._reader_task.done()
        assert not client._pending
        reader.feed_eof()
        await client._reader_task

    asyncio.run(scenario())


def test_terminal_event_waits_for_the_consumer_before_release():
    async def scenario():
        writer = RecordingWriter()
        reader = asyncio.StreamReader()
        client = AgentWorkerClient(reader, writer)
        request = AgentTurnRequest(
            "session", None, "turn-1", "问题", AgentApprovalMode.AUTO_EDIT
        )
        task = asyncio.create_task(client.start_turn(request))
        await asyncio.sleep(0)
        reader.feed_data(
            encode_message(
                AgentRpcResponse(writer.sent[0].request_id, result={"accepted": True})
            )
        )
        # 极快结束的轮次会在消费方开始读取前就收到终态
        reader.feed_data(
            encode_message(
                AgentRpcNotification(
                    "agent.event",
                    AgentEvent(
                        "session",
                        "turn-1",
                        0,
                        AgentEventType.TURN_COMPLETED,
                        {"status": "completed"},
                    ).to_mapping(),
                )
            )
        )
        await asyncio.wait_for(task, 0.5)

        event = await asyncio.wait_for(client.next_turn_event("turn-1"), 0.5)
        assert event.type is AgentEventType.TURN_COMPLETED

        client.release_turn("turn-1")
        assert "turn-1" not in client._turn_events
        reader.feed_eof()
        await client._reader_task

    asyncio.run(scenario())
