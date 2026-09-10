"""当天话题与未完成事项摘要的共享数据结构"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShortTermSummaryDraft:
    """尚未持久化且等待最终回复完成的结构化摘要"""

    topic: str
    unfinished_items: tuple[str, ...]
    decisions: tuple[str, ...] = ()
