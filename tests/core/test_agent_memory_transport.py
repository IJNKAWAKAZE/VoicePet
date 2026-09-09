"""使用真实 Agent 运行程序和本地 HTTP 服务验证动态记忆实际出现在请求中。"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from core.agent_codex import CodexAgentAdapter
from core.agent_types import AgentApprovalMode, AgentTurnRequest
from core.memory import MemoryStore
from core.memory_context import MemoryContextAssembler
from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext


def test_live_thread_receives_new_memory_and_summary_on_next_turn(tmp_path):
    pytest.importorskip("openai_codex")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            index = len(requests)
            item = {"type": "message", "id": f"msg_{index}", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
            response = {"id": f"resp_{index}", "object": "response", "status": "completed",
                        "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in (
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": "OK"},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ):
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

    async def scenario():
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        memory = MemoryStore(tmp_path / "memory.db")
        archive = SessionArchiveStore(tmp_path / "memory.db")
        context = SessionContext()
        assembler = MemoryContextAssembler(memory, archive=archive, session_context=context)
        adapter = CodexAgentAdapter(api_key="local-test-only", data_directory=tmp_path / "agent",
                                   model="gpt-5.2", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        thread_id = None
        try:
            await adapter.initialize()
            for index in range(3):
                if index == 1:
                    record = memory.create_confirmed(category="user_requested", content="我在沈阳", source_turn_id=str(uuid4()))
                    turn = archive.archive_turn(str(uuid4()), "讨论计划", "确认", context.session_id)
                    summary = archive.save_summary(turn.turn_id, "出行安排", (), decisions=("周六出发",))
                elif index == 2:
                    memory.delete(record.id)
                    archive.delete_summary(summary.id)
                history = assembler.build_history("之前确定了什么？")
                request = AgentTurnRequest(context.session_id, thread_id, f"turn-{index}", "Reply OK",
                                           AgentApprovalMode.SUGGEST, context="\n".join(item["content"] for item in history))
                async with asyncio.timeout(25):
                    async for event in adapter.run_turn(request):
                        thread_id = event.thread_id or thread_id
            assert len(requests) == 3
            latest = [json.dumps(next(item for item in reversed(request["input"]) if item.get("role") == "user"),
                                 ensure_ascii=False) for request in requests]
            assert "我在沈阳" not in latest[0]
            assert "我在沈阳" in latest[1]
            assert "周六出发" in latest[1]
            assert "我在沈阳" not in latest[2]
            assert "周六出发" not in latest[2]
            assert "本轮未提供记忆数据" in latest[2]
        finally:
            await adapter.close()
            archive.close()
            memory.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    asyncio.run(scenario())
