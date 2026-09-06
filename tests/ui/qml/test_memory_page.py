from concurrent.futures import Future
from uuid import uuid4

import pytest
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtTest import QTest
from test_qml_application import build_controller, completed

from core.memory import MemoryStore
from core.memory_data import MemoryDataManager


def visual_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_item(child, name)
        if found is not None:
            return found
    return None


def click(window, item):
    assert item is not None
    assert item.isVisible()
    assert item.isEnabled()
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    assert 0 <= point.x() <= window.width()
    assert 0 <= point.y() <= window.height()
    QTest.mouseClick(window, Qt.LeftButton, pos=point.toPoint())
    QTest.qWait(20)


@pytest.fixture
def memory_window(qapp, tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    manager = MemoryDataManager(store)
    controller, runtime, _tray, qml, shell, _chat, dialogs, *_ = build_controller(qapp)
    runtime.list_memories = lambda: completed(manager.list_records())
    runtime.confirm_memory = lambda memory_id: completed(manager.confirm(memory_id))
    runtime.resolve_memory_conflict = lambda memory_id: completed(
        manager.resolve_conflict(memory_id)
    )
    runtime.delete_memory = lambda memory_id: completed(manager.delete(memory_id))
    controller.start()
    window = qml.root_objects[0]
    window.setWidth(820)
    window.setHeight(560)
    shell.show_main("chat")
    qapp.processEvents()
    yield window, shell, dialogs, runtime, store
    controller.close()
    store.close()


def test_navigation_loads_memory_and_refresh_button_reloads(memory_window, qapp):
    window, shell, _dialogs, _runtime, store = memory_window
    record = store.create_candidate("称呼", "希望叫小蓝", str(uuid4()), 0.9)

    shell.navigate("memories")
    QTest.qWait(30)
    page = window.findChild(QObject, "memoryPage")
    memories = page.property("memories")
    assert memories.memoryModel.rowCount() == 1
    assert visual_item(window.contentItem(), "memoryCard-" + record.id) is not None

    store.create_candidate("偏好", "喜欢晴天", str(uuid4()), 0.9)
    click(window, visual_item(window.contentItem(), "memoryRefreshButton"))
    assert memories.memoryModel.rowCount() == 2
    shell.navigate("chat")
    store.create_candidate("爱好", "喜欢散步", str(uuid4()), 0.9)
    shell.navigate("memories")
    qapp.processEvents()
    assert memories.memoryModel.rowCount() == 3


@pytest.mark.parametrize("conflicted", [False, True])
def test_selected_memory_actions_work_at_minimum_window_size(memory_window, conflicted):
    window, shell, dialogs, _runtime, store = memory_window
    if conflicted:
        previous = store.create_candidate(
            "称呼", "旧称呼", str(uuid4()), 0.9, fact_key="user.name", value="旧称呼"
        )
        store.confirm(previous.id)
    record = store.create_candidate(
        "称呼",
        "希望叫小蓝",
        str(uuid4()),
        0.9,
        fact_key="user.name" if conflicted else None,
        value="小蓝" if conflicted else None,
    )
    if conflicted:
        store.confirm(record.id)
    shell.navigate("memories")
    QTest.qWait(30)
    card = visual_item(window.contentItem(), "memoryCard-" + record.id)
    click(window, card)
    detail = visual_item(window.contentItem(), "memoryDetails-" + record.id)
    assert detail is not None and detail.isVisible()
    assert record.source_turn_id in detail.property("text")
    action = "memoryResolveButton-" if conflicted else "memoryConfirmButton-"
    click(window, visual_item(window.contentItem(), action + record.id))
    if conflicted:
        assert "替换" in dialogs.currentConfirmation["summary"]
        dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], True)
        QTest.qWait(30)
    assert store.get(record.id).status.value == "confirmed"
    button = visual_item(window.contentItem(), action + record.id)
    assert button is None or not button.isVisible() or not button.isEnabled()

    click(window, visual_item(window.contentItem(), "memoryDeleteButton-" + record.id))
    assert dialogs.currentConfirmation["title"] == "删除这条记忆？"
    assert (
        window.findChild(QObject, "mainConfirmationSheet").property("visible") is True
    )
    assert store.get(record.id).status.value == "confirmed"
    dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], False)
    QTest.qWait(30)
    assert store.get(record.id).status.value == "confirmed"
    click(window, visual_item(window.contentItem(), "memoryDeleteButton-" + record.id))
    dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], True)
    QTest.qWait(30)
    assert store.get(record.id).status.value == "deleted"
    assert (
        window.findChild(QObject, "memoryPage").property("memories").selectedMemory
        == {}
    )


def test_memory_loading_error_and_empty_states_are_visible(memory_window):
    window, shell, _dialogs, runtime, _store = memory_window
    pending = Future()
    runtime.list_memories = lambda: pending
    shell.navigate("memories")
    QTest.qWait(20)
    status = visual_item(window.contentItem(), "memoryStatus")
    assert status is not None and status.isVisible()
    assert "加载" in status.property("text")
    pending.set_exception(RuntimeError("读取失败"))
    QTest.qWait(20)
    assert "失败" in status.property("text")
    runtime.list_memories = lambda: completed(())
    click(window, visual_item(window.contentItem(), "memoryRefreshButton"))
    assert "暂无" in status.property("text")
