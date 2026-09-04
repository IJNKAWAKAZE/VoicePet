"""按事件类型进行进程内发布和订阅"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

EventT = TypeVar("EventT")
Callback = Callable[[Any], object | Awaitable[object]]
ErrorHook = Callable[
    [object, Callback, Exception], object | Awaitable[object]
]


class Subscription:
    """可重复关闭的订阅句柄"""

    def __init__(self, close_callback: Callable[[], None]) -> None:
        self._close_callback = close_callback
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_callback()


class EventBus:
    """按注册顺序投递精确类型匹配的事件"""

    def __init__(self, on_callback_error: ErrorHook | None = None) -> None:
        self._subscribers: dict[type[object], list[Callback]] = {}
        self._on_callback_error = on_callback_error

    def subscribe(
        self,
        event_type: type[EventT],
        callback: Callable[[EventT], object | Awaitable[object]],
    ) -> Subscription:
        callbacks = self._subscribers.setdefault(event_type, [])
        callbacks.append(callback)

        def unsubscribe() -> None:
            current = self._subscribers.get(event_type)
            if current is None:
                return
            try:
                current.remove(callback)
            except ValueError:
                return
            if not current:
                self._subscribers.pop(event_type, None)

        return Subscription(unsubscribe)

    async def publish(self, event: object) -> None:
        failures: list[Exception] = []
        # 使用快照避免投递期间的退订改变当前事件接收者
        callbacks = tuple(self._subscribers.get(type(event), ()))
        for callback in callbacks:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:  # noqa: BLE001 订阅者边界必须隔离普通异常
                if self._on_callback_error is None:
                    failures.append(error)
                    continue
                try:
                    hook_result = self._on_callback_error(event, callback, error)
                    if inspect.isawaitable(hook_result):
                        await hook_result
                except Exception as hook_error:  # noqa: BLE001 错误钩子同样属于外部边界
                    failures.append(hook_error)

        if failures:
            raise ExceptionGroup("事件回调执行失败", failures)
