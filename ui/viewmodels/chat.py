"""聊天消息、会话列表和输入状态 ViewModel"""

from __future__ import annotations

import re
from collections.abc import Mapping
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
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

from core.agent_types import MAX_ATTACHMENTS
from core.attachments import Attachment, inspect_attachment
from core.config import default_config_path
from core.llm import LlmAttachment
from ui.markdown import sanitize_markdown

from .dialogs import ConfirmationRequest, DialogCoordinator

_INVALID_INDEX = QModelIndex()
MAX_ACTIVITY_OUTPUT_CHARS = 4_000


def _activity_message_id(activity_id: str, session_id: str = "") -> str:
    """执行条目的消息标识与 Codex 条目号一一对应"""

    return f"activity:{session_id}:{activity_id}" if session_id else f"activity:{activity_id}"


class ChatRuntimeProtocol(Protocol):
    def submit_text(
        self, text: str, attachments: tuple[LlmAttachment, ...] = ()
    ) -> Future[Any]: ...

    def cancel_active_turn(self, session_id: str = "") -> Future[None]: ...

    def list_sessions(self) -> Future[tuple[Any, ...]]: ...

    def activate_session(self, session_id: str) -> Future[tuple[Any, ...]]: ...

    def new_session(self) -> Future[str]: ...

    def list_memory_changes(
        self, session_id: str | None = None
    ) -> Future[tuple[Any, ...]]: ...

    def undo_memory(self, change_id: str) -> Future[bool]: ...

    def mark_memory_changes_viewed(self, change_ids: tuple[str, ...]) -> Future[int]: ...

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

    def row_of(self, message_id: str) -> int | None:
        """按消息标识定位条目，流式增量据此并入同一条"""

        if not message_id:
            return None
        for row, item in enumerate(self._items):
            if item.get("messageId") == message_id:
                return row
        return None

    def update_item(self, message_id: str, changes: dict[str, object]) -> bool:
        row = self.row_of(message_id)
        if row is None:
            return False
        self._items[row].update(changes)
        index = self.index(row)
        self.dataChanged.emit(index, index, list(self._roles))
        return True

    def update_row(self, row: int, changes: dict[str, object]) -> None:
        """会话列表按行更新运行状态，不依赖消息标识"""

        if not 0 <= row < len(self._items):
            return
        self._items[row].update(changes)
        index = self.index(row)
        self.dataChanged.emit(index, index, list(self._roles))

    def move_to_end(self, message_id: str) -> None:
        """占位气泡被真实回复接管时移到末尾，执行过程条目仍留在回复之前"""

        row = self.row_of(message_id)
        if row is None or row == len(self._items) - 1:
            return
        self.beginMoveRows(QModelIndex(), row, row, QModelIndex(), len(self._items))
        self._items.append(self._items.pop(row))
        self.endMoveRows()

    def item_at(self, row: int) -> dict[str, object] | None:
        """按行读取条目，用于判断条目当前状态"""

        if not 0 <= row < len(self._items):
            return None
        return self._items[row]

    def finish_streaming(self, status: str = "complete") -> None:
        """一轮结束时统一收尾仍在流式的条目，多条目流式不能只看最后一条"""

        for row, item in enumerate(self._items):
            if item.get("status") == "streaming":
                item["status"] = status
                index = self.index(row)
                self.dataChanged.emit(index, index, list(self._roles))

    @property
    def last(self) -> dict[str, object] | None:
        return self._items[-1] if self._items else None


class ChatMessageModel(_RoleListModel):
    """向 QML 发布已清理的聊天消息"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            (
                "messageId",
                "role",
                "markdown",
                "status",
                "createdAt",
                "attachments",
                "activityId",
                "activityKind",
                "activityTitle",
                "activityOutput",
                "createdLabel",
            ),
            parent,
        )


class SessionListModel(_RoleListModel):
    """向 QML 发布会话摘要"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(
            (
                "sessionId",
                "title",
                "turnCount",
                "updatedAt",
                "active",
                "group",
                "running",
                "updatedLabel",
            ),
            parent,
        )

    def mark_running(self, active: set[str]) -> None:
        """按会话运行状态刷新各行，用于区分后台仍在执行的会话"""

        for row, item in enumerate(self._items):
            self.update_row(
                row, {"running": str(item.get("sessionId", "")) in active}
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
    folderPathPasted = Signal(str)
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
        # 会话可以并行运行，处理状态和消息条目都必须按会话隔离
        self._session_items: dict[str, list[dict[str, object]]] = {}
        self._processing: set[str] = set()
        self._active_session_id = ""
        self._session_loading = False
        self._pending_session_id = ""
        self._pending_switch_id = ""
        self._memory_query_session = ""
        self._memory_session_id = ""
        self._memory_query_epoch = 0
        self._memory_action_busy = False
        self._memory_undo_session = ""
        self._memory_action_result = ""
        self._voice_recording = False
        self._message_playing = False
        self._agent_mode = "auto_edit"
        self._agent_mode_pending = False
        self._assistant_buffers: dict[tuple[str, str], str] = {}
        self._pending_assistant_id: dict[str, str] = {}
        self._activity_kinds: dict[tuple[str, str], str] = {}
        self._activity_titles: dict[tuple[str, str], str] = {}
        self._activity_outputs: dict[tuple[str, str], str] = {}
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
        change_ids: list[str] = []
        for row in range(self._memory_changes_model.rowCount()):
            change_id = self._memory_changes_model.data(
                self._memory_changes_model.index(row), Qt.UserRole + 1
            )
            self._viewed_memory_changes.add((self._memory_session(), str(change_id)))
            change_ids.append(str(change_id))
        self._memory_changes_model.reset_items([])
        self._memory_action_result = ""
        self.memoryChangesChanged.emit()
        self.memoryActionResultChanged.emit()
        self._persist_viewed_changes(tuple(change_ids))
        self.viewMemoryRequested.emit()

    def _persist_viewed_changes(self, change_ids: tuple[str, ...]) -> None:
        """把已查看状态写回存储，重启后不再重复提示同一批变更"""

        if not change_ids:
            return
        try:
            self._watch("memory-viewed", self._runtime.mark_memory_changes_viewed(change_ids))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return

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
        self._agent_mode_pending = self.processing
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
        if self._voice_recording or self.processing or self._session_loading:
            if self.processing or self._session_loading:
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
        except (RuntimeError, ValueError, TypeError) as error:
            self._voice_recording = False
            self.voiceRecordingChanged.emit()
            # 麦克风缺失等已知原因使用运行时给出的安全说明
            self.errorOccurred.emit(
                getattr(error, "safe_message", "") or "语音输入启动失败，请稍后重试"
            )

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

    @Slot(int, result=bool)
    def paste_attachments(self, existing: int = 0) -> bool:
        """接收剪贴板中的本地文件或截图，普通文字交回编辑器处理"""

        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData() if clipboard is not None else None
        if mime is None:
            return False
        urls = [url for url in mime.urls() if url.isLocalFile()]
        try:
            if urls:
                self._paste_local_paths(urls, existing)
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

    def _paste_local_paths(self, urls: list[QUrl], existing: int) -> None:
        """文件按剩余额度加入附件，文件夹改以绝对路径插入输入框"""

        remaining = max(MAX_ATTACHMENTS - max(existing, 0), 0)
        items: list[Attachment] = []
        folders: list[str] = []
        skipped = 0
        for url in urls:
            local = Path(url.toLocalFile())
            if local.is_dir():
                folders.append(str(local.resolve()))
                continue
            if len(items) >= remaining:
                skipped += 1
                continue
            items.append(inspect_attachment(local))
        for item in items:
            self.attachmentPasted.emit(QUrl.fromLocalFile(item.path).toString(), item.kind)
        for folder in folders:
            self.folderPathPasted.emit(folder)
        if skipped:
            self.errorOccurred.emit(
                f"最多只能添加 {MAX_ATTACHMENTS} 个附件，已忽略 {skipped} 个文件"
            )

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
        """当前显示的会话是否正在回复"""

        return self._active_session_id in self._processing

    @Property(bool, notify=processingChanged)
    def anyProcessing(self) -> bool:
        """任意会话仍在运行，删除会话一类全局操作据此判定"""

        return bool(self._processing)

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
        if self._session_loading or self.processing:
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
        if len(normalized_attachments) > MAX_ATTACHMENTS:
            self.errorOccurred.emit(f"最多只能添加 {MAX_ATTACHMENTS} 个附件")
            return
        if not normalized and normalized_attachments:
            self.errorOccurred.emit("请先输入文字后再发送附件")
            return
        if not normalized and not normalized_attachments:
            return
        session_id = self._active_session_id
        self._append_message(
            session_id,
            "user",
            normalized or "已附加文件",
            "complete",
            attachments=_attachment_view_items(normalized_attachments),
        )
        self._pending_assistant_id[session_id] = self._append_message(
            session_id, "assistant", "", "streaming"
        )
        self.scrollToLatestRequested.emit()
        self._set_processing(session_id, True)
        try:
            try:
                future = self._runtime.submit_text(normalized, normalized_attachments)
            except TypeError:
                future = self._runtime.submit_text(normalized)
            # 发送归属发起时所在的会话，切走以后失败仍落到同一条消息上
            self._watch(f"submit:{session_id}", future)
        except (RuntimeError, ValueError, TypeError):
            self._update_last(session_id, {"status": "error"})
            self._set_processing(session_id, False)
            self.errorOccurred.emit("消息发送失败，请稍后重试")

    @Slot(str)
    def begin_turn(self, session_id: str = "") -> None:
        # 语音等待首段回复时也属于处理中，禁止把旧轮回复切入其他会话
        self._set_processing(session_id, True)

    @Slot()
    def stop_generation(self) -> None:
        session_id = self._active_session_id
        if session_id not in self._processing:
            return
        try:
            self._watch("cancel", self._runtime.cancel_active_turn(session_id))
        except RuntimeError:
            self.errorOccurred.emit("当前操作无法停止")
        self._finish_streaming(session_id, "stopped")
        self._finish_activities(session_id, "stopped")
        self._set_processing(session_id, False)

    @Slot(str, str)
    def append_user_message(self, text: str, session_id: str = "") -> None:
        if isinstance(text, str) and text.strip():
            self._append_message(session_id, "user", text.strip(), "complete")
            self.scrollToLatestRequested.emit()

    @Slot(str, str, str)
    def append_assistant_delta(self, text: str, item_id: str = "", session_id: str = "") -> None:
        """每个 Codex 消息条目单独成条，没有条目号的旧链路沿用最后一条流式回复"""

        if not isinstance(text, str) or not text:
            return
        message_id = f"agent:{item_id}" if item_id else ""
        if not message_id:
            last = self._last_item(session_id)
            if (
                last is None
                or last.get("role") != "assistant"
                or last.get("status") != "streaming"
            ):
                self._append_message(session_id, "assistant", text, "streaming")
            else:
                combined = str(last.get("markdown", "")) + text
                self._update_last(session_id, {"markdown": sanitize_markdown(combined)})
            self._set_processing(session_id, True)
            return
        key = (session_id, message_id)
        buffered = self._assistant_buffers.get(key, "") + text
        self._assistant_buffers[key] = buffered
        markdown = sanitize_markdown(buffered)
        if not self._update_item(session_id, message_id, {"markdown": markdown}):
            # 首段真实回复接管发送时插入的占位气泡，避免留下空条目
            pending = self._pending_assistant_id.get(session_id, "")
            if pending and self._update_item(
                session_id, pending, {"messageId": message_id, "markdown": markdown}
            ):
                # 回复接管占位气泡时移到末尾，执行过程条目留在回复之前
                self._move_to_end(session_id, message_id)
            else:
                self._append_message(
                    session_id, "assistant", buffered, "streaming", message_id=message_id
                )
            self._pending_assistant_id.pop(session_id, None)
        self._set_processing(session_id, True)

    @Slot(str)
    def finish_assistant(self, session_id: str = "") -> None:
        self._finish_streaming(session_id, "complete")
        self._finish_activities(session_id, "stopped")
        for key in [key for key in self._assistant_buffers if key[0] == session_id]:
            del self._assistant_buffers[key]
        self._pending_assistant_id.pop(session_id, None)
        self._set_processing(session_id, False)
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

    @Slot(str, str, str, str)
    def start_agent_activity(
        self, activity_id: str, kind: str, title: str, session_id: str = ""
    ) -> None:
        """每个工具调用在聊天记录里单独成条，后续输出按标识原地更新"""

        if not isinstance(activity_id, str) or not activity_id:
            return
        message_id = _activity_message_id(activity_id, session_id)
        if self._row_of(session_id, message_id) is not None:
            return
        key = (session_id, activity_id)
        normalized_kind = (
            kind
            if isinstance(kind, str) and kind in {"command", "file", "tool", "thinking"}
            else "tool"
        )
        self._activity_kinds[key] = normalized_kind
        default_title = (
            "正在思考" if normalized_kind == "thinking" else "Agent 正在执行操作"
        )
        self._activity_titles[key] = (
            title.strip()[:400] if isinstance(title, str) and title.strip() else default_title
        )
        self._activity_outputs.setdefault(key, "")
        self._append_message(
            session_id,
            "activity",
            "",
            "running",
            message_id=message_id,
            activity=self._activity_fields(session_id, activity_id),
        )
        self._set_processing(session_id, True)

    @Slot(str, str, str)
    def append_agent_activity(
        self, activity_id: str, text: str, session_id: str = ""
    ) -> None:
        """执行输出按尾部保留，超长命令不会把整条消息撑爆"""

        if not isinstance(activity_id, str) or not activity_id or not isinstance(text, str) or not text:
            return
        key = (session_id, activity_id)
        if key not in self._activity_outputs:
            self.start_agent_activity(activity_id, "command", "正在执行命令", session_id)
        buffered = (self._activity_outputs.get(key, "") + text)[-MAX_ACTIVITY_OUTPUT_CHARS:]
        self._activity_outputs[key] = buffered
        if not self._update_item(
            session_id,
            _activity_message_id(activity_id, session_id),
            {"activityOutput": buffered},
        ):
            return
        self._set_processing(session_id, True)

    @Slot(str, str, str)
    def finish_agent_activity(
        self, activity_id: str, status: str, session_id: str = ""
    ) -> None:
        if not isinstance(activity_id, str) or not activity_id:
            return
        final = {"completed": "complete", "failed": "failed", "stopped": "stopped"}.get(
            status if isinstance(status, str) else "", "complete"
        )
        self._update_item(
            session_id,
            _activity_message_id(activity_id, session_id),
            {"status": final},
        )

    def _finish_activities(self, session_id: str, status: str) -> None:
        """轮次结束或中断时收尾仍在执行中的条目，已有终态的条目保持原状"""

        for key in tuple(self._activity_outputs):
            if key[0] == session_id and self._activity_status(session_id, key[1]) == "running":
                self.finish_agent_activity(key[1], status, session_id)
        for store in (
            self._activity_kinds,
            self._activity_titles,
            self._activity_outputs,
        ):
            for key in [key for key in store if key[0] == session_id]:
                del store[key]

    def _activity_status(self, session_id: str, activity_id: str) -> str:
        """读取执行条目的当前状态，避免结束轮次时改写已有终态"""

        message_id = _activity_message_id(activity_id, session_id)
        model = self._model_for(session_id)
        if model is not None:
            row = model.row_of(message_id)
            item = model.item_at(row) if row is not None else None
            return str(item.get("status", "")) if item is not None else ""
        for item in self._items(session_id):
            if item.get("messageId") == message_id:
                return str(item.get("status", ""))
        return ""

    def _activity_fields(self, session_id: str, activity_id: str) -> dict[str, object]:
        key = (session_id, activity_id)
        return {
            "activityId": activity_id,
            "activityKind": self._activity_kinds.get(key, "tool"),
            "activityTitle": self._activity_titles.get(key, ""),
            "activityOutput": self._activity_outputs.get(key, ""),
        }

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
        """记忆页只切换它自己的会话范围，不改变聊天正在显示的会话"""

        self._memory_session_id = session_id
        self.activeSessionIdChanged.emit()
        self._memory_query_epoch += 1
        self._memory_action_result = ""
        self.memoryActionResultChanged.emit()
        self._memory_changes_model.reset_items([])
        self.memoryChangesChanged.emit()
        self.refresh_memory_changes()

    def _memory_session(self) -> str:
        """记忆变更查询默认跟随当前聊天会话"""

        return self._memory_session_id or self._active_session_id

    @Property(str, notify=activeSessionIdChanged)
    def memorySessionId(self) -> str:
        """记忆页当前作用的会话，后台记忆事件据此判断是否刷新提示"""

        return self._memory_session()

    @Slot()
    def refresh_memory_changes(self) -> None:
        session_id = self._memory_session()
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
            self._memory_undo_session = self._memory_session()
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
        # 其它会话可以在后台继续运行，切换本身不再被回复阻塞
        if self._session_loading:
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
        if self._session_loading:
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
        # 删除会关闭 Agent 线程持有者，必须等所有会话的轮次都结束
        if self.anyProcessing or self._session_loading:
            self.errorOccurred.emit("会话运行结束后才能删除会话")
            return
        if self._dialogs is None or session_id == "":
            self.errorOccurred.emit("会话删除当前不可用")
            return

        def remove() -> None:
            # 确认等待期间可能开始新的回复，执行前再次检查
            if self.anyProcessing or self._session_loading:
                self.errorOccurred.emit("会话运行结束后才能删除会话")
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
            if operation.startswith("submit:"):
                session_id = operation.split(":", 1)[1]
                self._update_last(session_id, {"status": "error"})
                self._set_processing(session_id, False)
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
            self._sync_session_running()
            self.sessionsChanged.emit()
        elif operation.startswith("submit:"):
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
            target = self._pending_switch_id or self._active_session_id
            self._load_turns(tuple(result or ()), target)
            self._show_session(target)
            self._pending_switch_id = ""
            self.refresh_memory_changes()
            self._set_session_loading(False)
            self.refresh_sessions()
        elif operation.startswith("agent_mode_load:"):
            session_id = operation.split(":", 1)[1]
            if session_id != self._active_session_id or result not in {"suggest", "auto_edit", "full_auto"}:
                return
            self._agent_mode = str(result)
            self._agent_mode_pending = self.processing
            self.agentModeChanged.emit()
        elif operation in {"agent_mode_global", "agent_mode_default"}:
            if operation == "agent_mode_default":
                reader = getattr(self._runtime, "global_agent_mode", None)
                if callable(reader):
                    self._watch("agent_mode_global", reader())
                return
            if result in {"suggest", "auto_edit", "full_auto"}:
                self._agent_mode = str(result)
                self._agent_mode_pending = self.processing
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
            self._pending_session_id = str(result)
            self._session_items[self._pending_session_id] = []
            self._show_session(self._pending_session_id)
            self.refresh_memory_changes()
            self._set_session_loading(False)
            self.refresh_sessions()
        elif operation.startswith("memory-changes:"):
            parts = operation.split(":")
            session_id = parts[1]
            epoch = int(parts[2])
            if (
                session_id != self._memory_session()
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
                    if not bool(_read(change, "undone", False))
                    and not bool(_read(change, "viewed", False))
                    and (session_id, str(_read(change, "id", "")))
                    not in self._viewed_memory_changes
                ]
            )
            self.memoryChangesChanged.emit()
        elif operation == "memory-undo":
            self._memory_action_busy = False
            if self._memory_undo_session != self._memory_session():
                self.memoryActionResultChanged.emit()
                return
            self._memory_action_result = (
                "已撤销这项记忆变更" if result is True else "这项变更已无法撤销"
            )
            self.memoryActionResultChanged.emit()
            self.refresh_memory_changes()

    def _load_turns(self, turns: tuple[object, ...], session_id: str) -> None:
        """只在会话还没有内存条目时用归档重建，避免覆盖正在运行的内容"""

        if session_id and session_id in self._session_items:
            return
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
        self._session_items[session_id] = items

    def _show_session(self, session_id: str) -> None:
        """把模型切换到目标会话的条目列表，其它会话继续在后台累积"""

        self._active_session_id = session_id
        self._memory_session_id = ""
        self._message_model.reset_items(self._items(session_id))
        self.activeSessionIdChanged.emit()
        self.processingChanged.emit()
        self._sync_session_running()

    def _items(self, session_id: str) -> list[dict[str, object]]:
        return self._session_items.setdefault(session_id, [])

    def _model_for(self, session_id: str) -> ChatMessageModel | None:
        """只有当前显示的会话需要驱动 QML 模型"""

        return self._message_model if session_id == self._active_session_id else None

    def _row_of(self, session_id: str, message_id: str) -> int | None:
        model = self._model_for(session_id)
        if model is not None:
            return model.row_of(message_id)
        for row, item in enumerate(self._items(session_id)):
            if item.get("messageId") == message_id:
                return row
        return None

    def _update_item(
        self, session_id: str, message_id: str, changes: dict[str, object]
    ) -> bool:
        model = self._model_for(session_id)
        if model is not None:
            return model.update_item(message_id, changes)
        row = self._row_of(session_id, message_id)
        if row is None:
            return False
        self._items(session_id)[row].update(changes)
        return True

    def _update_last(self, session_id: str, changes: dict[str, object]) -> None:
        model = self._model_for(session_id)
        if model is not None:
            model.update_last(changes)
            return
        items = self._items(session_id)
        if items:
            items[-1].update(changes)

    def _move_to_end(self, session_id: str, message_id: str) -> None:
        """真实回复接管占位气泡后移到末尾，执行过程条目留在回复之前"""

        model = self._model_for(session_id)
        if model is not None:
            model.move_to_end(message_id)
            return
        items = self._items(session_id)
        row = self._row_of(session_id, message_id)
        if row is not None and row != len(items) - 1:
            items.append(items.pop(row))

    def _last_item(self, session_id: str) -> dict[str, object] | None:
        model = self._model_for(session_id)
        if model is not None:
            return model.last
        items = self._items(session_id)
        return items[-1] if items else None

    def _finish_streaming(self, session_id: str, status: str) -> None:
        model = self._model_for(session_id)
        if model is not None:
            model.finish_streaming(status)
            return
        for item in self._items(session_id):
            if item.get("status") == "streaming":
                item["status"] = status

    def _append_message(
        self,
        session_id: str,
        role: str,
        text: str,
        status: str,
        *,
        attachments: list[dict[str, object]] | None = None,
        message_id: str | None = None,
        activity: dict[str, object] | None = None,
    ) -> str:
        resolved = message_id or str(uuid4())
        item = _message_item(
            resolved,
            role,
            text,
            status,
            _iso(datetime.now(UTC)),
            attachments=attachments,
        )
        if activity:
            item.update(activity)
        model = self._model_for(session_id)
        if model is not None:
            model.append_item(item)
        else:
            self._items(session_id).append(item)
        return resolved

    def _set_processing(self, session_id: str, processing: bool) -> None:
        active = session_id in self._processing
        if processing == active:
            return
        if processing:
            self._processing.add(session_id)
        else:
            self._processing.discard(session_id)
        if not processing and not self._processing and self._agent_mode_pending:
            self._agent_mode_pending = False
            self.agentModeChanged.emit()
        self.processingChanged.emit()
        self._sync_session_running()

    def _sync_session_running(self) -> None:
        """会话列表用运行标记区分后台仍在执行的会话"""

        self._session_model.mark_running(self._processing)

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
        "createdLabel": _time_label(created_at),
        "attachments": list(attachments or []),
        "activityId": "",
        "activityKind": "",
        "activityTitle": "",
        "activityOutput": "",
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
    updated_at = _iso(updated)
    return {
        "sessionId": str(_read(record, "id", "")),
        "title": str(_read(record, "title", "未命名会话"))[:120],
        "turnCount": int(_read(record, "turn_count", 0)),
        "updatedAt": updated_at,
        "updatedLabel": _time_label(updated_at),
        "active": bool(_read(record, "is_active", False)),
        "group": "最近",
        "running": False,
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


_MESSAGE_TIME_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})$")


def _time_label(value: object) -> str:
    """聊天时间用今天/昨天加时刻展示，跨年才补上年份"""

    text = str(value)
    match = _MESSAGE_TIME_PATTERN.match(text)
    if match is None:
        return text
    stamp, clock = match.groups()
    today = datetime.now(UTC).astimezone().date()
    if stamp == today.isoformat():
        return f"今天 {clock}"
    if stamp == (today - timedelta(days=1)).isoformat():
        return f"昨天 {clock}"
    return f"{stamp[5:]} {clock}" if stamp[:4] == f"{today.year:04d}" else f"{stamp} {clock}"
