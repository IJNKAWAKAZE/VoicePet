from concurrent.futures import Future
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest

from core.config import UiConfig
from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext
from core.session_data import SessionDataManager
from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.dialogs import DialogCoordinator
from ui.viewmodels.theme import ThemeViewModel


def completed(value):
    future = Future()
    future.set_result(value)
    return future


class ArchiveRuntime:
    def __init__(self, archive):
        self.archive = archive
        self.sessions = SessionDataManager(archive, SessionContext())

    def list_sessions(self):
        return completed(self.sessions.list_records())

    def current_session_id(self):
        return completed(self.sessions.current_session_id)

    def list_session_turns(self, session_id):
        return completed(self.sessions.list_turns(session_id))

    def activate_session(self, session_id):
        return completed(self.sessions.activate(session_id))

    def new_session(self):
        return completed(self.sessions.new())

    def submit_text(self, text):
        # Runtime 接受请求不等于已完成归档
        self.pending_text = text
        return completed(str(uuid4()))

    def archive_reply(self):
        self.archive.archive_turn(
            str(uuid4()), self.pending_text, "新的回答", self.sessions.current_session_id,
        )

    def delete_session(self, session_id):
        return completed(self.sessions.delete(session_id))

    def clear_sessions(self):
        return completed(self.sessions.clear())


@pytest.fixture
def archived_chat(tmp_path, qapp):
    archive = SessionArchiveStore(tmp_path / "sessions.db")
    runtime = ArchiveRuntime(archive)
    old_id = runtime.sessions.current_session_id
    archive.archive_turn(str(uuid4()), "旧问题", "旧回答", old_id)
    chat = ChatViewModel(runtime)
    yield chat, runtime, old_id
    archive.close()


def model_rows(model):
    return [{name.decode(): model.data(model.index(row), role)
             for role, name in model.roleNames().items()}
            for row in range(model.rowCount())]


def find_item_by_name(item, name):
    """QML 对象不在 QObject 子树上，只能沿可视子项查找"""

    for child in item.childItems():
        if child.objectName() == name:
            return child
        found = find_item_by_name(child, name)
        if found is not None:
            return found
    return None


def test_startup_restores_current_history_without_activating_another_session(archived_chat):
    chat, runtime, old_id = archived_chat
    chat.restore_current_session()
    assert chat.activeSessionId == old_id
    assert [row["markdown"] for row in model_rows(chat.messageModel)] == ["旧问题", "旧回答"]
    assert runtime.sessions.current_session_id == old_id


def test_new_session_stays_visible_then_refreshes_after_archive(archived_chat):
    chat, runtime, old_id = archived_chat
    chat.refresh_sessions()
    chat.new_session()
    new_id = runtime.sessions.current_session_id
    rows = model_rows(chat.sessionModel)
    assert {row["sessionId"] for row in rows} == {old_id, new_id}
    assert [row["sessionId"] for row in rows if row["active"]] == [new_id]
    chat.submit("新问题")
    runtime.archive_reply()
    chat.finish_assistant()
    rows = model_rows(chat.sessionModel)
    current = next(row for row in rows if row["sessionId"] == new_id)
    assert current["turnCount"] == 1
    assert current["title"] == "新问题"
    chat.activate_session(old_id)
    assert runtime.sessions.current_session_id == old_id
    assert [row["sessionId"] for row in model_rows(chat.sessionModel) if row["active"]] == [old_id]
    assert chat.messageModel.data(chat.messageModel.index(0), Qt.UserRole + 3) == "旧问题"


def test_failed_switch_retains_current_messages_and_selection(archived_chat):
    chat, _, old_id = archived_chat
    chat.activate_session(old_id)
    previous = model_rows(chat.messageModel)
    errors = QSignalSpy(chat.errorOccurred)
    chat.activate_session(str(uuid4()))
    assert errors.count() == 1
    assert chat.activeSessionId == old_id
    assert model_rows(chat.messageModel) == previous


def test_session_switch_and_new_keep_running_reply_alive(archived_chat):
    chat, runtime, old_id = archived_chat
    chat.activate_session(old_id)
    chat.submit("正在回答的问题")
    assert chat.processing is True

    chat.new_session()
    new_id = runtime.sessions.current_session_id
    assert new_id != old_id
    assert chat.activeSessionId == new_id
    assert chat.processing is False
    assert chat.messageModel.rowCount() == 0

    # 切回仍在运行的会话时，已流出的内容和运行状态都要保留
    chat.activate_session(old_id)
    assert chat.activeSessionId == old_id
    assert [row["markdown"] for row in model_rows(chat.messageModel)] == [
        "旧问题",
        "旧回答",
        "正在回答的问题",
        "",
    ]
    assert chat.processing is True
    assert [
        row["running"]
        for row in model_rows(chat.sessionModel)
        if row["sessionId"] == old_id
    ] == [True]


def test_pending_switch_keeps_selection_until_it_succeeds(archived_chat):
    chat, runtime, old_id = archived_chat
    chat.activate_session(old_id)
    pending = Future()
    runtime.activate_session = lambda session_id: pending
    chat.activate_session("next")
    assert chat.activeSessionId == old_id
    assert chat.sessionLoading is True
    chat.new_session()
    pending.set_exception(ValueError("missing"))
    assert chat.sessionLoading is False
    assert chat.activeSessionId == old_id


def test_deleting_current_session_requires_confirmation_and_restores_empty_chat(archived_chat):
    _, runtime, old_id = archived_chat
    dialogs = DialogCoordinator()
    chat = ChatViewModel(runtime, dialogs=dialogs)
    chat.activate_session(old_id)
    chat.request_delete_session(old_id)
    assert runtime.sessions.list_turns(old_id)
    request = dialogs.currentConfirmation
    dialogs.resolve_confirmation(request["requestId"], False)
    assert runtime.sessions.list_turns(old_id)
    chat.request_delete_session(old_id)
    dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], True)
    assert not runtime.sessions.list_turns(old_id)
    assert chat.messageModel.rowCount() == 0
    assert chat.activeSessionId == runtime.sessions.current_session_id


def test_clear_sessions_can_be_cancelled_then_confirmed(archived_chat):
    _, runtime, old_id = archived_chat
    dialogs = DialogCoordinator()
    chat = ChatViewModel(runtime, dialogs=dialogs)
    chat.activate_session(old_id)
    chat.request_clear_sessions()
    dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], False)
    assert runtime.sessions.list_turns(old_id)
    chat.request_clear_sessions()
    dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], True)
    assert not runtime.sessions.list_records()
    assert chat.messageModel.rowCount() == 0
    assert not chat.sessionLoading


def test_delete_keeps_input_locked_until_current_history_is_restored(archived_chat):
    _, runtime, old_id = archived_chat
    dialogs = DialogCoordinator()
    chat = ChatViewModel(runtime, dialogs=dialogs)
    chat.activate_session(old_id)
    current = Future()
    turns = Future()
    runtime.current_session_id = lambda: current
    runtime.list_session_turns = lambda session_id: turns
    chat.request_delete_session(old_id)
    dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], True)
    assert chat.sessionLoading
    chat.submit("恢复历史期间不能发送")
    assert not hasattr(runtime, "pending_text")
    current.set_result(runtime.sessions.current_session_id)
    assert chat.sessionLoading
    turns.set_result(())
    assert not chat.sessionLoading
    assert chat.messageModel.rowCount() == 0


@pytest.mark.parametrize("fail_current", [True, False])
def test_history_restore_failure_releases_loading(archived_chat, fail_current):
    chat, runtime, _old_id = archived_chat
    pending = Future()
    if fail_current:
        runtime.current_session_id = lambda: pending
    else:
        runtime.list_session_turns = lambda session_id: pending
    chat.restore_current_session()
    assert chat.sessionLoading
    pending.set_exception(RuntimeError("unavailable"))
    assert not chat.sessionLoading


def test_session_delete_button_does_not_also_activate_row(archived_chat, qapp):
    chat, _runtime, old_id = archived_chat
    chat.refresh_sessions()
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    QQuickStyle.setStyle("Basic")
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(
        Path("ui/qml/components/SessionList.qml").resolve(),
    )))
    root = component.createWithInitialProperties({"theme": theme, "sessionModel": chat.sessionModel})
    window = QQuickWindow()
    try:
        assert root is not None, component.errors()
        root.setParentItem(window.contentItem())
        root.setWidth(208)
        root.setHeight(240)
        window.resize(208, 240)
        window.show()
        deleted = QSignalSpy(root.deleteRequested)
        activated = QSignalSpy(root.sessionRequested)
        QTest.qWait(30)
        QTest.mouseClick(window, Qt.LeftButton, pos=QPoint(180, 33))
        assert deleted.count() == 1
        assert deleted.at(0) == [old_id]
        assert activated.count() == 0
    finally:
        window.close()
        if root is not None:
            root.deleteLater()
        engine.deleteLater()
        qapp.processEvents()


def test_two_sessions_stream_in_parallel_without_overwriting_each_other(archived_chat):
    chat, runtime, first_id = archived_chat
    chat.refresh_sessions()
    chat.new_session()
    second_id = runtime.sessions.current_session_id
    assert second_id != first_id

    # 前台切到新会话后原会话转为后台，但仍继续累积自己的回复
    chat.begin_turn(first_id)
    chat.append_assistant_delta("后台第一段", "item-1", first_id)
    assert chat.processing is False
    assert chat.anyProcessing is True
    assert chat.messageModel.rowCount() == 0
    assert {row["sessionId"]: row["running"] for row in model_rows(chat.sessionModel)} == {
        first_id: True,
        second_id: False,
    }

    # 前台会话同时开自己的轮次，两条流式互不覆盖
    chat.begin_turn(second_id)
    chat.append_user_message("前台问题", second_id)
    chat.append_assistant_delta("前台回答", "item-2", second_id)
    assert chat.processing is True
    assert [item["markdown"] for item in chat.messageModel._items] == [
        "前台问题",
        "前台回答",
    ]

    chat.append_assistant_delta("后台第二段", "item-1", first_id)
    chat.finish_assistant(first_id)
    assert chat.processing is True
    assert chat.anyProcessing is True
    assert [
        row["running"]
        for row in model_rows(chat.sessionModel)
        if row["sessionId"] == first_id
    ] == [False]

    chat.finish_assistant(second_id)
    assert chat.anyProcessing is False

    # 切回后台会话能看到完整的过程和结果
    chat.activate_session(first_id)
    assert chat.activeSessionId == first_id
    assert [item["markdown"] for item in chat.messageModel._items] == [
        "后台第一段后台第二段"
    ]


def test_session_row_marks_running_background_session(archived_chat, qapp):
    chat, _runtime, session_id = archived_chat
    chat.refresh_sessions()
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    QQuickStyle.setStyle("Basic")
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(
        Path("ui/qml/components/SessionList.qml").resolve(),
    )))
    root = component.createWithInitialProperties({"theme": theme, "sessionModel": chat.sessionModel})
    window = QQuickWindow()
    try:
        assert root is not None, component.errors()
        root.setParentItem(window.contentItem())
        root.setWidth(208)
        root.setHeight(240)
        window.resize(208, 240)
        window.show()
        QTest.qWait(30)
        label = find_item_by_name(root, "sessionRunningLabel")
        assert label is not None
        assert label.property("visible") is False

        chat.begin_turn(session_id)
        QTest.qWait(30)

        delegate = label.parentItem()
        while delegate.property("running") is None:
            delegate = delegate.parentItem()
        assert delegate.property("running") is True
        assert label.property("visible") is True
        assert label.property("text") == "运行中"
    finally:
        window.close()
        if root is not None:
            root.deleteLater()
        engine.deleteLater()
        qapp.processEvents()


def test_session_row_shows_last_message_time(archived_chat, qapp):
    chat, _runtime, session_id = archived_chat
    chat.refresh_sessions()
    theme = ThemeViewModel(UiConfig(), lambda config: None)
    QQuickStyle.setStyle("Basic")
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(
        Path("ui/qml/components/SessionList.qml").resolve(),
    )))
    root = component.createWithInitialProperties({"theme": theme, "sessionModel": chat.sessionModel})
    window = QQuickWindow()
    try:
        assert root is not None, component.errors()
        root.setParentItem(window.contentItem())
        root.setWidth(208)
        root.setHeight(240)
        window.resize(208, 240)
        window.show()
        QTest.qWait(30)

        label = find_item_by_name(root, "sessionUpdatedLabel")

        assert label is not None
        updated_label = chat.sessionModel.data(chat.sessionModel.index(0), Qt.UserRole + 8)
        assert updated_label
        assert label.property("text") == updated_label + " · 1 轮对话"
        # 时间那行独占整行宽度，不会被运行标识挤掉
        assert label.property("width") == label.parentItem().property("width")
        # 行高不能因为多了一段文字而变化
        delegate = label.parentItem()
        while delegate.property("running") is None:
            delegate = delegate.parentItem()
        assert delegate.property("height") == 66

        chat.begin_turn(session_id)
        QTest.qWait(30)
        assert delegate.property("running") is True
        running_label = find_item_by_name(root, "sessionRunningLabel")
        assert running_label is not None
        assert running_label.property("visible") is True
        # 运行标识在标题那行，不会占用时间那行的宽度
        assert running_label.property("y") < label.property("y")
        assert label.property("width") == label.parentItem().property("width")
    finally:
        window.close()
        if root is not None:
            root.deleteLater()
        engine.deleteLater()
        qapp.processEvents()


def test_finished_session_can_be_deleted_while_another_one_runs(tmp_path, qapp):
    archive = SessionArchiveStore(tmp_path / "sessions.db")
    runtime = ArchiveRuntime(archive)
    dialogs = DialogCoordinator()
    chat = ChatViewModel(runtime, dialogs=dialogs)
    finished_id = runtime.sessions.current_session_id
    archive.archive_turn(str(uuid4()), "已完成的问题", "已完成的回答", finished_id)
    running_id = runtime.sessions.new()
    archive.archive_turn(str(uuid4()), "正在执行的问题", "执行中的回答", running_id)
    chat.activate_session(finished_id)
    chat.begin_turn(running_id)
    assert chat.anyProcessing is True

    try:
        chat.request_delete_session(finished_id)
        # 运行中的是另一个会话，删除已结束的会话不应该被拦下
        assert dialogs.currentConfirmation is not None
        dialogs.resolve_confirmation(dialogs.currentConfirmation["requestId"], True)
        assert not runtime.sessions.list_turns(finished_id)
    finally:
        archive.close()
