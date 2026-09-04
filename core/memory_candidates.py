"""验证模型推断并仅创建待用户审核的记忆候选"""

from __future__ import annotations

from collections.abc import Mapping

from .llm import ToolDefinition
from .memory import MemoryConfigurationError, MemoryRecord, MemoryStore

MEMORY_CANDIDATE_TOOL_NAME = "propose_memory"


def memory_candidate_tool_definition() -> ToolDefinition:
    """返回不会进入 Tool Worker 的严格候选记忆工具定义"""

    return ToolDefinition(
        MEMORY_CANDIDATE_TOOL_NAME,
        "提议一个稳定且不敏感的用户事实供用户稍后审核，不得用于密码、令牌、验证码、证件或支付信息",
        {
            "type": "object",
            "properties": {
                "category": {"type": "string", "maxLength": 64},
                "content": {"type": "string", "maxLength": 4096},
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
            },
            "required": ["category", "content", "confidence"],
            "additionalProperties": False,
        },
    )


class MemoryCandidateService:
    """在本地重新校验模型参数后写入 candidate 状态"""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def create(
        self,
        arguments: Mapping[str, object],
        source_turn_id: str,
    ) -> MemoryRecord:
        """创建一个永不自动确认的模型推断候选"""

        if set(arguments) != {"category", "content", "confidence"}:
            raise MemoryConfigurationError("记忆候选参数字段无效")
        category = arguments["category"]
        content = arguments["content"]
        confidence = arguments["confidence"]
        if (
            not isinstance(category, str)
            or not category.strip()
            or len(category) > 64
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 4096
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise MemoryConfigurationError("记忆候选参数值无效")
        return self._store.create_candidate(
            category.strip(),
            content.strip(),
            source_turn_id,
            float(confidence),
        )

    def discard(self, memory_id: str) -> bool:
        """软删除已创建但所属轮次随后失效的候选"""

        return self._store.delete(memory_id)
