"""仅在当前进程内保留最近完整对话轮次"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from threading import RLock
from uuid import UUID, uuid4


class SessionContext:
    """按轮次和字符预算构造短期会话历史"""

    def __init__(self, *, max_turns: int = 8, max_chars: int = 4000) -> None:
        if max_turns <= 0 or max_chars <= 0:
            raise ValueError("会话上下文限制必须为正数")
        self._turns: deque[tuple[str, str]] = deque(maxlen=max_turns)
        self._max_chars = max_chars
        self._lock = RLock()
        self._session_id = str(uuid4())

    @property
    def session_id(self) -> str:
        """返回语音和文字入口共同使用的当前会话标识"""

        with self._lock:
            return self._session_id

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        """忽略不完整内容并记录一个完整问答轮次"""

        user = user_text.strip()
        assistant = assistant_text.strip()
        if not user or not assistant:
            return
        with self._lock:
            self._turns.append((user, assistant))

    def build_history(self) -> tuple[Mapping[str, object], ...]:
        """优先返回最近且未超出字符预算的完整轮次"""

        selected: list[tuple[str, str]] = []
        used_chars = 0
        with self._lock:
            for user, assistant in reversed(self._turns):
                turn_chars = len(user) + len(assistant)
                if used_chars + turn_chars > self._max_chars:
                    break
                selected.append((user, assistant))
                used_chars += turn_chars

        history: list[Mapping[str, object]] = []
        for user, assistant in reversed(selected):
            history.extend(
                (
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": assistant},
                )
            )
        return tuple(history)

    def clear(self) -> None:
        """创建新会话并清空当前进程中的短期历史"""

        with self._lock:
            self._turns.clear()
            self._session_id = str(uuid4())

    def discard(self, session_id: str) -> None:
        """只持有单个会话的旧装配没有别的会话状态需要释放"""

    def reset(self) -> None:
        """与 clear 等价，供按会话管理状态的实现统一起见"""

        self.clear()

    def activate(
        self,
        session_id: str,
        turns: tuple[tuple[str, str], ...],
    ) -> None:
        """切换到已有会话并恢复其最近上下文"""

        try:
            UUID(session_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("会话 ID 必须是 UUID") from error
        normalized: list[tuple[str, str]] = []
        for user_text, assistant_text in turns:
            user = user_text.strip()
            assistant = assistant_text.strip()
            if user and assistant:
                normalized.append((user, assistant))
        with self._lock:
            self._session_id = session_id
            self._turns.clear()
            self._turns.extend(normalized[-self._turns.maxlen :])
