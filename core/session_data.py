"""为设置界面提供会话数据管理边界"""

from __future__ import annotations

from dataclasses import replace

from .agent_store import AgentStore
from .codex_session_files import CodexSessionFiles
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
        *,
        agent_store: AgentStore | None = None,
        codex_files: CodexSessionFiles | None = None,
    ) -> None:
        self._archive = archive
        self._context = context
        self._agent_store = agent_store
        self._codex_files = codex_files

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
        if not turns and not self._session_exists(session_id):
            raise ValueError("会话不存在")
        self._context.activate(
            session_id,
            tuple((turn.user_text, turn.assistant_text) for turn in turns),
        )
        return turns

    def _session_exists(self, session_id: str) -> bool:
        """归档里还没有轮次的会话可能是刚新建或正在执行的，仍然要能切回"""

        if session_id == self._context.session_id:
            return True
        known = getattr(self._context, "session_ids", None)
        if callable(known) and session_id in known():
            return True
        return any(
            record.id == session_id for record in self._archive.list_sessions()
        )


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

        self._delete_codex_thread(session_id)
        deleted = self._archive.discard_session(session_id)
        if self._agent_store is not None:
            self._agent_store.delete_session(session_id)
        if deleted:
            self._context.discard(session_id)
            if session_id == self._context.session_id:
                self._context.clear()
        return deleted

    def clear(self) -> int:
        """删除全部会话并清空当前上下文"""

        session_ids = self._agent_store.session_ids() if self._agent_store is not None else ()
        for session_id in session_ids:
            self._delete_codex_thread(session_id)
        affected = self._archive.clear_all()
        if self._agent_store is not None:
            for session_id in session_ids:
                self._agent_store.delete_session(session_id)
        self._context.reset()
        return affected

    def _delete_codex_thread(self, session_id: str) -> None:
        if self._agent_store is None or self._codex_files is None:
            return
        thread_id = self._agent_store.thread_id(session_id)
        if not thread_id:
            return
        try:
            self._codex_files.delete_thread(thread_id)
        except OSError:
            # 其它会话仍在执行时线程文件可能被占用；残留文件只是可重建的缓存
            pass
