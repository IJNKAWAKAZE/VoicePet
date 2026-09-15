from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QImage, QWheelEvent
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QSignalSpy, QTest

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


def test_history_messages_show_time_above_each_bubble(history_window, qapp):
    root, _chat, _, _ = history_window
    session_list = root.findChild(QQuickItem, "sessionList")
    button = next(item for item in visual_items(session_list)
                  if item.property("sessionId") == "history-1")
    position = button.mapToScene(QPoint(20, 20)).toPoint()
    QTest.mouseClick(root, Qt.LeftButton, pos=position)
    QTest.qWait(100)

    page = root.findChild(QQuickItem, "chatPage")
    message_list = next(item for item in visual_items(page)
                        if item.objectName() == "chatMessageList"
                        and item.property("count") == 2)
    labels = [item for item in visual_items(message_list)
              if item.objectName() == "messageTimeLabel"]
    bubbles = [item for item in visual_items(message_list)
               if item.metaObject().className().startswith("MessageBubble")]
    assert len(labels) == 2
    # 提问和回复各自带一条时间，且都排在气泡上方
    assert [label.isVisible() for label in labels] == [True, True]
    assert all(label.property("text").startswith("今天 ") for label in labels)
    assert bubbles[0].y() >= labels[0].y() + labels[0].height()
    assert bubbles[1].y() >= labels[1].y() + labels[1].height()


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


def test_execution_process_never_reaches_the_message_list(history_window, qapp):
    root, chat, shell, _ = history_window
    shell.navigate("chat")
    QTest.qWait(30)

    page = root.findChild(QQuickItem, "chatPage")
    message_list = next(item for item in visual_items(page)
                        if item.objectName() == "chatMessageList")
    chat.activate_session("history-1")
    QTest.qWait(120)
    before = message_list.property("count")

    chat.append_assistant_delta("先看目录结构", "message-1", "history-1")
    chat.append_assistant_delta("go vet 通过", "message-2", "history-1")
    chat.finish_assistant("history-1")
    QTest.qWait(60)

    # 聊天列表只增加模型自己说的话，思考与命令都不成条
    assert chat.message_model.rowCount() == before + 2
    assert message_list.property("count") == before + 2


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


@pytest.mark.parametrize("entry", ["text", "voice", "composer_voice"])
def test_new_user_input_returns_from_history_to_latest_message(history_window, entry):
    root, chat, _, _ = history_window
    for index in range(18):
        chat.append_user_message(f"历史问题 {index}")
        chat.append_assistant_delta("历史回答。" * 30)
        chat.finish_assistant()
    QTest.qWait(60)
    messages = root.findChild(QQuickItem, "chatMessageList")
    messages.setProperty("followTail", False)
    messages.positionViewAtBeginning()
    QTest.qWait(30)
    assert not messages.property("atYEnd")
    if entry == "text":
        chat.submit("新的文字问题")
    elif entry == "voice":
        chat.append_user_message("刚刚说出的语音问题")
    else:
        chat.start_voice_input()
    QTest.qWait(80)
    assert messages.property("atYEnd")
    assert messages.property("followTail")


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


def test_chat_composer_exposes_attachment_menu_and_microphone_button(history_window):
    root, _, _, _ = history_window
    composer = root.findChild(QQuickItem, "chatComposer")
    assert composer is not None
    plus = composer.findChild(QObject, "attachmentMenuButton")
    microphone = composer.findChild(QObject, "microphoneButton")
    assert plus is not None and microphone is not None
    QTest.mouseClick(root, Qt.LeftButton, pos=plus.mapToScene(QPointF(8, 8)).toPoint())
    menu = composer.findChild(QObject, "attachmentMenu")
    assert menu is not None and menu.property("visible")
    QTest.qWait(80)
    image_button = menu.findChild(QQuickItem, "uploadImageButton")
    file_button = menu.findChild(QQuickItem, "uploadFileButton")
    assert image_button.width() > 100
    assert menu.property("height") >= image_button.height() + file_button.height() + 16
    assert menu.findChild(QObject, "uploadImageButton") is not None
    assert menu.findChild(QObject, "uploadFileButton") is not None
    assert menu.findChild(QObject, "voiceInputMenuButton") is None
    clicked = QSignalSpy(file_button.clicked)
    QTest.mouseClick(root, Qt.LeftButton, pos=file_button.mapToScene(QPointF(40, 20)).toPoint())
    assert clicked.count() == 1
    assert not menu.property("visible")


def test_voice_append_keeps_draft_and_moves_cursor_to_end(history_window):
    root, chat, _, _ = history_window
    editor = root.findChild(QQuickItem, "composerText")
    editor.setProperty("text", "原有文字\n")
    editor.setProperty("cursorPosition", 0)
    chat.transcriptReady.emit("第一段语音")
    chat.transcriptReady.emit("第二段语音")
    assert editor.property("text") == "原有文字\n第一段语音 第二段语音"
    assert editor.property("cursorPosition") == editor.property("length")
    assert editor.property("activeFocus")


@pytest.mark.parametrize("keyboard", [False, True])
def test_sending_voice_draft_clears_editor_after_acceptance(history_window, keyboard):
    root, chat, _, _ = history_window
    editor = root.findChild(QQuickItem, "composerText")
    chat.transcriptReady.emit("语音输入的文字")
    assert editor.property("text") == "语音输入的文字"
    if keyboard:
        QTest.keyClick(root, Qt.Key_Return)
    else:
        button = root.findChild(QQuickItem, "composerSendButton")
        QTest.mouseClick(root, Qt.LeftButton, pos=button.mapToScene(QPoint(10, 10)).toPoint())
    QTest.qWait(30)
    assert editor.property("text") == ""


@pytest.mark.parametrize("kind", ["image", "file"])
def test_attachment_leaves_a_full_visible_line_for_the_draft(history_window, tmp_path, kind):
    root, _, _, _ = history_window
    composer = root.findChild(QQuickItem, "chatComposer")
    editor = root.findChild(QQuickItem, "composerText")
    scroll = root.findChild(QQuickItem, "composerScroll")
    toolbar = root.findChild(QQuickItem, "composerToolbar")
    composer.addAttachment(QUrl.fromLocalFile(str(tmp_path / "attachment.png")), kind)
    composer.setDraftText("这个附件是什么？")
    QTest.qWait(30)
    assert scroll.height() >= editor.property("cursorRectangle").height() + editor.property("topPadding") + editor.property("bottomPadding")
    assert scroll.mapToScene(QPointF(0, scroll.height())).y() <= toolbar.mapToScene(QPointF(0, 0)).y()


def test_long_draft_has_scrollbar_and_cursor_can_reach_both_ends(history_window):
    root, _, _, _ = history_window
    composer = root.findChild(QQuickItem, "chatComposer")
    editor = root.findChild(QQuickItem, "composerText")
    scroll = root.findChild(QQuickItem, "composerScroll")
    bar = root.findChild(QQuickItem, "composerScrollBar")
    draft = "多行草稿，需要能够上下查看。\n" * 50
    composer.setDraftText(draft)
    QTest.qWait(30)
    flickable = scroll.property("contentItem")
    assert bar.property("visible")
    assert bar.property("size") < 1
    assert flickable.property("contentY") > 0
    assert scroll.height() <= 140
    QTest.keyClick(root, Qt.Key_Home, Qt.ControlModifier)
    QTest.qWait(30)
    assert editor.property("cursorPosition") == 0
    assert flickable.property("contentY") <= 1
    QTest.keyClick(root, Qt.Key_End, Qt.ControlModifier)
    QTest.qWait(30)
    assert editor.property("cursorPosition") == len(draft)
    assert flickable.property("contentY") > 0


def test_busy_enter_does_not_insert_newline_and_ready_enter_sends(history_window):
    root, chat, _, _ = history_window
    composer = root.findChild(QQuickItem, "chatComposer")
    editor = root.findChild(QQuickItem, "composerText")
    sent = QSignalSpy(composer.submitRequested)
    chat.begin_turn()
    composer.setDraftText("等待发送的文字")
    QTest.keyClick(root, Qt.Key_Return)
    assert editor.property("text") == "等待发送的文字"
    assert sent.count() == 0
    QTest.keyClick(root, Qt.Key_Return, Qt.ShiftModifier)
    assert editor.property("text") == "等待发送的文字\n"
    chat.finish_assistant()
    QTest.keyClick(root, Qt.Key_Return)
    QTest.qWait(30)
    assert sent.count() == 1
    assert editor.property("text") == ""


def test_qml_attachment_payload_is_plain_mapping_and_can_be_submitted(history_window, tmp_path):
    root, chat, _, _ = history_window
    composer = root.findChild(QQuickItem, "chatComposer")
    path = tmp_path / "测试附件.txt"
    path.write_text("附件内容", encoding="utf-8")
    composer.addAttachment(QUrl.fromLocalFile(str(path)), "file")
    payload = composer.attachmentPayload()
    variant = payload.toVariant()
    assert isinstance(variant[0], dict)
    assert variant[0]["path"].startswith("file:///")
    chat.submit("读取附件", payload)
    assert chat.messageModel.rowCount() == 2


@pytest.mark.parametrize("kind", ["image", "file"])
def test_select_attachment_restores_draft_focus(history_window, tmp_path, kind):
    root, _, _, _ = history_window
    composer = root.findChild(QQuickItem, "chatComposer")
    editor = root.findChild(QQuickItem, "composerText")
    editor.setProperty("text", "已有草稿")
    editor.setProperty("cursorPosition", 0)
    plus = composer.findChild(QQuickItem, "attachmentMenuButton")
    plus.forceActiveFocus()
    composer.addAttachment(QUrl.fromLocalFile(str(tmp_path / "附件.png")), kind)
    QTest.qWait(50)
    assert editor.property("activeFocus")
    assert editor.property("text") == "已有草稿"
    assert editor.property("cursorPosition") == editor.property("length")


def test_chat_history_renders_image_attachment(history_window):
    root, chat, _, _ = history_window
    composer = root.findChild(QQuickItem, "chatComposer")
    composer.addAttachment(
        QUrl.fromLocalFile(str(Path("assets/ui/themes/deep_night/background.webp").resolve())),
        "image",
    )
    chat.submit("请看看图片", composer.attachmentPayload())
    QTest.qWait(120)
    page = root.findChild(QQuickItem, "chatPage")
    bubble = next(
        item for item in visual_items(page)
        if item.metaObject().className().startswith("MessageBubble")
    )
    attachments = bubble.property("attachments")
    assert len(attachments) == 1
    image = next(
        (item for item in visual_items(bubble) if item.objectName() == "messageAttachmentImage"),
        None,
    )
    assert image is not None and image.property("visible")


@pytest.mark.parametrize("content", ["text", "files", "image"])
def test_ctrl_v_pastes_clipboard_content(history_window, tmp_path, monkeypatch, content):
    root, chat, _, _ = history_window
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    editor = root.findChild(QQuickItem, "composerText")
    composer = root.findChild(QQuickItem, "chatComposer")
    clipboard = QGuiApplication.clipboard()
    if content == "text":
        clipboard.setText("粘贴的文字")
    elif content == "files":
        paths = [tmp_path / "资料.txt", tmp_path / "截图.png"]
        for path in paths:
            path.write_bytes(b"test")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        clipboard.setMimeData(mime)
    else:
        screenshot = QImage(16, 12, QImage.Format_ARGB32)
        screenshot.fill(Qt.blue)
        clipboard.setImage(screenshot)
    editor.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_V, Qt.ControlModifier)
    QTest.qWait(50)
    payload = composer.attachmentPayload().toVariant()
    if content == "text":
        assert editor.property("text") == "粘贴的文字"
        assert payload == []
    else:
        assert editor.property("text") == ""
        assert editor.property("activeFocus")
        assert len(payload) == (2 if content == "files" else 1)
        assert chat.messageModel.rowCount() == 0
        if content == "image":
            saved = QImage(QUrl(payload[0]["path"]).toLocalFile())
            assert saved.size() == screenshot.size()
            assert saved.pixelColor(0, 0) == screenshot.pixelColor(0, 0)
    clipboard.clear()


def test_ctrl_v_pastes_folder_as_absolute_path(history_window, tmp_path):
    root, chat, _, _ = history_window
    editor = root.findChild(QQuickItem, "composerText")
    composer = root.findChild(QQuickItem, "chatComposer")
    errors = QSignalSpy(chat.errorOccurred)
    folder = tmp_path / "旅行资料"
    folder.mkdir()
    document = tmp_path / "说明.txt"
    document.write_bytes(b"test")
    mime = QMimeData()
    mime.setUrls([
        QUrl.fromLocalFile(str(folder)),
        QUrl.fromLocalFile(str(document)),
    ])
    QGuiApplication.clipboard().setMimeData(mime)

    editor.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_V, Qt.ControlModifier)
    QTest.qWait(50)

    # 文件夹没有附件语义，改为把绝对路径插入输入框，文件仍然作为附件
    assert Path(editor.property("text")) == folder.resolve()
    payload = composer.attachmentPayload().toVariant()
    assert [item["name"] for item in payload] == ["说明.txt"]
    assert errors.count() == 0
    QGuiApplication.clipboard().clear()


def test_ctrl_v_warns_when_attachment_limit_reached(history_window, tmp_path):
    root, chat, _, _ = history_window
    editor = root.findChild(QQuickItem, "composerText")
    composer = root.findChild(QQuickItem, "chatComposer")
    errors = QSignalSpy(chat.errorOccurred)
    document = tmp_path / "资料.txt"
    document.write_bytes(b"test")
    for _ in range(16):
        composer.addAttachment(QUrl.fromLocalFile(str(document)), "file")
    assert len(composer.attachmentPayload().toVariant()) == 16
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(document))])
    QGuiApplication.clipboard().setMimeData(mime)

    editor.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_V, Qt.ControlModifier)
    QTest.qWait(50)

    assert len(composer.attachmentPayload().toVariant()) == 16
    assert editor.property("text") == ""
    assert errors.count() == 1
    assert "16" in errors.at(0)[0]
    QGuiApplication.clipboard().clear()


def test_message_actions_are_clickable_without_moving_next_message(history_window):
    root, chat, _, _ = history_window
    chat.append_user_message("复制这段文字")
    chat.append_assistant_delta("下一条消息")
    QTest.qWait(100)
    page = root.findChild(QQuickItem, "chatPage")
    bubbles = [item for item in visual_items(page)
               if item.metaObject().className().startswith("MessageBubble")]
    bubble, following = bubbles
    initial_y = following.mapToScene(QPointF()).y()
    QTest.mouseMove(root, bubble.mapToScene(QPointF(20, 20)).toPoint())
    QTest.qWait(50)
    for name in ("copyMessageButton", "playMessageButton"):
        button = bubble.findChild(QQuickItem, name)
        # 实际跨过气泡与操作栏之间的间隙，再移到按钮中心
        QTest.mouseMove(root, bubble.mapToScene(QPointF(bubble.width() - 20, bubble.height() + 1)).toPoint())
        QTest.mouseMove(root, button.mapToScene(QPointF(14, 14)).toPoint())
        QTest.qWait(50)
        assert button.property("visible")
        clicked = QSignalSpy(button.clicked)
        QTest.mouseClick(root, Qt.LeftButton, pos=button.mapToScene(QPointF(14, 14)).toPoint())
        assert clicked.count() == 1
        assert following.mapToScene(QPointF()).y() == initial_y
    assert QGuiApplication.clipboard().text() == "复制这段文字"


def test_message_actions_are_icon_only_and_hidden_until_hover(history_window, qapp):
    root, chat, _, _ = history_window
    chat.append_user_message("带操作的消息")
    QTest.qWait(50)
    page = root.findChild(QQuickItem, "chatPage")
    bubble = next(item for item in visual_items(page)
                  if item.metaObject().className().startswith("MessageBubble"))
    copy_button = bubble.findChild(QObject, "copyMessageButton")
    play_button = bubble.findChild(QObject, "playMessageButton")
    assert copy_button is not None and play_button is not None
    assert not copy_button.property("visible")
    QTest.mouseMove(root, bubble.mapToScene(QPointF(20, 20)).toPoint())
    qapp.processEvents()
    assert copy_button.property("visible")
    assert play_button.property("visible")
    assert not str(copy_button.property("text"))
    assert not str(play_button.property("text"))


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


def test_code_block_wraps_inside_bubble_instead_of_overflowing(history_window):
    root, chat, _, _ = history_window
    long_line = "Left 60%: " + "a flat opaque dark charcoal panel with chamfered corners, " * 3
    chat.append_user_message("给我一段提示词")
    chat.append_assistant_delta("①概念图\n```\n" + long_line.strip() + "\n```\n②底图\n")
    QTest.qWait(150)

    page = root.findChild(QQuickItem, "chatPage")
    bubble = next(
        item for item in visual_items(page)
        if item.metaObject().className().startswith("MessageBubble")
        and "```" in str(item.property("markdown"))
    )
    # Qt 的 Markdown 导入器把代码块段落标成不换行，必须换行渲染，否则文字会画出气泡外
    visible = [item for item in visual_items(bubble) if item.property("visible")]
    code = next(item for item in visible if item.objectName() == "messageCodeText")
    assert code.property("contentWidth") <= code.width() + 1
    assert code.height() == pytest.approx(code.property("implicitHeight"))

    code_block = next(item for item in visible if item.objectName() == "messageCodeBlock")
    assert code_block.y() + code_block.height() <= bubble.height()

    prose = [item for item in visible if item.objectName() == "messageMarkdownText"]
    assert len(prose) == 2
    assert all(item.height() == pytest.approx(item.property("implicitHeight")) for item in prose)
