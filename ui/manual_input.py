"""从系统托盘打开的常驻多轮聊天窗口"""

from __future__ import annotations

from math import ceil

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_STYLE = """
QWidget#manual_input { background: #0D1117; color: #F1F5F7; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QFrame#chat_sidebar { background: #111720; border-right: 1px solid #28313C; }
QLabel#sidebar_title { color: #F7FAFB; font-size: 17px; font-weight: 700; }
QLabel#manual_title { color: #F7FAFB; font-size: 19px; font-weight: 700; }
QLabel#session_title { color: #8E9AA6; font-size: 12px; }
QLabel#manual_error { color: #FFAAA0; min-height: 18px; }
QListWidget#chat_sessions { background: transparent; color: #B8C3CC; border: 0; outline: 0; padding: 2px; }
QListWidget#chat_sessions::item { padding: 10px 9px; margin: 2px 0; border-radius: 8px; }
QListWidget#chat_sessions::item:hover { background: #1D2630; color: #FFFFFF; }
QListWidget#chat_sessions::item:selected { background: #20383A; color: #82E4DC; }
QFrame#session_card { background: transparent; border: 0; border-radius: 8px; }
QLabel#session_card_title { color: #B8C3CC; font-size: 13px; }
QLabel#session_card_meta { color: #71808D; font-size: 11px; }
QLabel#session_card_title[active="true"] { color: #82E4DC; font-weight: 700; }
QToolButton#delete_session { background: transparent; color: #70808D; border: 0; border-radius: 10px; font-size: 17px; }
QToolButton#delete_session:hover { background: #49252B; color: #FF9299; }
QScrollArea#chat_history { background: #0D1117; border: 0; }
QScrollArea#chat_history > QWidget > QWidget { background: #0D1117; }
QFrame#message_user { background: #286B69; border: 1px solid #37807D; border-radius: 13px; }
QFrame#message_assistant { background: #19232E; border: 1px solid #2D3A47; border-radius: 13px; }
QLabel#message_user_name { color: #DFFFFC; font-size: 11px; font-weight: 700; }
QLabel#message_assistant_name { color: #74DED3; font-size: 11px; font-weight: 700; }
QTextBrowser#message_body { background: transparent; color: #F2F6F7; border: 0; padding: 0; font-size: 14px; }
QLabel#empty_history { color: #73808D; font-size: 13px; }
QLabel#typing_body { color: #9DAAB5; font-size: 13px; }
QFrame#composer { background: #111820; border: 1px solid #34414E; border-radius: 12px; }
QLineEdit#chat_input { background: transparent; color: #F1F5F7; border: 0; padding: 10px 11px; min-height: 30px; selection-background-color: #285A5A; }
QFrame#composer[focused="true"] { border-color: #5FD3C7; }
QPushButton { background: #202A35; color: #EDF2F4; border: 1px solid #3A4653; border-radius: 8px; padding: 8px 14px; min-height: 22px; }
QPushButton:hover { background: #293541; border-color: #5FD3C7; }
QPushButton:pressed { background: #182129; }
QPushButton:disabled { color: #687582; background: #171D24; border-color: #2A333D; }
QPushButton#manual_send { background: #5FD3C7; color: #0D1719; border-color: #5FD3C7; font-weight: 700; min-width: 64px; }
QPushButton#manual_send:hover { background: #7BE0D6; }
QPushButton#new_session { color: #74DED3; width: 100%; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 6px 3px; }
QScrollBar::handle:vertical { background: #4A5663; border-radius: 5px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: #6B7A89; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


class _ComposerFrame(QFrame):
    """让输入框焦点反馈覆盖整个编辑区域"""

    def set_focused(self, focused: bool) -> None:
        self.setProperty("focused", focused)
        self.style().unpolish(self)
        self.style().polish(self)


class _ChatLineEdit(QLineEdit):
    """只把回车解释为发送，不交给顶层窗口处理"""

    send_requested = Signal()
    focus_changed = Signal(bool)

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_changed.emit(False)


class _MarkdownMessageBody(QTextBrowser):
    """安全渲染聊天消息中的 Markdown"""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("message_body")
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(
            self._document_size_changed
        )
        self.set_message(text)

    def set_message(self, text: str) -> None:
        features = (
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML
        )
        self.document().setMarkdown(text, features)
        self._fit_height()

    def set_content_width(self, width: int) -> None:
        self.setFixedWidth(max(1, width))
        self.document().setTextWidth(max(1, width))
        self._fit_height()

    def _fit_height(self) -> None:
        self._document_size_changed(self.document().size())

    def _document_size_changed(self, size) -> None:
        target_height = max(20, ceil(size.height()) + 2)
        if self.height() != target_height:
            self.setFixedHeight(target_height)
            self.updateGeometry()
        self.verticalScrollBar().setValue(0)

    def loadResource(self, resource_type, name):
        """阻止 Markdown 内容读取本地或远程图片"""

        if resource_type == QTextDocument.ResourceType.ImageResource:
            return None
        return super().loadResource(resource_type, name)


class _SessionCard(QFrame):
    """带独立删除入口的会话侧栏卡片"""

    activated = Signal(str)
    delete_requested = Signal(str)

    def __init__(
        self,
        session_id: str,
        title: str,
        meta: str,
        *,
        active: bool,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self.setObjectName("session_card")
        title_label = QLabel(title)
        title_label.setObjectName("session_card_title")
        title_label.setProperty("active", active)
        meta_label = QLabel(meta)
        meta_label.setObjectName("session_card_meta")
        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(2)
        labels.addWidget(title_label)
        labels.addWidget(meta_label)
        delete_button = QToolButton()
        delete_button.setObjectName("delete_session")
        delete_button.setText("×")
        delete_button.setToolTip("删除会话")
        delete_button.setAccessibleName("删除会话")
        delete_button.setFixedSize(24, 24)
        delete_button.clicked.connect(
            lambda: self.delete_requested.emit(self._session_id)
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 7, 5, 7)
        layout.setSpacing(4)
        layout.addLayout(labels, 1)
        layout.addWidget(delete_button, 0, Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            self.activated.emit(self._session_id)
        super().mousePressEvent(event)


class ManualInputDialog(QWidget):
    """持续展示当前会话并收集多条手动文本"""

    submitted = Signal(str)
    new_session_requested = Signal()
    session_activate_requested = Signal(str)
    session_delete_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("manual_input")
        self.setWindowTitle("VoicePet 聊天")
        self.resize(920, 650)
        self.setMinimumSize(720, 500)
        self.setStyleSheet(_STYLE)
        self._session_id = ""
        self._messages: list[tuple[str, str]] = []
        self._assistant_streaming = False
        self._streaming_body: _MarkdownMessageBody | None = None
        self._streaming_bubble: QFrame | None = None
        self._submitting = False
        self._processing = False
        self._typing_active = False
        self._typing_label: QLabel | None = None
        self._typing_step = 0
        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(420)
        self._typing_timer.timeout.connect(self._animate_typing)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._apply_scroll_bottom)

        self.session_list = QListWidget()
        self.session_list.setObjectName("chat_sessions")
        self.session_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        sidebar_title = QLabel("会话")
        sidebar_title.setObjectName("sidebar_title")
        self.new_session_button = QPushButton("＋  新建会话")
        self.new_session_button.setObjectName("new_session")
        sidebar = QFrame()
        sidebar.setObjectName("chat_sidebar")
        sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 18, 14, 16)
        sidebar_layout.setSpacing(10)
        sidebar_layout.addWidget(sidebar_title)
        sidebar_layout.addWidget(self.new_session_button)
        sidebar_layout.addWidget(self.session_list, 1)

        title = QLabel("新会话")
        title.setObjectName("manual_title")
        self.chat_title = title
        self.session_title = QLabel("语音和文字共享当前会话上下文")
        self.session_title.setObjectName("session_title")
        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        title_layout.addWidget(title)
        title_layout.addWidget(self.session_title)
        header = QHBoxLayout()
        header.addLayout(title_layout)
        header.addStretch(1)

        self._messages_widget = QWidget()
        self._messages_layout = QVBoxLayout(self._messages_widget)
        self._messages_layout.setContentsMargins(18, 14, 18, 14)
        self._messages_layout.setSpacing(8)
        self._messages_layout.setSizeConstraint(
            QLayout.SizeConstraint.SetMinAndMaxSize
        )
        self.history_view = QScrollArea()
        self.history_view.setObjectName("chat_history")
        self.history_view.setWidgetResizable(True)
        self.history_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.history_view.setWidget(self._messages_widget)
        self.history_view.verticalScrollBar().rangeChanged.connect(
            self._scroll_range_changed
        )

        self.text_edit = _ChatLineEdit()
        self.text_edit.setObjectName("chat_input")
        self.text_edit.setMaxLength(4096)
        self.text_edit.setPlaceholderText("输入消息，按回车发送")
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("manual_send")
        composer = _ComposerFrame()
        composer.setObjectName("composer")
        composer.setProperty("focused", False)
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(2, 2, 4, 2)
        composer_layout.setSpacing(4)
        composer_layout.addWidget(self.text_edit, 1)
        composer_layout.addWidget(self.send_button)
        self.error_label = QLabel()
        self.error_label.setObjectName("manual_error")

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(22, 17, 22, 14)
        main_layout.setSpacing(10)
        main_layout.addLayout(header)
        main_layout.addWidget(self.history_view, 1)
        main_layout.addWidget(composer)
        main_layout.addWidget(self.error_label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar)
        layout.addWidget(main, 1)

        self.text_edit.send_requested.connect(self._submit)
        self.text_edit.focus_changed.connect(composer.set_focused)
        self.send_button.clicked.connect(self._submit)
        self.new_session_button.clicked.connect(
            self.new_session_requested.emit
        )
        self._render_history()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def conversation_text(self) -> str:
        """返回用于界面验证的当前纯文本记录"""

        if not self._messages:
            return "这里还没有消息"
        return "\n".join(text for _, text in self._messages)

    def open_for_input(self) -> None:
        """非阻塞显示窗口并保留当前会话内容"""

        self.error_label.clear()
        self.set_submitting(False)
        self.show()
        self.raise_()
        self.activateWindow()
        self.text_edit.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._scroll_to_bottom()

    def set_session_records(self, records) -> None:
        """刷新侧栏中的多轮会话列表"""

        blocker = QSignalBlocker(self.session_list)
        self.session_list.clear()
        found_current = False
        for record in records:
            title = " ".join(record.title.split())
            if len(title) > 22:
                title = f"{title[:21]}…"
            active = (
                record.id == self._session_id
                if self._session_id
                else record.is_active
            )
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 62))
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            self.session_list.addItem(item)
            card = _SessionCard(
                record.id,
                title,
                f"{record.turn_count} 轮 · "
                f"{record.updated_at.astimezone():%m-%d %H:%M}",
                active=active,
            )
            card.activated.connect(self._activate_session_card)
            card.delete_requested.connect(self.session_delete_requested.emit)
            self.session_list.setItemWidget(item, card)
            if active:
                found_current = True
                self.session_list.setCurrentItem(item)
        if self._session_id and not found_current:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 62))
            item.setData(Qt.ItemDataRole.UserRole, self._session_id)
            self.session_list.insertItem(0, item)
            card = _SessionCard(
                self._session_id,
                "新会话",
                "尚无消息",
                active=True,
            )
            card.activated.connect(self._activate_session_card)
            card.delete_requested.connect(self.session_delete_requested.emit)
            self.session_list.setItemWidget(item, card)
            self.session_list.setCurrentItem(item)
        del blocker

    def set_session(self, session_id: str, turns, title: str = "") -> None:
        """显示当前会话及其完整持久化历史"""

        self._session_id = session_id
        self._messages = []
        for turn in turns:
            self._messages.append(("user", turn.user_text))
            self._messages.append(("assistant", turn.assistant_text))
        self._assistant_streaming = False
        self.set_assistant_typing(False)
        display_title = title.strip() or "新会话"
        if len(display_title) > 40:
            display_title = f"{display_title[:39]}…"
        self.chat_title.setText(display_title)
        self.session_title.setText("语音和文字共享当前会话上下文")
        self._render_history()

    def append_user_message(self, text: str) -> None:
        """把语音或手动输入追加到当前聊天记录"""

        normalized = text.strip()
        if not normalized:
            return
        self._assistant_streaming = False
        self._messages.append(("user", normalized))
        if self.chat_title.text() == "新会话":
            title = normalized if len(normalized) <= 40 else f"{normalized[:39]}…"
            self.chat_title.setText(title)
        self._render_history()

    def append_assistant_delta(self, text: str) -> None:
        """持续更新当前一条助手回复"""

        if not text:
            return
        if self._typing_active:
            self._typing_active = False
            self._typing_timer.stop()
        if self._assistant_streaming and self._messages:
            role, current = self._messages[-1]
            if role == "assistant":
                self._messages[-1] = (role, current + text)
                if self._streaming_body is not None:
                    updated = current + text
                    self._streaming_body.set_message(updated)
                    if self._streaming_bubble is not None:
                        self._resize_bubble(
                            self._streaming_bubble,
                            self._streaming_body,
                            updated,
                        )
                    self._scroll_to_bottom()
                    return
        self._messages.append(("assistant", text))
        self._assistant_streaming = True
        self._render_history()

    def finish_assistant_message(self) -> None:
        """结束当前流式助手消息"""

        self._assistant_streaming = False
        self._streaming_body = None
        self._streaming_bubble = None
        self.set_assistant_typing(False)

    def set_assistant_typing(self, active: bool) -> None:
        """显示或隐藏助手正在输入状态"""

        if self._typing_active == active:
            return
        self._typing_active = active
        self._typing_step = 0
        if active:
            self._typing_timer.start()
        else:
            self._typing_timer.stop()
        self._render_history()

    def set_submitting(self, active: bool) -> None:
        """提交期间避免重复发送"""

        self._submitting = active
        self._update_composer_state()

    def set_processing(self, active: bool) -> None:
        """在当前回复完成前锁定输入区域"""

        self._processing = active
        self._update_composer_state()

    def show_error(self, message: str) -> None:
        """恢复输入并显示安全错误"""

        self.set_submitting(False)
        self.error_label.setText(message)
        self.show()
        self.text_edit.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def submission_succeeded(self) -> None:
        """提交成功后清空输入并保持聊天窗口打开"""

        self._submitting = False
        self._processing = True
        self._update_composer_state()
        self.error_label.clear()
        self.text_edit.clear()

    def _submit(self) -> None:
        text = self.text_edit.text().strip()
        if not text:
            self.show_error("请输入内容后再发送")
            return
        self.error_label.clear()
        self.submitted.emit(text)

    def _activate_session_card(self, session_id: str) -> None:
        for index in range(self.session_list.count()):
            item = self.session_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == session_id:
                self.session_list.setCurrentItem(item)
                break
        if session_id != self._session_id:
            self.session_activate_requested.emit(session_id)

    def _render_history(self) -> None:
        while self._messages_layout.count():
            item = self._messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._streaming_body = None
        self._streaming_bubble = None
        self._typing_label = None
        if not self._messages and not self._typing_active:
            self._messages_layout.addStretch(1)
            empty = QLabel("这里还没有消息\n\n可以输入文字，也可以直接使用语音聊天")
            empty.setObjectName("empty_history")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._messages_layout.addWidget(empty)
            self._messages_layout.addStretch(1)
            return
        for index, (role, text) in enumerate(self._messages):
            row, body, bubble = self._message_row(role, text)
            self._messages_layout.addWidget(row)
            if index == len(self._messages) - 1 and role == "assistant":
                self._streaming_body = body
                self._streaming_bubble = bubble
        if self._typing_active:
            typing_row, self._typing_label = self._typing_row()
            self._messages_layout.addWidget(typing_row)
        self._messages_layout.addStretch(1)
        self._scroll_to_bottom()

    @staticmethod
    def _message_row(
        role: str,
        text: str,
    ) -> tuple[QWidget, _MarkdownMessageBody, QFrame]:
        row = QWidget()
        row.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 3, 0, 3)
        body = _MarkdownMessageBody(text)
        bubble = QFrame()
        bubble.setObjectName(
            "message_user" if role == "user" else "message_assistant"
        )
        ManualInputDialog._resize_bubble(bubble, body, text)
        bubble.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(13, 9, 13, 11)
        bubble_layout.setSpacing(4)
        sender = QLabel("你" if role == "user" else "VoicePet")
        sender.setObjectName(
            "message_user_name" if role == "user" else "message_assistant_name"
        )
        bubble_layout.addWidget(sender)
        bubble_layout.addWidget(body)
        if role == "user":
            row_layout.addStretch(1)
            row_layout.addWidget(bubble)
        else:
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)
        return row, body, bubble

    @staticmethod
    def _resize_bubble(
        bubble: QFrame,
        body: _MarkdownMessageBody,
        text: str,
    ) -> None:
        longest_line = max(text.splitlines() or [text], key=len)
        natural_width = QFontMetrics(body.font()).horizontalAdvance(
            longest_line
        ) + 36
        minimum_width = 180
        bubble.setFixedWidth(max(minimum_width, min(natural_width, 540)))
        body.set_content_width(bubble.width() - 28)

    @staticmethod
    def _typing_row() -> tuple[QWidget, QLabel]:
        row = QWidget()
        row.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 3, 0, 3)
        bubble = QFrame()
        bubble.setObjectName("message_assistant")
        bubble.setFixedWidth(180)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(13, 9, 13, 11)
        sender = QLabel("VoicePet")
        sender.setObjectName("message_assistant_name")
        typing = QLabel("正在输入")
        typing.setObjectName("typing_body")
        bubble_layout.addWidget(sender)
        bubble_layout.addWidget(typing)
        row_layout.addWidget(bubble)
        row_layout.addStretch(1)
        return row, typing

    def _animate_typing(self) -> None:
        if self._typing_label is None:
            return
        self._typing_step = (self._typing_step + 1) % 4
        self._typing_label.setText(f"正在输入{'.' * self._typing_step}")

    def _scroll_to_bottom(self) -> None:
        self._scroll_timer.start(0)

    def _apply_scroll_bottom(self) -> None:
        scroll = self.history_view.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    def _scroll_range_changed(self, minimum: int, maximum: int) -> None:
        del minimum
        self.history_view.verticalScrollBar().setValue(maximum)

    def _update_composer_state(self) -> None:
        busy = self._submitting or self._processing
        self.text_edit.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        if self._submitting:
            button_text = "发送中…"
        elif self._processing:
            button_text = "回复中…"
        else:
            button_text = "发送"
        self.send_button.setText(button_text)
        self.text_edit.setPlaceholderText(
            "等待当前回复完成…" if busy else "输入消息，按回车发送"
        )
        if not busy and self.isVisible():
            self.text_edit.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
