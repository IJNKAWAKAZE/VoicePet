"""为设置界面提供会话数据管理边界"""

from __future__ import annotations

from dataclasses import replace

from .session_archive import (
    SessionArchiveStore,
    SessionRecord,
    SessionTurnRecord,
    ShortTermSummaryRecord,
)
from .session_context import SessionContext


class SessionDataManager:
    """管理持久化会话并同步清理进程内上下文"""

    def __init__(
        self,
        archive: SessionArchiveStore,
        context: SessionContext,
    ) -> None:
        self._archive = archive
        self._context = context

    @property
    def current_session_id(self) -> str:
        """返回语音和文字共同使用的当前会话"""

        return self._context.session_id

    def list_records(self) -> tuple[SessionRecord, ...]:
        """按最新优先返回会话摘要并标记当前会话"""

        current_id = self._context.session_id
        return tuple(
            replace(record, is_active=record.id == current_id)
            for record in reversed(self._archive.list_sessions())
        )

    def list_turns(self, session_id: str) -> tuple[SessionTurnRecord, ...]:
        """返回选定会话的完整多轮记录"""

        return self._archive.list_session_turns(session_id)

    def list_summaries(
        self,
        session_id: str | None = None,
    ) -> tuple[ShortTermSummaryRecord, ...]:
        """返回全部或指定会话的有效短期摘要"""

        return self._archive.list_summaries(session_id)

    def delete_summary(self, summary_id: str) -> bool:
        """删除一个短期摘要并阻止同来源重建"""

        return self._archive.delete_summary(summary_id)

    def source_label(self, turn_id: str) -> dict[str, object]:
        """返回摘要来源的最小元数据标签"""

        return self._archive.source_label(turn_id)

    def activate(self, session_id: str) -> tuple[SessionTurnRecord, ...]:
        """切换当前会话并恢复可用的最近上下文"""

        turns = self._archive.list_session_turns(session_id)
        if not turns and all(
            record.id != session_id for record in self._archive.list_sessions()
        ):
            raise ValueError("会话不存在")
        self._context.activate(
            session_id,
            tuple((turn.user_text, turn.assistant_text) for turn in turns),
        )
        return turns

    def resume_latest(self) -> tuple[SessionTurnRecord, ...]:
        """启动时恢复最近一次持久化会话"""

        sessions = self._archive.list_sessions()
        if not sessions:
            return ()
        return self.activate(sessions[-1].id)

    def new(self) -> str:
        """创建新的空白当前会话"""

        self._context.clear()
        return self._context.session_id

    def delete(self, session_id: str) -> bool:
        """删除整个会话并在必要时创建空白当前会话"""

        deleted = self._archive.discard_session(session_id)
        if deleted and session_id == self._context.session_id:
            self._context.clear()
        return deleted

    def clear(self) -> int:
        """删除全部会话并清空当前上下文"""

        affected = self._archive.clear_all()
        self._context.clear()
        return affected
