import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from core.event_bus import EventBus
from core.events import CorrelationId, TextDelta, TurnId
from ui.event_bridge import QtEventBridge


def app():
    return QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.001)
    assert predicate()


def test_event_bridge_queues_runtime_events_from_background_thread():
    app()
    bus = EventBus()
    bridge = QtEventBridge(bus, (TextDelta,))
    received = []
    threads = []

    def receive(event):
        received.append(event)
        threads.append(threading.get_ident())

    bridge.event_received.connect(receive)
    event = TextDelta(TurnId.new(), CorrelationId.new(), "你好")
    worker = threading.Thread(target=lambda: __import__("asyncio").run(bus.publish(event)))
    worker.start()
    worker.join()
    spin_until(lambda: received == [event])

    assert threads == [threading.get_ident()]
    bridge.close()
    __import__("asyncio").run(bus.publish(event))
    QCoreApplication.processEvents()
    assert received == [event]
