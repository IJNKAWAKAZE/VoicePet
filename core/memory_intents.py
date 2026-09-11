"""用户主动记忆命令的严格解析、计划与执行"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Protocol

from .memory import MemoryRecord, MemoryStatus, MemoryStore
from .memory_facts import infer_explicit_fact, is_explicit_correction


class SessionArchiveData(Protocol):
    """清理当天会话层所需的最小边界"""

    def clear_date(self, target: date) -> int: ...


class MemoryIntentAction(str, Enum):
    """用户可明确触发的记忆数据动作"""

    REMEMBER = "remember"
    FORGET = "forget"
    LIST = "list"
    CLEAR_TODAY = "clear_today"


@dataclass(frozen=True, slots=True)
class MemoryIntent:
    """从有限中文表达中解析出的记忆意图"""

    action: MemoryIntentAction
    content: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryOperation:
    """绑定来源轮次且等待上层确认的记忆操作计划"""

    action: MemoryIntentAction
    content: str | None
    source_turn_id: str
    summary: str
    requires_confirmation: bool


@dataclass(frozen=True, slots=True)
class MemoryOperationResult:
    """记忆操作返回的结构化安全结果"""

    action: MemoryIntentAction
    affected: int
    records: tuple[MemoryRecord, ...]
    safe_message: str


class MemoryIntentParser:
    """只识别明确记忆动词且拒绝模糊普通对话"""

    # 用户常用“记得这个喜好”“记下……”表达保存意图；这些属于明确记忆请求。
    _REMEMBER = re.compile(r"^(?:请)?(?:记住|记得|记下)(?:一下|这个|这点)?[：:，,\s]*(.+)$")
    _FORGET = re.compile(r"^(?:请)?(?:忘掉|忘记)[：:，,\s]*(.+)$")
    _LIST = frozenset({"你记得什么", "查看记忆", "列出记忆"})
    _CLEAR = frozenset({"清空今天对话", "清除今天对话"})

    def parse(self, text: str) -> MemoryIntent | None:
        normalized = text.strip()
        if normalized in self._LIST:
            return MemoryIntent(MemoryIntentAction.LIST)
        if normalized in self._CLEAR:
            return MemoryIntent(MemoryIntentAction.CLEAR_TODAY)
        for pattern, action in (
            (self._REMEMBER, MemoryIntentAction.REMEMBER),
            (self._FORGET, MemoryIntentAction.FORGET),
        ):
            match = pattern.fullmatch(normalized)
            if match is None:
                continue
            content = match.group(1).strip()
            # “记得这个喜好：……”中的说明性前缀不属于记忆内容。
            if action is MemoryIntentAction.REMEMBER and "：" in content:
                content = content.split("：", 1)[1].strip()
            elif action is MemoryIntentAction.REMEMBER and ":" in content:
                content = content.split(":", 1)[1].strip()
            if 0 < len(content) <= 4096:
                return MemoryIntent(action, content)
        return None


class MemoryOperationService:
    """把明确意图转换为可确认计划并操作 MemoryStore"""

    def __init__(
        self,
        store: MemoryStore,
        *,
        parser: MemoryIntentParser | None = None,
        session_archive: SessionArchiveData | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._store = store
        self._parser = parser or MemoryIntentParser()
        self._session_archive = session_archive
        self._today = today

    def plan(self, text: str, source_turn_id: str) -> MemoryOperation | None:
        intent = self._parser.parse(text)
        if intent is None:
            return None
        summaries = {
            MemoryIntentAction.REMEMBER: f"保存记忆：{intent.content}",
            MemoryIntentAction.FORGET: f"删除匹配记忆：{intent.content}",
            MemoryIntentAction.LIST: "查看已保存的本地记忆",
            MemoryIntentAction.CLEAR_TODAY: "清空今天的本地会话与短期摘要",
        }
        return MemoryOperation(
            intent.action,
            intent.content,
            source_turn_id,
            summaries[intent.action],
            intent.action is not MemoryIntentAction.LIST,
        )

    def execute(self, operation: MemoryOperation) -> MemoryOperationResult:
        if not isinstance(operation, MemoryOperation):
            raise TypeError("记忆操作计划无效")
        if operation.action is MemoryIntentAction.REMEMBER:
            assert operation.content is not None
            inferred = infer_explicit_fact(operation.content)
            fact_options = (
                {}
                if inferred is None
                else {
                    "fact_key": inferred[0],
                    "value": inferred[1],
                    "explicit_update": is_explicit_correction(operation.content),
                }
            )
            record = self._store.create_confirmed(
                category="user_requested",
                content=operation.content,
                source_turn_id=operation.source_turn_id,
                **fact_options,
            )
            return MemoryOperationResult(
                operation.action,
                1,
                (record,),
                "记忆已保存",
            )
        if operation.action is MemoryIntentAction.LIST:
            records = self._store.list_all()
            return MemoryOperationResult(
                operation.action,
                len(records),
                records,
                f"共找到 {len(records)} 条记忆",
            )
        if operation.action is MemoryIntentAction.CLEAR_TODAY:
            affected = (
                0
                if self._session_archive is None
                else self._session_archive.clear_date(self._today())
            )
            return MemoryOperationResult(
                operation.action,
                affected,
                (),
                f"已清空今天的 {affected} 条会话记录",
            )
        assert operation.action is MemoryIntentAction.FORGET
        assert operation.content is not None
        records = self._store.search(
            operation.content,
            statuses=frozenset(
                {
                    MemoryStatus.CANDIDATE,
                    MemoryStatus.CONFIRMED,
                    MemoryStatus.CONFLICTED,
                }
            ),
            limit=100,
        )
        affected = sum(self._store.delete(record.id) for record in records)
        return MemoryOperationResult(
            operation.action,
            affected,
            (),
            f"已删除 {affected} 条匹配记忆",
        )
