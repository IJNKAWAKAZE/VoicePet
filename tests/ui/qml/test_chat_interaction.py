from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from core.config import AppConfig, UiConfig
from ui.pet_animation_model import PetAnimationModel, PetInteractionController
from ui.qml_resources import qml_root
from ui.qml_runtime import QmlRuntime
from ui.viewmodels.app_shell import AppShellViewModel
from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.diagnostics import DiagnosticsViewModel
from ui.viewmodels.dialogs import DialogCoordinator
from ui.viewmodels.memories import MemoryViewModel
from ui.viewmodels.pets import PetViewModel
from ui.viewmodels.settings import SettingsViewModel
from ui.viewmodels.theme import ThemeViewModel


def completed(value):
    future = Future()
    future.set_result(value)
    return future


class HistoryRuntime:
    def list_sessions(self):
        return completed(({
            "id": "history-1", "title": "昨天的旅行计划", "turn_count": 1,
            "is_active": False,
        },))

    def activate_session(self, session_id):
        assert session_id == "history-1"
        return completed(({
            "id": "turn-1", "user_text": "周末去哪里玩？",
            "assistant_text": "可以去海边散步，也可以看一场电影。",
        },))

    def __getattr__(self, name):
        return lambda *args: completed(())


@pytest.fixture
def history_window(qapp):
    shell = AppShellViewModel()
    service = HistoryRuntime()
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    dialogs = DialogCoordinator()
    settings = SettingsViewModel(AppConfig(), type("Store", (), {"save": lambda *args: None})())
    chat = ChatViewModel(service)
    runtime = QmlRuntime(qapp, {
        "appShell": shell,
        "themeViewModel": theme,
        "dialogCoordinator": dialogs,
        "chatViewModel": chat,
        "memoryViewModel": MemoryViewModel(service, dialogs),
        "settingsViewModel": settings,
        "petViewModel": PetViewModel(service, settings, lambda: ()),
        "diagnosticsViewModel": DiagnosticsViewModel(service),
        "petAnimation": PetAnimationModel(Path("assets/pet/dpsk-girl").resolve()),
        "petInteraction": PetInteractionController(),
    }, qml_root() / "Main.qml")
    runtime.load()
    root = runtime.root_objects[0]
    root.show()
    chat.refresh_sessions()
    QTest.qWait(100)
    yield root, chat, shell, theme
    runtime.close()


def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)


def test_click_history_renders_saved_messages(history_window, qapp):
    root, chat, _, _ = history_window
    session_list = root.findChild(QQuickItem, "sessionList")
    button = next(item for item in visual_items(session_list)
                  if item.property("sessionId") == "history-1")
    position = button.mapToScene(QPoint(20, 20)).toPoint()
    QTest.mouseClick(root, Qt.LeftButton, pos=position)
    QTest.qWait(100)

    assert chat.activeSessionId == "history-1"
    assert chat.messageModel.rowCount() == 2
    # 检查实际气泡尺寸和排列，不能只检查模型条数
    page = root.findChild(QQuickItem, "chatPage")
    message_list = next(item for item in visual_items(page)
                        if item.objectName() == "chatMessageList"
                        and item.property("count") == 2)
    assert message_list.property("count") == 2
    rows = [item for item in visual_items(message_list)
            if item.property("role") in ("user", "assistant")
            and item.parentItem() == message_list.property("contentItem")]
    assert len(rows) == 2
    assert all(row.height() >= 38 for row in rows)
    assert rows[1].y() >= rows[0].y() + rows[0].height() + 10
    bubbles = [item for item in visual_items(message_list)
               if item.metaObject().className().startswith("MessageBubble")]
    assert [bubble.property("markdown") for bubble in bubbles] == [
        "周末去哪里玩？", "可以去海边散步，也可以看一场电影。",
    ]
    assert all(bubble.isVisible() and bubble.height() > 24 for bubble in bubbles)


@pytest.mark.parametrize("section", ["settings", "memories"])
def test_non_chat_pages_release_history_sidebar_space(history_window, section, qapp):
    root, _, shell, _ = history_window
    shell.navigate(section)
    QTest.qWait(30)

    sidebar = root.findChild(QObject, "contextSidebar")
    navigation = root.findChild(QObject, "navigationRail")
    content = root.findChild(QObject, "mainContent")
    assert sidebar.property("visible") is False
    assert content.property("width") == root.width() - navigation.property("width") - 20
    assert content.mapToScene(QPoint(0, 0)).x() + content.width() <= root.width() - 10


def test_loaded_history_scrolls_past_last_user_to_assistant_bottom(history_window):
    root, chat, _, _ = history_window
    chat._runtime.activate_session = lambda session_id: completed(tuple({
        "id": str(index), "user_text": "问题 " + str(index),
        "assistant_text": "回答 " + str(index) + "：" + "这是一段需要多行排版的回复。" * 12,
    } for index in range(15)))
    chat.activate_session("history-1")
    QTest.qWait(120)
    page = root.findChild(QQuickItem, "chatPage")
    message_list = next(item for item in visual_items(page)
                        if item.objectName() == "chatMessageList")
    assert message_list.property("count") == 30
    assert message_list.property("atYEnd"), {name: message_list.property(name) for name in (
        "followTail", "contentY", "contentHeight", "originY", "height", "moving", "flicking",
    )}
    # 最后一条必须是助手回复，且底部实际落在消息视口内
    final_row = next(item for item in visual_items(message_list)
                     if item.property("role") == "assistant"
                     and item.parentItem() == message_list.property("contentItem")
                     and str(item.property("markdown")).startswith("回答 14："))
    assert final_row.y() + final_row.height() <= message_list.property("contentY") + message_list.height() + 2


def test_streaming_reply_follows_tail_after_wrapping(history_window):
    root, chat, _, _ = history_window
    chat.append_user_message("请详细回答")
    chat.append_assistant_delta("分段回复。" * 200)
    QTest.qWait(100)
    messages = root.findChild(QQuickItem, "chatMessageList")
    assert messages.property("atYEnd")


def test_empty_streaming_placeholder_does_not_render_a_blank_bubble(history_window):
    root, chat, _, _ = history_window
    chat.submit("只测试占位消息")
    QTest.qWait(80)

    messages = root.findChild(QQuickItem, "chatMessageList")
    assert chat.messageModel.rowCount() == 2
    rows = [
        item for item in visual_items(messages)
        if item.parentItem() == messages.property("contentItem")
        and item.property("role") in ("user", "assistant")
    ]
    assert len(rows) == 2
    assert rows[0].height() > 0
    assert rows[1].height() == 0
    chat.append_assistant_delta("还有一段更长的补充。" * 150)
    QTest.qWait(100)
    assert messages.property("atYEnd")


def test_streaming_does_not_pull_reader_away_from_older_messages(history_window, qapp):
    root, chat, _, _ = history_window
    chat.append_user_message("请详细回答")
    chat.append_assistant_delta("分段回复。" * 300)
    QTest.qWait(100)
    messages = root.findChild(QQuickItem, "chatMessageList")
    position = messages.mapToScene(QPointF(200, 200))
    wheel = QWheelEvent(position, root.mapToGlobal(position.toPoint()), QPoint(), QPoint(0, 120),
                        Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    qapp.sendEvent(root, wheel)
    QTest.qWait(200)
    assert not messages.property("atYEnd")
    before = messages.property("contentY")
    chat.append_assistant_delta("还有一些补充。" * 100)
    QTest.qWait(100)
    assert abs(messages.property("contentY") - before) <= 2
