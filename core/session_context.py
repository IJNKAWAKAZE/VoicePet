"""仅在当前进程内保留最近完整对话轮次"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping


class SessionContext:
    """按轮次和字符预算构造短期会话历史"""

    def __init__(self, *, max_turns: int = 8, max_chars: int = 4000) -> None:
        if max_turns <= 0 or max_chars <= 0:
            raise ValueError("会话上下文限制必须为正数")
        self._turns: deque[tuple[str, str]] = deque(maxlen=max_turns)
        self._max_chars = max_chars

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        """忽略不完整内容并记录一个完整问答轮次"""

        user = user_text.strip()
        assistant = assistant_text.strip()
        if not user or not assistant:
            return
        self._turns.append((user, assistant))

    def build_history(self) -> tuple[Mapping[str, object], ...]:
        """优先返回最近且未超出字符预算的完整轮次"""

        selected: list[tuple[str, str]] = []
        used_chars = 0
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
        """清空当前进程中的短期会话历史"""

        self._turns.clear()
