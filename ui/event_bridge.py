"""把 Runtime 事件安全投递到 Qt 主线程"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QObject, Signal

from core.event_bus import EventBus, Subscription


class QtEventBridge(QObject):
    """使用 Qt queued signal 跨线程转发不可变事件"""

    event_received = Signal(object)

    def __init__(
        self,
        event_bus: EventBus,
        event_types: Sequence[type[Any]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._subscriptions: list[Subscription] = []
        for event_type in event_types:
            self._subscriptions.append(
                event_bus.subscribe(event_type, self.event_received.emit)
            )

    def close(self) -> None:
        for subscription in self._subscriptions:
            subscription.close()
        self._subscriptions.clear()
