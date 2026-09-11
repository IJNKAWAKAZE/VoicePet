"""聊天消息、会话列表和输入状态 ViewModel"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtQml import QJSValue

from core.attachments import inspect_attachment
from core.config import default_config_path
from core.llm import LlmAttachment
from ui.markdown import sanitize_markdown

from .dialogs import ConfirmationRequest, DialogCoordinator

_INVALID_INDEX = QModelIndex()


class ChatRuntimeProtocol(Protocol):
    def submit_text(
        self, text: str, attachments: tuple[LlmAttachment, ...] = ()
    ) -> Future[Any]: ...

    def cancel_active_turn(self) -> Future[None]: ...

    def list_sessions(self) -> Future[tuple[Any, ...]]: ...

    def activate_session(self, session_id: str) -> Future[tuple[Any, ...]]: ...

    def new_session(self) -> Future[str]: ...

    def list_memory_changes(
        self, session_id: str | None = None
    ) -> Future[tuple[Any, ...]]: ...

    def undo_memory(self, change_id: str) -> Future[bool]: ...

    def session_agent_mode(self, session_id: str) -> Future[str]: ...

    def global_agent_mode(self) -> Future[str]: ...


class _RoleListModel(QAbstractListModel):
    def __init__(self, roles: tuple[str, ...], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._roles = {
            Qt.UserRole + offset: name.encode("utf-8")
            for offset, name in enumerate(roles, start=1)
        }
        self._items: list[dict[str, object]] = []

    def roleNames(self) -> dict[int, bytes]:
        return self._roles

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        name = self._roles.get(role)
        if name is None:
            return None
        return self._items[index.row()].get(name.decode("utf-8"))

    def reset_items(self, items: list[dict[str, object]]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def append_item(self, item: dict[str, object]) -> None:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(item)
        self.endInsertRows()

    def update_last(self, changes: dict[str, object]) -> None:
        if not self._items:
            return
        row = len(self._items) - 1
        self._items[row].update(changes)
        index = self.index(row)
        self.dataChanged.emit(index, index, list(self._roles))

    @property
    def last(self) -> dict[str, object] | None:
        return self._items[-1] if self._items else None


class ChatMessageModel(_RoleListModel):
    """向 QML 发布已清理的聊天消息"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            ("messageId", "role", "markdown", "status", "createdAt", "attachments"), parent
        )


class SessionListModel(_RoleListModel):
    """向 QML 发布会话摘要"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            ("sessionId", "title", "turnCount", "updatedAt", "active", "group"),
            parent,
        )


class ChatMemoryChangeModel(_RoleListModel):
    """独立于消息历史的当前会话记忆变更"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            ("changeId", "memoryId", "kind", "createdAt", "undone"), parent
        )


class ChatViewModel(QObject):
    """把 Runtime Future 结果安全转回 Qt 主线程"""

    processingChanged = Signal()
    activeSessionIdChanged = Signal()
    errorOccurred = Signal(str)
    sessionsChanged = Signal()
    sessionLoadingChanged = Signal()
    memoryChangesChanged = Signal()
    memoryActionResultChanged = Signal()
    viewMemoryRequested = Signal()
    transcriptReady = Signal(str)
    voiceRecordingChanged = Signal()
    messagePlayingChanged = Signal()
    submissionAccepted = Signal()
    scrollToLatestRequested = Signal()
    agentModeChanged = Signal()
    attachmentPasted = Signal(str, str)
    _futureFinished = Signal(str, object, object)

    def __init__(
        self,
        runtime: ChatRuntimeProtocol,
        parent: QObject | None = None,
        *,
        dialogs: DialogCoordinator | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._dialogs = dialogs
        self._message_model = ChatMessageModel(self)
        self._session_model = SessionListModel(self)
        self._memory_changes_model = ChatMemoryChangeModel(self)
        self._viewed_memory_changes: set[tuple[str, str]] = set()
        self._processing = False
        self._active_session_id = ""
        self._session_loading = False
        self._pending_session_id = ""
        self._pending_switch_id = ""
        self._memory_query_session = ""
        self._memory_query_epoch = 0
        self._memory_action_busy = False
        self._memory_undo_session = ""
        self._memory_action_result = ""
        self._voice_recording = False
        self._message_playing = False
        self._agent_mode = "auto_edit"
        self._agent_mode_pending = False
        self._futureFinished.connect(self._handle_future)

    @Property(QObject, constant=True)
    def message_model(self) -> ChatMessageModel:
        return self._message_model

    @Property(QObject, constant=True)
    def messageModel(self) -> ChatMessageModel:
        return self._message_model

    @Property(QObject, constant=True)
    def session_model(self) -> SessionListModel:
        return self._session_model

    @Property(QObject, constant=True)
    def sessionModel(self) -> SessionListModel:
        return self._session_model

    @Property(QObject, constant=True)
    def memoryChangesModel(self) -> ChatMemoryChangeModel:
        return self._memory_changes_model

    @Property(int, notify=memoryChangesChanged)
    def memoryChangeCount(self) -> int:
        return self._memory_changes_model.rowCount()

    @Property(str, notify=memoryActionResultChanged)
    def memoryActionResult(self) -> str:
        return self._memory_action_result

    @Property(bool, notify=memoryActionResultChanged)
    def memoryActionBusy(self) -> bool:
        return self._memory_action_busy

    @Slot()
    def open_memory(self) -> None:
        # 查看仅收起已展示的提示，实际记忆和撤销记录仍保留在记忆页面
        for row in range(self._memory_changes_model.rowCount()):
            change_id = self._memory_changes_model.data(
                self._memory_changes_model.index(row), Qt.UserRole + 1
            )
            self._viewed_memory_changes.add((self._active_session_id, str(change_id)))
        self._memory_changes_model.reset_items([])
        self._memory_action_result = ""
        self.memoryChangesChanged.emit()
        self.memoryActionResultChanged.emit()
        self.viewMemoryRequested.emit()

    @Property(bool, notify=voiceRecordingChanged)
    def voiceRecording(self) -> bool:
        return self._voice_recording

    @Property(bool, notify=messagePlayingChanged)
    def messagePlaying(self) -> bool:
        return self._message_playing

    @Property(str, notify=agentModeChanged)
    def agentMode(self) -> str:
        return self._agent_mode

    @Property(str, notify=agentModeChanged)
    def agentModeLabel(self) -> str:
        return {"suggest": "建议模式", "auto_edit": "自动编辑", "full_auto": "全自动"}.get(self._agent_mode, "自动编辑")

    @Property(bool, notify=agentModeChanged)
    def agentModePending(self) -> bool:
        return self._agent_mode_pending

    @Property("QVariantList", constant=True)
    def agentModeOptions(self) -> list[dict[str, str]]:
        return [
            {"value": "suggest", "label": "建议模式", "description": "命令和文件变更逐次确认", "icon": "◉"},
            {"value": "auto_edit", "label": "自动编辑", "description": "普通文件编辑自动执行", "icon": "✎"},
            {"value": "full_auto", "label": "全自动", "description": "执行前不弹出 VoicePet 确认", "icon": "⚡"},
        ]

    @Slot(str)
    def set_agent_mode(self, mode: str) -> None:
        if mode not in {"suggest", "auto_edit", "full_auto"}:
            self.errorOccurred.emit("Agent 模式无效")
            return
        setter = getattr(self._runtime, "set_global_agent_mode", None)
        if callable(setter):
            try:
                self._watch("agent_mode", setter(mode))
            except (RuntimeError, TypeError, ValueError):
                self.errorOccurred.emit("Agent 模式切换失败")
                return
        self._agent_mode = mode
        self._agent_mode_pending = self._processing
        self.agentModeChanged.emit()

    @Slot()
    def use_default_agent_mode(self) -> None:
        setter = getattr(self._runtime, "set_global_agent_mode", None)
        if callable(setter):
            self._watch("agent_mode_default", setter(None))

    @Slot()
    def load_global_agent_mode(self) -> None:
        """应用启动后读取持久化的全局 Agent 模式"""
        reader = getattr(self._runtime, "global_agent_mode", None)
        if callable(reader):
            try:
                self._watch("agent_mode_global", reader())
            except (RuntimeError, TypeError, ValueError):
                self.errorOccurred.emit("Agent 模式读取失败")

    def _load_agent_mode(self) -> None:
        """切换会话后重新读取会话覆盖和应用默认值"""
        reader = getattr(self._runtime, "session_agent_mode", None)
        if not callable(reader) or not self._active_session_id:
            return
        session_id = self._active_session_id
        try:
            self._watch(f"agent_mode_load:{session_id}", reader(session_id))
        except (RuntimeError, TypeError, ValueError):
            self.errorOccurred.emit("Agent 模式读取失败")

    @Slot()
    def start_voice_input(self) -> None:
        if self._voice_recording or self._processing or self._session_loading:
            if self._processing or self._session_loading:
                self.errorOccurred.emit("当前回复完成后才能录音")
            return
        starter = getattr(self._runtime, "capture_manual_transcript", None)
        if not callable(starter):
            self.errorOccurred.emit("语音输入当前不可用")
            return
        try:
            self._voice_recording = True
            self.voiceRecordingChanged.emit()
            self.scrollToLatestRequested.emit()
            self._watch("voice_input", starter())
        except (RuntimeError, ValueError, TypeError):
            self._voice_recording = False
            self.voiceRecordingChanged.emit()
            self.errorOccurred.emit("语音输入启动失败，请稍后重试")

    @Slot(str)
    def copy_message(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            self.errorOccurred.emit("复制失败")
            return
        clipboard.setText(text)
        if self._dialogs is not None:
            self._dialogs.toast("已复制", "success")

    @Slot(result=bool)
    def paste_attachments(self) -> bool:
        """接收剪贴板中的本地文件或截图，普通文字交回编辑器处理"""

        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData() if clipboard is not None else None
        if mime is None:
            return False
        urls = [url for url in mime.urls() if url.isLocalFile()]
        try:
            if urls:
                items = [inspect_attachment(url.toLocalFile()) for url in urls]
                for item in items:
                    self.attachmentPasted.emit(QUrl.fromLocalFile(item.path).toString(), item.kind)
                return True
            if not mime.hasImage():
                return False
            screenshot = clipboard.image()
            if screenshot.isNull():
                raise ValueError("剪贴板图片无效")
            # 截图保存为独立文件，使发送和历史预览不依赖剪贴板后续内容
            directory = default_config_path().parent / "cache" / "clipboard"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"粘贴图片-{uuid4().hex}.png"
            if not screenshot.save(str(path), "PNG"):
                raise OSError("剪贴板图片保存失败")
            self.attachmentPasted.emit(QUrl.fromLocalFile(str(path)).toString(), "image")
        except (OSError, ValueError, RuntimeError):
            self.errorOccurred.emit("无法粘贴附件，请确认文件可读取或重新复制图片")
        return True

    @Slot(str)
    def open_attachment(self, path: str) -> None:
        """使用系统默认程序打开附件"""

        if not isinstance(path, str) or not path.strip():
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except (OSError, RuntimeError, TypeError):
            opened = False
        if not opened:
            self.errorOccurred.emit("文件打开失败，请确认文件仍然存在")

    @Slot(str)
    def play_message(self, text: str) -> None:
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized:
            return
        if self._message_playing:
            self.stop_message_playback()
            return
        speaker = getattr(self._runtime, "speak_notice", None)
        if not callable(speaker):
            self.errorOccurred.emit("语音播放当前不可用")
            return
        try:
            self._message_playing = True
            self.messagePlayingChanged.emit()
            self._watch("play_message", speaker(normalized))
        except (RuntimeError, ValueError, TypeError):
            self._message_playing = False
            self.messagePlayingChanged.emit()
            self.errorOccurred.emit("语音播放失败，请稍后重试")

    @Slot()
    def stop_message_playback(self) -> None:
        if not self._message_playing:
            return
        try:
            self._watch("stop_message_playback", self._runtime.cancel_active_turn())
        except (AttributeError, RuntimeError, TypeError):
            pass
        self._message_playing = False
        self.messagePlayingChanged.emit()

    @Property(bool, notify=processingChanged)
    def processing(self) -> bool:
        return self._processing

    @Property(str, notify=activeSessionIdChanged)
    def active_session_id(self) -> str:
        return self._active_session_id

    @Property(str, notify=activeSessionIdChanged)
    def activeSessionId(self) -> str:
        return self._active_session_id

    @Property(bool, notify=sessionLoadingChanged)
    def session_loading(self) -> bool:
        return self._session_loading

    @Property(bool, notify=sessionLoadingChanged)
    def sessionLoading(self) -> bool:
        return self._session_loading

    @Slot(str, "QVariant")
    def submit(self, text: str, attachments: object = ()) -> None:
        if self._session_loading or self._processing:
            self.errorOccurred.emit("请等待当前会话加载或回复完成")
            return
        normalized = text.strip() if isinstance(text, str) else ""
        normalized_attachments: list[LlmAttachment] = []
        try:
            for item in _attachment_items(attachments):
                if isinstance(item, LlmAttachment):
                    normalized_attachments.append(item)
                    continue
                if not isinstance(item, Mapping):
                    raise TypeError("附件格式无效")
                raw_path = item.get("path", "")
                if hasattr(raw_path, "toLocalFile"):
                    raw_path = raw_path.toLocalFile()
                inspected = inspect_attachment(str(raw_path))
                normalized_attachments.append(
                    LlmAttachment(
                        inspected.path,
                        inspected.name,
                        inspected.media_type,
                        inspected.size,
                        inspected.sha256,
                        inspected.kind,
                    )
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            self.errorOccurred.emit("附件不可读取，请重新选择文件")
            return
        normalized_attachments = tuple(normalized_attachments)
        if not normalized and normalized_attachments:
            self.errorOccurred.emit("请先输入文字后再发送附件")
            return
        if not normalized and not normalized_attachments:
            return
        self._append_message(
            "user",
            normalized or "已附加文件",
            "complete",
            attachments=_attachment_view_items(normalized_attachments),
        )
        self._append_message("assistant", "", "streaming")
        self.scrollToLatestRequested.emit()
        self._set_processing(True)
        try:
            try:
                future = self._runtime.submit_text(normalized, normalized_attachments)
            except TypeError:
                future = self._runtime.submit_text(normalized)
            self._watch("submit", future)
        except (RuntimeError, ValueError, TypeError):
            self._message_model.update_last({"status": "error"})
            self._set_processing(False)
            self.errorOccurred.emit("消息发送失败，请稍后重试")

    @Slot()
    def begin_turn(self) -> None:
        # 语音等待首段回复时也属于处理中，禁止把旧轮回复切入其他会话
        self._set_processing(True)

    @Slot()
    def stop_generation(self) -> None:
        if not self._processing:
            return
        try:
            self._watch("cancel", self._runtime.cancel_active_turn())
        except RuntimeError:
            self.errorOccurred.emit("当前操作无法停止")
        self._message_model.update_last({"status": "stopped"})
        self._set_processing(False)

    @Slot(str)
    def append_user_message(self, text: str) -> None:
        if isinstance(text, str) and text.strip():
            self._append_message("user", text.strip(), "complete")
            self.scrollToLatestRequested.emit()

    @Slot(str)
    def append_assistant_delta(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            return
        last = self._message_model.last
        if (
            last is None
            or last.get("role") != "assistant"
            or last.get("status") != "streaming"
        ):
            self._append_message("assistant", text, "streaming")
        else:
            combined = str(last.get("markdown", "")) + text
            self._message_model.update_last({"markdown": sanitize_markdown(combined)})
        self._set_processing(True)

    @Slot()
    def finish_assistant(self) -> None:
        last = self._message_model.last
        if last is not None and last.get("role") == "assistant":
            self._message_model.update_last({"status": "complete"})
        self._set_processing(False)
        self.refresh_sessions()

    @Slot(str, str, "QVariantMap")
    def append_tool_result(
        self,
        tool_call_id: str,
        status: str,
        payload: Mapping[str, object],
    ) -> None:
        """只把工具返回的安全摘要加入聊天记录"""

        normalized_status = (
            status
            if status
            in {
                "success",
                "denied",
                "cancelled",
                "timeout",
                "failed",
                "partial",
            }
            else "failed"
        )
        raw_message = payload.get("message") if isinstance(payload, Mapping) else None
        if isinstance(raw_message, str) and raw_message.strip():
            message = raw_message.strip()[:400]
        else:
            labels = {
                "success": "工具操作已完成",
                "denied": "工具操作未获允许",
                "cancelled": "工具操作已取消",
                "timeout": "工具操作已超时",
                "partial": "工具操作已部分完成",
                "failed": "工具操作未完成",
            }
            message = labels[normalized_status]
        safe_id = str(tool_call_id).strip()[:128] or str(uuid4())
        self._message_model.append_item(
            _message_item(
                f"tool:{safe_id}",
                "tool",
                message,
                normalized_status,
                _iso(datetime.now(UTC)),
            )
        )

    @Slot()
    def refresh_sessions(self) -> None:
        try:
            self._watch("sessions", self._runtime.list_sessions())
        except RuntimeError:
            self.errorOccurred.emit("会话列表加载失败")

    @Slot()
    def restore_current_session(self) -> None:
        self._set_session_loading(True)
        try:
            self._watch("current", self._runtime.current_session_id())
        except (AttributeError, RuntimeError):
            self._set_session_loading(False)
            self.errorOccurred.emit("当前会话读取失败")

    @Slot(str)
    def set_active_session_for_memory(self, session_id: str) -> None:
        """同步当前会话并从持久存储恢复独立变更记录"""

        self._active_session_id = session_id
        self._memory_query_epoch += 1
        self._memory_action_result = ""
        self.memoryActionResultChanged.emit()
        self._memory_changes_model.reset_items([])
        self.memoryChangesChanged.emit()
        self.refresh_memory_changes()

    @Slot()
    def refresh_memory_changes(self) -> None:
        session_id = self._active_session_id
        if not session_id:
            self._memory_changes_model.reset_items([])
            self.memoryChangesChanged.emit()
            return
        self._memory_query_session = session_id
        self._memory_query_epoch += 1
        epoch = self._memory_query_epoch
        try:
            self._watch(
                f"memory-changes:{session_id}:{epoch}",
                self._runtime.list_memory_changes(session_id),
            )
        except (AttributeError, RuntimeError, ValueError):
            self._memory_action_result = "记忆记录加载失败，请重试"
            self.memoryActionResultChanged.emit()
            return

    @Slot(str)
    def undo_memory_change(self, change_id: str) -> None:
        if not change_id:
            return
        try:
            self._memory_undo_session = self._active_session_id
            self._memory_action_busy = True
            self._memory_action_result = ""
            self.memoryActionResultChanged.emit()
            self._watch("memory-undo", self._runtime.undo_memory(change_id))
        except (AttributeError, RuntimeError, ValueError):
            self._memory_action_busy = False
            self._memory_action_result = "撤销请求失败，请重试"
            self.memoryActionResultChanged.emit()
            return

    @Slot(str)
    def activate_session(self, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            return
        if self._processing or self._session_loading:
            self.errorOccurred.emit("当前回复完成后才能切换会话")
            return
        self._set_session_loading(True)
        self._pending_switch_id = session_id
        try:
            self._watch("turns", self._runtime.activate_session(session_id))
        except (RuntimeError, ValueError):
            self._set_session_loading(False)
            self.errorOccurred.emit("会话打开失败")

    @Slot()
    def new_session(self) -> None:
        if self._processing or self._session_loading:
            self.errorOccurred.emit("当前回复完成后才能新建会话")
            return
        self._set_session_loading(True)
        try:
            self._watch("new", self._runtime.new_session())
        except RuntimeError:
            self._set_session_loading(False)
            self.errorOccurred.emit("新会话创建失败")

    @Slot(str)
    def request_delete_session(self, session_id: str) -> None:
        self._confirm_session_removal(session_id)

    @Slot()
    def request_clear_sessions(self) -> None:
        self._confirm_session_removal(None)

    def _confirm_session_removal(self, session_id: str | None) -> None:
        if self._processing or self._session_loading:
            self.errorOccurred.emit("当前回复完成后才能删除会话")
            return
        if self._dialogs is None or session_id == "":
            self.errorOccurred.emit("会话删除当前不可用")
            return

        def remove() -> None:
            # 确认等待期间可能开始新的回复，执行前再次检查
            if self._processing or self._session_loading:
                self.errorOccurred.emit("当前回复完成后才能删除会话")
                return
            try:
                future = (
                    self._runtime.clear_sessions()
                    if session_id is None
                    else self._runtime.delete_session(session_id)
                )
                self._set_session_loading(True)
                self._watch("delete", future)
            except (RuntimeError, ValueError):
                self._set_session_loading(False)
                self.errorOccurred.emit("会话删除失败")

        self._dialogs.confirm(
            ConfirmationRequest(
                f"delete-session:{uuid4()}",
                "清空全部会话？" if session_id is None else "删除这段会话？",
                "删除后无法恢复，会同时删除会话相关的近期摘要，长期记忆不受影响",
                "仅删除会话历史，保留长期记忆、设置和桌宠形象",
                False,
                "本地数据",
                show_details=False,
                confirm_label="清空" if session_id is None else "删除",
            ),
            remove,
        )

    def _watch(self, operation: str, future: Future[Any]) -> None:
        def done(completed: Future[Any]) -> None:
            try:
                self._futureFinished.emit(operation, completed.result(), None)
            except Exception as error:  # noqa: BLE001
                self._futureFinished.emit(operation, None, error)

        future.add_done_callback(done)

    @Slot(str, object, object)
    def _handle_future(self, operation: str, result: object, error: object) -> None:
        if error is not None:
            if operation.startswith("memory-"):
                if operation.startswith("memory-changes:"):
                    self._memory_action_result = "记忆记录加载失败，请重试"
                    self.memoryActionResultChanged.emit()
                if operation == "memory-undo":
                    self._memory_action_busy = False
                    self._memory_action_result = "撤销失败，请重试"
                    self.memoryActionResultChanged.emit()
                return
            if operation in {"turns", "current", "new", "delete"}:
                self._set_session_loading(False)
                if operation == "turns":
                    self._pending_switch_id = ""
            if operation == "submit":
                self._message_model.update_last({"status": "error"})
                self._set_processing(False)
            if operation == "voice_input":
                self._voice_recording = False
                self.voiceRecordingChanged.emit()
            if operation in {"play_message", "stop_message_playback"}:
                self._message_playing = False
                self.messagePlayingChanged.emit()
            self.errorOccurred.emit("操作失败，请稍后重试")
            return
        if operation == "sessions":
            sessions = tuple(result or ())
            items = [_session_item(item) for item in sessions]
            if self._pending_session_id and not any(
                item["sessionId"] == self._pending_session_id for item in items
            ):
                items.insert(
                    0,
                    _session_item(
                        {
                            "id": self._pending_session_id,
                            "title": "新会话",
                            "turn_count": 0,
                            "is_active": True,
                        }
                    ),
                )
            self._session_model.reset_items(items)
            self.sessionsChanged.emit()
        elif operation == "submit":
            # 后台归档完成后刷新侧栏，让新会话立即可见
            self.submissionAccepted.emit()
            self.refresh_sessions()
        elif operation == "voice_input":
            self._voice_recording = False
            self.voiceRecordingChanged.emit()
            if isinstance(result, str) and result.strip():
                self.transcriptReady.emit(result.strip())
        elif operation in {"play_message", "stop_message_playback"}:
            self._message_playing = False
            self.messagePlayingChanged.emit()
        elif operation == "delete":
            self._pending_session_id = ""
            self._pending_switch_id = ""
            # 删除后的当前会话和消息恢复完成前保持输入锁定
            self.restore_current_session()
            self.refresh_sessions()
            if self._dialogs is not None:
                self._dialogs.toast("会话历史已更新", "success")
        elif operation == "turns":
            self._load_turns(tuple(result or ()))
            if self._pending_switch_id:
                self._active_session_id = self._pending_switch_id
                self.activeSessionIdChanged.emit()
                self.refresh_memory_changes()
                self._pending_switch_id = ""
            self._set_session_loading(False)
            self.refresh_sessions()
        elif operation.startswith("agent_mode_load:"):
            session_id = operation.split(":", 1)[1]
            if session_id != self._active_session_id or result not in {"suggest", "auto_edit", "full_auto"}:
                return
            self._agent_mode = str(result)
            self._agent_mode_pending = self._processing
            self.agentModeChanged.emit()
        elif operation in {"agent_mode_global", "agent_mode_default"}:
            if operation == "agent_mode_default":
                reader = getattr(self._runtime, "global_agent_mode", None)
                if callable(reader):
                    self._watch("agent_mode_global", reader())
                return
            if result in {"suggest", "auto_edit", "full_auto"}:
                self._agent_mode = str(result)
                self._agent_mode_pending = self._processing
                self.agentModeChanged.emit()
        elif operation == "current":
            session_id = str(result or "")
            if session_id:
                self._pending_switch_id = session_id
                self._set_session_loading(True)
                try:
                    self._watch("turns", self._runtime.list_session_turns(session_id))
                except (AttributeError, RuntimeError, ValueError):
                    self._pending_switch_id = ""
                    self._set_session_loading(False)
                    self.errorOccurred.emit("会话历史读取失败")
            else:
                self._set_session_loading(False)
        elif operation == "new":
            self._active_session_id = str(result)
            self._pending_session_id = self._active_session_id
            self.activeSessionIdChanged.emit()
            self._message_model.reset_items([])
            self.refresh_memory_changes()
            self._set_session_loading(False)
            self.refresh_sessions()
        elif operation.startswith("memory-changes:"):
            parts = operation.split(":")
            session_id = parts[1]
            epoch = int(parts[2])
            if (
                session_id != self._active_session_id
                or epoch != self._memory_query_epoch
            ):
                return
            self._memory_changes_model.reset_items(
                [
                    {
                        "changeId": str(_read(change, "id", "")),
                        "memoryId": str(_read(change, "memory_id", "")),
                        "kind": str(_read(change, "kind", "")),
                        "createdAt": _iso(_read(change, "created_at", "")),
                        "undone": bool(_read(change, "undone", False)),
                    }
                    for change in tuple(result or ())
                    if (session_id, str(_read(change, "id", "")))
                    not in self._viewed_memory_changes
                ]
            )
            self.memoryChangesChanged.emit()
        elif operation == "memory-undo":
            self._memory_action_busy = False
            if self._memory_undo_session != self._active_session_id:
                self.memoryActionResultChanged.emit()
                return
            self._memory_action_result = (
                "已撤销这项记忆变更" if result is True else "这项变更已无法撤销"
            )
            self.memoryActionResultChanged.emit()
            self.refresh_memory_changes()

    def _load_turns(self, turns: tuple[object, ...]) -> None:
        items: list[dict[str, object]] = []
        for turn in turns:
            created_at = _iso(_read(turn, "created_at", datetime.now(UTC)))
            turn_id = str(_read(turn, "id", uuid4()))
            raw_attachments = _read(turn, "attachments", ())
            items.extend(
                (
                    _message_item(
                        f"{turn_id}:user",
                        "user",
                        _read(turn, "user_text", ""),
                        "complete",
                        created_at,
                        attachments=_attachment_view_items(raw_attachments),
                    ),
                    _message_item(
                        f"{turn_id}:assistant",
                        "assistant",
                        _read(turn, "assistant_text", ""),
                        "complete",
                        created_at,
                    ),
                )
            )
        self._message_model.reset_items(items)

    def _append_message(
        self,
        role: str,
        text: str,
        status: str,
        *,
        attachments: list[dict[str, object]] | None = None,
    ) -> None:
        self._message_model.append_item(
            _message_item(
                str(uuid4()),
                role,
                text,
                status,
                _iso(datetime.now(UTC)),
                attachments=attachments,
            )
        )

    def _set_processing(self, processing: bool) -> None:
        if processing == self._processing:
            return
        self._processing = processing
        if not processing and self._agent_mode_pending:
            self._agent_mode_pending = False
            self.agentModeChanged.emit()
        self.processingChanged.emit()

    def _set_session_loading(self, loading: bool) -> None:
        if loading == self._session_loading:
            return
        self._session_loading = loading
        self.sessionLoadingChanged.emit()


def _message_item(
    message_id: str,
    role: str,
    markdown: object,
    status: str,
    created_at: str,
    *,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "messageId": message_id,
        "role": role,
        "markdown": sanitize_markdown(str(markdown)),
        "status": status,
        "createdAt": created_at,
        "attachments": list(attachments or []),
    }


def _attachment_view_items(
    attachments: object,
) -> list[dict[str, object]]:
    """生成 QML 只读展示所需的附件元数据"""

    items: list[dict[str, object]] = []
    for attachment in _attachment_items(attachments):
        if isinstance(attachment, LlmAttachment):
            path = str(attachment.path)
            name = str(attachment.name)
            media_type = str(attachment.media_type)
            kind = str(attachment.kind)
        elif isinstance(attachment, Mapping):
            path = str(attachment.get("path", ""))
            name = str(attachment.get("name", "附件"))
            media_type = str(attachment.get("mediaType", attachment.get("media_type", "")))
            kind = str(attachment.get("kind", "file"))
            if not path:
                continue
        else:
            continue
        # QML Image 使用 file URL 才能稳定加载 Windows 本地文件
        try:
            from pathlib import Path

            url = Path(path).resolve().as_uri()
        except (OSError, ValueError):
            url = path
        items.append(
            {
                "name": name,
                "path": path,
                "url": url,
                "mediaType": media_type,
                "kind": kind,
            }
        )
    return items


def _attachment_items(value: object) -> tuple[object, ...]:
    """兼容 QML var、QVariantList 和 Python 附件序列"""

    if isinstance(value, QJSValue):
        if value.isNull() or value.isUndefined():
            return ()
        value = value.toVariant()
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("附件格式无效")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError("附件格式无效") from error


def _session_item(record: object) -> dict[str, object]:
    updated = _read(record, "updated_at", datetime.now(UTC))
    return {
        "sessionId": str(_read(record, "id", "")),
        "title": str(_read(record, "title", "未命名会话"))[:120],
        "turnCount": int(_read(record, "turn_count", 0)),
        "updatedAt": _iso(updated),
        "active": bool(_read(record, "is_active", False)),
        "group": "最近",
    }


def _read(source: object, name: str, default: object) -> object:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _iso(value: object) -> str:
    return (
        value.astimezone().strftime("%Y-%m-%d %H:%M")
        if isinstance(value, datetime)
        else str(value)
    )
