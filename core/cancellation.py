"""长时间运行适配器使用的协作取消机制"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class CancelledError(Exception):
    """操作检测到协作取消时抛出的专用异常"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"操作已取消: {reason}")
        self.reason = reason


@dataclass(slots=True)
class _CancellationState:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None


class CancellationToken:
    """只读取消状态视图"""

    def __init__(self, state: _CancellationState) -> None:
        self._state = state

    @property
    def is_cancelled(self) -> bool:
        return self._state.event.is_set()

    @property
    def reason(self) -> str | None:
        return self._state.reason

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled:
            # 事件只会在写入原因后置位
            assert self._state.reason is not None
            raise CancelledError(self._state.reason)

    async def wait(self) -> str:
        await self._state.event.wait()
        # 等待完成时取消原因一定可见
        assert self._state.reason is not None
        return self._state.reason


class CancellationSource:
    """拥有取消写权限并向适配器暴露只读令牌"""

    def __init__(self) -> None:
        self._state = _CancellationState()
        self._token = CancellationToken(self._state)

    @property
    def token(self) -> CancellationToken:
        return self._token

    def cancel(self, reason: str) -> None:
        if self._state.event.is_set():
            return
        # 先保存原因再唤醒等待者
        self._state.reason = reason
        self._state.event.set()
