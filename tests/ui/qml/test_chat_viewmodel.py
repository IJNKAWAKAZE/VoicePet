from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from core.llm import LlmAttachment
from ui.markdown import sanitize_markdown
from ui.viewmodels.chat import ChatViewModel
from ui.viewmodels.dialogs import DialogCoordinator


def completed(value=None):
    future = Future()
    future.set_result(value)
    return future


def test_mode_change_stops_showing_next_turn_after_reply(qapp):
    chat = ChatViewModel(FakeRuntime())
    chat.begin_turn()
    chat.set_agent_mode("full_auto")
    assert chat.agentModePending
    chat.finish_assistant()
    assert not chat.agentModePending
    chat.begin_turn()
    assert not chat.agentModePending


@dataclass
class Session:
    id: str
    title: str
    turn_count: int
    updated_at: datetime
    is_active: bool


@dataclass
class Turn:
    id: str
    user_text: str
    assistant_text: str
    created_at: datetime


class FakeRuntime:
    def __init__(self):
        self.submitted = []
        self.cancelled = 0
        self.activated = []
        self.created = 0
        self.spoken = []
        self.voice_future = completed("语音结果")
        self.sessions = (
            Session("s1", "第一段对话", 2, datetime.now(UTC), True),
        )
        self.turns = (
            Turn("t1", "你好", "你好呀", datetime.now(UTC)),
        )

    def submit_text(self, text, attachments=()):
        self.submitted.append((text, tuple(attachments)))
        return completed("turn")

    def cancel_active_turn(self, session_id: str = ""):
        self.cancelled += 1
        return completed()

    def speak_notice(self, text):
        self.spoken.append(text)
        return completed("speech")

    def capture_manual_transcript(self):
        return self.voice_future

    def list_sessions(self):
        return completed(self.sessions)

    def list_session_turns(self, session_id):
        return completed(self.turns)

    def activate_session(self, session_id):
        self.activated.append(session_id)
        return completed(self.turns)

    def new_session(self):
        self.created += 1
        return completed("new-session")


def test_markdown_removes_images_and_html_but_keeps_links_as_text():
    source = (
        "你好 ![私密](file:///private.png) <img src='https://x/y.png'> "
        "[官网](https://example.com)"
    )

    safe = sanitize_markdown(source)

    assert "![" not in safe
    assert "<img" not in safe
    assert "官网" in safe
    assert "https://example.com" in safe


def test_markdown_adds_qt_emphasis_boundaries_for_chinese_punctuation():
    safe = sanitize_markdown("活动是**「幽影迷城」**大型活动")
    assert safe == "活动是 **「幽影迷城」** 大型活动"


def test_submit_rejects_blank_and_builds_streaming_pair():
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)

    chat.submit("   ")
    chat.submit("  你好  ")

    assert runtime.submitted == [("你好", ())]
    assert chat.processing is True
    assert chat.message_model.rowCount() == 2
    assert chat.message_model.data(chat.message_model.index(0), Qt.UserRole + 2) == "user"
    assert chat.message_model.data(chat.message_model.index(1), Qt.UserRole + 4) == "streaming"


def test_submit_passes_attachment_metadata_to_runtime():
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)
    attachment = LlmAttachment("C:/tmp/a.txt", "a.txt", "text/plain", 1, "a" * 64, "file")

    chat.submit("请读取", [attachment])

    assert runtime.submitted == [("请读取", (attachment,))]


def test_submit_exposes_attachment_metadata_for_chat_history(tmp_path):
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)
    image = tmp_path / "预览.png"
    image.write_bytes(b"not-a-real-image")

    chat.submit("请看看", [{"path": str(image), "kind": "image"}])

    item = chat.message_model._items[0]
    assert item["markdown"] == "请看看"
    assert item["attachments"][0]["name"] == "预览.png"
    assert item["attachments"][0]["kind"] == "image"
    assert str(item["attachments"][0]["url"]).startswith("file:///")


def test_submit_rejects_attachment_without_text(tmp_path):
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)
    path = tmp_path / "a.txt"
    path.write_text("内容", encoding="utf-8")

    messages = []
    chat.errorOccurred.connect(messages.append)
    chat.submit("   ", [{"path": str(path), "kind": "file"}])

    assert runtime.submitted == []
    assert chat.message_model.rowCount() == 0
    assert messages == ["请先输入文字后再发送附件"]


def test_submit_rejects_more_attachments_than_limit(tmp_path):
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)
    path = tmp_path / "a.txt"
    path.write_text("内容", encoding="utf-8")
    payload = [{"path": str(path), "name": "a.txt", "kind": "file"}] * 17

    messages = []
    chat.errorOccurred.connect(messages.append)
    chat.submit("请读取", payload)

    assert runtime.submitted == []
    assert chat.message_model.rowCount() == 0
    assert messages == ["最多只能添加 16 个附件"]


def test_open_attachment_uses_system_default_handler(monkeypatch):
    opened = []
    monkeypatch.setattr(
        "ui.viewmodels.chat.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    ChatViewModel(FakeRuntime()).open_attachment(r"C:\\tmp\\a.txt")

    assert opened == ["C://tmp//a.txt"]


def test_submit_rejects_unreadable_attachment_and_keeps_message_empty(tmp_path):
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)

    chat.submit("请读取", [{"path": str(tmp_path / "missing.txt"), "name": "missing.txt", "kind": "file"}])

    assert runtime.submitted == []
    assert chat.message_model.rowCount() == 0


def test_copy_message_writes_plain_text_to_system_clipboard(qapp):
    dialogs = DialogCoordinator()
    chat = ChatViewModel(FakeRuntime(), dialogs=dialogs)

    chat.copy_message("要复制的内容")

    assert QGuiApplication.clipboard().text() == "要复制的内容"
    assert dialogs.toastModel.rowCount() == 1
    assert dialogs.toastModel.data(dialogs.toastModel.index(0), Qt.UserRole + 1) == "已复制"


def test_play_message_delegates_to_runtime_speech(qapp):
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)

    chat.play_message("要播放的内容")

    assert runtime.spoken == ["要播放的内容"]


def test_start_voice_input_emits_transcript_without_submitting(qapp):
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)
    received = []
    chat.transcriptReady.connect(received.append)

    chat.start_voice_input()
    qapp.processEvents()

    assert received == ["语音结果"]
    assert runtime.submitted == []


def test_deltas_only_update_last_streaming_assistant_message():
    chat = ChatViewModel(FakeRuntime())
    chat.submit("问题")

    chat.append_assistant_delta("答")
    chat.append_assistant_delta("案")
    chat.finish_assistant()
    chat.append_assistant_delta("下一条")

    model = chat.message_model
    assert model.rowCount() == 3
    assert model.data(model.index(1), Qt.UserRole + 3) == "答案"
    assert model.data(model.index(1), Qt.UserRole + 4) == "complete"
    assert model.data(model.index(2), Qt.UserRole + 3) == "下一条"
    assert chat.processing is True


def test_stop_generation_delegates_to_runtime():
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)
    chat.submit("问题")

    chat.stop_generation()

    assert runtime.cancelled == 1
    assert chat.processing is False


def test_sessions_and_turns_are_mapped_without_exposing_runtime_objects():
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)

    chat.refresh_sessions()
    chat.activate_session("s1")

    assert chat.session_model.rowCount() == 1
    session_index = chat.session_model.index(0)
    assert chat.session_model.data(session_index, Qt.UserRole + 1) == "s1"
    assert chat.session_model.data(session_index, Qt.UserRole + 5) is True
    assert chat.message_model.rowCount() == 2
    assert runtime.activated == ["s1"]


def test_new_session_clears_messages_after_runtime_accepts():
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)
    chat.activate_session("s1")

    chat.new_session()

    assert runtime.created == 1
    assert chat.message_model.rowCount() == 0
    assert chat.active_session_id == "new-session"


def test_message_time_labels_use_today_yesterday_and_dates():
    from ui.viewmodels.chat import _time_label

    today = datetime.now(UTC).astimezone().date()
    assert _time_label(f"{today.isoformat()} 09:05") == "今天 09:05"
    yesterday = today - timedelta(days=1)
    assert _time_label(f"{yesterday.isoformat()} 23:40") == "昨天 23:40"
    older = today - timedelta(days=40)
    assert _time_label(f"{older.isoformat()} 08:00") == f"{older:%m-%d} 08:00"
    assert _time_label("2019-01-02 03:04") == "2019-01-02 03:04"
    assert _time_label("") == ""


def test_every_message_keeps_its_own_time_label():
    chat = ChatViewModel(FakeRuntime())
    chat.submit("问题")
    chat.append_assistant_delta("最终回复", "msg-1")

    items = chat.message_model._items
    assert [item["role"] for item in items] == ["user", "assistant"]
    # 回复接管占位气泡后仍然带着自己的时间
    assert all(item["createdLabel"].startswith("今天 ") for item in items)
    assert items[0]["createdLabel"] == items[1]["createdLabel"]


def test_session_list_reports_last_message_time():
    chat = ChatViewModel(FakeRuntime())

    chat.refresh_sessions()

    index = chat.session_model.index(0)
    assert chat.session_model.data(index, Qt.UserRole + 8).startswith("今天 ")


def test_restored_history_keeps_message_times():
    runtime = FakeRuntime()
    chat = ChatViewModel(runtime)

    chat.activate_session("s1")

    stamp = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")
    restored = [item for item in chat.message_model._items if item["role"] == "assistant"]
    assert [item["createdAt"] for item in restored] == [stamp]
    assert restored[0]["createdLabel"] == f"今天 {stamp[11:]}"


def test_execution_process_stays_out_of_the_chat():
    chat = ChatViewModel(FakeRuntime())
    chat.submit("问题")

    chat.append_assistant_delta("先看目录结构", "message-1")
    chat.append_assistant_delta("go vet 通过", "message-2")
    chat.finish_assistant()

    # 聊天记录只保留对话内容：过程细节不成条，每条模型消息各成一个回复气泡
    items = chat.message_model._items
    assert [item["role"] for item in items] == ["user", "assistant", "assistant"]
    assert [item["markdown"] for item in items][1:] == ["先看目录结构", "go vet 通过"]
    assert chat.processing is False


def test_reply_takes_over_the_placeholder_bubble():
    chat = ChatViewModel(FakeRuntime())
    chat.submit("问题")
    chat.append_assistant_delta("这是最终回复", "msg-1")

    items = chat.message_model._items
    assert [item["role"] for item in items] == ["user", "assistant"]
    assert items[-1]["messageId"] == "agent:msg-1"
    assert items[-1]["markdown"] == "这是最终回复"


def test_each_agent_message_item_becomes_its_own_bubble():
    chat = ChatViewModel(FakeRuntime())
    chat.submit("问题")

    chat.append_assistant_delta("先看代码", "message-1")
    chat.append_assistant_delta("，再改配置", "message-1")
    chat.append_assistant_delta("改完了", "message-2")
    chat.finish_assistant()

    model = chat.message_model
    assert model.rowCount() == 3
    assert model.data(model.index(1), Qt.UserRole + 3) == "先看代码，再改配置"
    assert model.data(model.index(1), Qt.UserRole + 4) == "complete"
    assert model.data(model.index(2), Qt.UserRole + 3) == "改完了"
    assert model.data(model.index(2), Qt.UserRole + 4) == "complete"


def test_interim_assistant_messages_keep_their_own_bubbles():
    chat = ChatViewModel(FakeRuntime())
    chat.submit("问题")

    chat.append_assistant_delta("我先看一下 RustDesk 的状态。", "m1")
    chat.append_assistant_delta("旧连接还在走代理，我把它踢掉再验证：", "m2")
    chat.finish_assistant()

    model = chat.message_model
    assert model.rowCount() == 3
    # 模型自己说的中间说明同样作为一条消息展示
    assert model.data(model.index(1), Qt.UserRole + 2) == "assistant"
    assert model.data(model.index(1), Qt.UserRole + 3) == "我先看一下 RustDesk 的状态。"
    assert model.data(model.index(1), Qt.UserRole + 4) == "complete"
    assert model.data(model.index(2), Qt.UserRole + 2) == "assistant"
    assert model.data(model.index(2), Qt.UserRole + 3) == "旧连接还在走代理，我把它踢掉再验证："


def test_tool_result_is_rendered_as_safe_structured_message():
    chat = ChatViewModel(FakeRuntime())

    chat.append_tool_result(
        "call-1",
        "success",
        {"message": "已打开记事本", "secret": "sk-private"},
    )

    model = chat.message_model
    assert model.rowCount() == 1
    assert model.data(model.index(0), Qt.UserRole + 1) == "tool:call-1"
    assert model.data(model.index(0), Qt.UserRole + 2) == "tool"
    assert model.data(model.index(0), Qt.UserRole + 3) == "已打开记事本"
    assert model.data(model.index(0), Qt.UserRole + 4) == "success"
    assert "sk-private" not in model.data(model.index(0), Qt.UserRole + 3)
