import asyncio
from types import SimpleNamespace

from core.agent_codex import CodexAgentAdapter
from core.agent_types import AgentApprovalMode, AgentTurnRequest


def test_agent_context_is_applied_to_new_and_resumed_threads(tmp_path):
    async def scenario():
        options, inputs = [], []

        class Client:
            async def thread_start(self, config):
                options.append(config)
                return SimpleNamespace(thread=SimpleNamespace(id="thread"))

            async def thread_resume(self, thread, config):
                options.append(config)

            async def turn_start(self, thread, content, config):
                inputs.append(content)
                return SimpleNamespace(turn=SimpleNamespace(id="native-turn"))

            async def next_turn_notification(self, turn):
                return SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=SimpleNamespace(status="completed")))

        adapter = object.__new__(CodexAgentAdapter)
        adapter._client = Client()
        adapter._system_prompt = "你是桌面助手"
        adapter._data_directory = tmp_path
        adapter._model = "test"
        adapter._reasoning_effort = "low"
        adapter._turns = {}
        adapter._cancelled = set()
        for index, (thread, context) in enumerate(((None, "用户事实：住在杭州"), ("thread", "摘要：周六出发"), ("thread", ""))):
            request = AgentTurnRequest("session", thread, f"turn-{index}", "我在哪个城市？", AgentApprovalMode.SUGGEST, context=context)
            _ = [event async for event in adapter.run_turn(request)]
        assert all("杭州" not in item["baseInstructions"] for item in options)
        assert "杭州" in inputs[0][0]["text"]
        assert "周六出发" in inputs[1][0]["text"]
        assert "杭州" not in inputs[1][0]["text"]
        assert "周六出发" not in inputs[2][0]["text"]
        assert "本轮未提供记忆数据" in inputs[2][0]["text"]
        assert all(content[1] == {"type": "text", "text": "我在哪个城市？"} for content in inputs)

    asyncio.run(scenario())
