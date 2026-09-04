"""验证模型生成的当天话题与未完成事项摘要"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .llm import ToolDefinition
from .memory import MemoryPolicy
from .session_archive import SessionArchiveStore, ShortTermSummaryRecord

SHORT_TERM_SUMMARY_TOOL_NAME = "update_daily_summary"


@dataclass(frozen=True, slots=True)
class ShortTermSummaryDraft:
    """尚未持久化且等待最终回复完成的结构化摘要"""

    topic: str
    unfinished_items: tuple[str, ...]


def short_term_summary_tool_definition() -> ToolDefinition:
    """返回不会进入 Tool Worker 的严格短期摘要工具定义"""

    return ToolDefinition(
        SHORT_TERM_SUMMARY_TOOL_NAME,
        "更新当前轮次的当天话题和未完成事项，话题应简短且不得包含密码、令牌、验证码、证件或支付信息",
        {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "maxLength": 500},
                "unfinished_items": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "maxLength": 200},
                },
            },
            "required": ["topic", "unfinished_items"],
            "additionalProperties": False,
        },
    )


class ShortTermSummaryService:
    """先验证模型摘要并在轮次完成后写入会话存储"""

    def __init__(
        self,
        archive: SessionArchiveStore,
        *,
        policy: MemoryPolicy | None = None,
    ) -> None:
        self._archive = archive
        self._policy = policy or MemoryPolicy()

    def prepare(self, arguments: Mapping[str, object]) -> ShortTermSummaryDraft:
        """把模型参数转换为有界不可变摘要草稿"""

        if set(arguments) != {"topic", "unfinished_items"}:
            raise ValueError("短期摘要参数字段无效")
        topic = arguments["topic"]
        raw_items = arguments["unfinished_items"]
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 500:
            raise ValueError("短期摘要话题无效")
        if not isinstance(raw_items, Sequence) or isinstance(
            raw_items,
            (str, bytes, bytearray),
        ):
            raise TypeError("短期摘要未完成事项无效")
        if len(raw_items) > 10 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200
            for item in raw_items
        ):
            raise ValueError("短期摘要未完成事项无效")
        if self._policy.is_prohibited(topic) or any(
            self._policy.is_prohibited(item) for item in raw_items
        ):
            raise ValueError("短期摘要包含禁止保存的敏感信息")
        return ShortTermSummaryDraft(
            topic.strip(),
            tuple(item.strip() for item in raw_items),
        )

    def save(
        self,
        source_turn_id: str,
        draft: ShortTermSummaryDraft,
    ) -> ShortTermSummaryRecord:
        """把已验证草稿绑定到已归档的完成轮次"""

        if not isinstance(draft, ShortTermSummaryDraft):
            raise TypeError("短期摘要草稿无效")
        return self._archive.save_summary(
            source_turn_id,
            draft.topic,
            draft.unfinished_items,
        )
