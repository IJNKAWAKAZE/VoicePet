"""设置草稿、统一验证和即时设置 ViewModel"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from core.asr import AsrModelState
from core.config import AppConfig, ConfigError
from core.credentials import CredentialError
from core.tts import TtsVoiceOption
from core.wake_models import WakeModelState


class ConfigStoreProtocol(Protocol):
    def save(self, config: AppConfig) -> None: ...


class CredentialStoreProtocol(Protocol):
    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> bool: ...


class SettingsRuntimeProtocol(Protocol):
    def invalidate_memory(self) -> Future[None]: ...

    def list_tts_voices(self) -> Future[tuple[TtsVoiceOption, ...]]: ...

    def preview_tts_voice(self, voice_name: str) -> Future[None]: ...

    def asr_model_state(self) -> Future[AsrModelState]: ...

    def download_asr_model(self) -> Future[None]: ...

    def wake_model_state(self) -> Future[WakeModelState]: ...

    def download_wake_model(self, progress: object) -> Future[None]: ...


class SettingsViewModel(QObject):
    """把高频即时设置与需要正式保存的草稿分开"""

    draftChanged = Signal()
    draftRestored = Signal()
    configChanged = Signal()
    fieldErrorsChanged = Signal()
    saved = Signal()
    errorOccurred = Signal(str)
    statusMessageChanged = Signal()
    operationBusyChanged = Signal()
    voiceOptionsChanged = Signal()
    voicesLoadingChanged = Signal()
    voicesErrorChanged = Signal()
    wakeModelStatusChanged = Signal()
    wakeModelNoticeChanged = Signal()
    _wakeModelCheckFinished = Signal(int, object, object)
    _voicesFinished = Signal(object, object)
    _operationFinished = Signal(str, object, object)
    _memoryInvalidationFinished = Signal(int, bool)

    _IMMEDIATE_UI_FIELDS = frozenset(
        {
            "theme_id",
            "reduce_motion",
            "pet_scale",
            "pet_click_through",
            "preferred_screen",
            "always_on_top",
            "active_skin",
            "start_at_login",
            "global_hotkey_enabled",
            "global_hotkey",
        }
    )

    def __init__(
        self,
        config: AppConfig,
        store: ConfigStoreProtocol,
        *,
        credential_store: CredentialStoreProtocol | None = None,
        runtime: SettingsRuntimeProtocol | None = None,
        data_directory: str | Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._store = store
        self._credential_store = credential_store
        self._runtime = runtime
        self._running_asr_model = config.asr.model
        self._data_directory = Path(data_directory).resolve() if data_directory is not None else None
        self._voice_options: list[dict[str, str]] = []
        self._voices_loading = False
        self._voices_error = ""
        self._draft = config.to_dict()
        self._field_errors: dict[str, str] = {}
        self._last_field = ""
        self._status_message = ""
        self._operation_busy = False
        self._status_context = ""
        self._status_generation = 0
        self._operation_generation = 0
        self._voices_generation = 0
        self._wake_model_status = "unknown"
        self._wake_model_generation = 0
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(6000)
        self._status_timer.timeout.connect(lambda: self._set_status(""))
        self._operationFinished.connect(self._handle_operation_finished)
        self._voicesFinished.connect(self._handle_voices_finished)
        self._memoryInvalidationFinished.connect(self._handle_memory_invalidation)
        self.draftChanged.connect(self.voiceOptionsChanged.emit)
        self.draftChanged.connect(self.wakeModelNoticeChanged.emit)
        self._wakeModelCheckFinished.connect(self._handle_wake_model_check)

    @property
    def config(self) -> AppConfig:
        return self._config

    @Property("QVariantMap", notify=draftChanged)
    def draft(self) -> dict[str, Any]:
        return deepcopy(self._draft)

    @Property(bool, notify=draftChanged)
    def hasDraftChanges(self) -> bool:
        return self._draft != self._config.to_dict()

    @Property("QVariantList", notify=draftChanged)
    def asrModelOptions(self) -> list[str]:
        models = ["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "large-v3-turbo"]
        current = str(self.draft_value("asr", "model") or "")
        if current and current not in models:
            models.insert(0, current)
        return models

    @Property(str, notify=draftChanged)
    def asrDownloadNote(self) -> str:
        if self.draft_value("asr", "model") != self._running_asr_model:
            return f"当前运行模型为 {self._running_asr_model}；切换模型后请保存设置并重启，再下载所选模型"
        return f"下载当前运行模型：{self._running_asr_model}"

    @Slot(str)
    def set_status_context(self, category: str) -> None:
        if category != self._status_context:
            self._status_context = category
            self._status_generation += 1
            self._set_status("")

    @Property(str, notify=statusMessageChanged)
    def status_message(self) -> str:
        return self._status_message

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @Property(bool, notify=operationBusyChanged)
    def operation_busy(self) -> bool:
        return self._operation_busy

    @Property(bool, notify=operationBusyChanged)
    def operationBusy(self) -> bool:
        return self._operation_busy

    @Property(str, constant=True)
    def dataDirectory(self) -> str:
        return str(self._data_directory) if self._data_directory is not None else ""

    @Slot()
    def open_data_directory(self) -> None:
        if self._data_directory is None:
            self._set_status("数据目录当前不可用")
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._data_directory)))
        except (OSError, RuntimeError):
            opened = False
        self._set_status("已打开数据目录" if opened else "打开数据目录失败，请稍后重试")

    @Property("QVariantList", notify=voiceOptionsChanged)
    def voiceOptions(self) -> list[dict[str, str]]:
        options = [dict(option) for option in self._voice_options]
        selected = str(self.draft_value("tts", "voice") or "")
        if selected and not any(option["short_name"] == selected for option in options):
            options.insert(0, {"label": selected, "short_name": selected})
        return options

    @Property(bool, notify=voicesLoadingChanged)
    def voicesLoading(self) -> bool:
        return self._voices_loading

    @Property(str, notify=voicesErrorChanged)
    def voicesError(self) -> str:
        return self._voices_error

    @Slot()
    def refresh_voices(self) -> None:
        if self._voices_loading:
            return
        self._voices_generation = self._status_generation
        self._set_status("正在刷新声音列表…", in_progress=True)
        self._voices_error = ""
        self.voicesErrorChanged.emit()

        if self._runtime is None:
            self._handle_voices_finished(None, RuntimeError("运行时不可用"))
            return
        self._voices_loading = True
        self.voicesLoadingChanged.emit()
        try:
            future = self._runtime.list_tts_voices()
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError) as error:
            self._handle_voices_finished(None, error)
            return

        def done(completed: Future[Any]) -> None:
            try:
                result = completed.result()
            except Exception as error:  # noqa: BLE001
                self._voicesFinished.emit(None, error)
                return
            self._voicesFinished.emit(result, None)

        future.add_done_callback(done)

    @Property(str, notify=wakeModelStatusChanged)
    def wakeModelStatus(self) -> str:
        return self._wake_model_status

    @Property(str, notify=wakeModelNoticeChanged)
    def wakeModelNotice(self) -> str:
        if not self.draft_value("wake_word", "enabled"):
            return ""
        return {
            "unknown": "尚未检查中文唤醒模型，请检查模型状态",
            "checking": "正在检查中文唤醒模型…",
            "missing": "尚未下载中文唤醒模型，语音唤醒暂不可用，请点击下方按钮下载",
            "cached": "中文唤醒模型包已下载，等待安装并加载",
            "downloading": "正在下载中文唤醒模型，完成后将刷新状态…",
            "error": "中文唤醒模型检查失败或下载失败，请重试",
        }.get(self._wake_model_status, "")

    def _set_wake_model_status(self, status: str) -> None:
        self._wake_model_status = status
        self.wakeModelStatusChanged.emit()
        self.wakeModelNoticeChanged.emit()

    @Slot()
    def refresh_wake_model(self) -> None:
        if self._wake_model_status in {"checking", "downloading"}:
            return
        self._wake_model_generation += 1
        generation = self._wake_model_generation
        self._set_wake_model_status("checking")
        try:
            future = self._runtime.wake_model_state()
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError) as error:
            self._handle_wake_model_check(generation, None, error)
            return

        def done(completed: Future[Any]) -> None:
            if completed.cancelled():
                self._wakeModelCheckFinished.emit(
                    generation, None, RuntimeError("模型检查已取消")
                )
                return
            error = completed.exception()
            result = completed.result() if error is None else None
            self._wakeModelCheckFinished.emit(generation, result, error)

        future.add_done_callback(done)

    @Slot(int, object, object)
    def _handle_wake_model_check(
        self, generation: int, result: object, error: object
    ) -> None:
        if generation != self._wake_model_generation:
            return
        if error is not None or not isinstance(result, str) or result not in {
            "missing", "cached", "ready",
        }:
            self._set_wake_model_status("error")
            return
        self._set_wake_model_status(result)

    @Slot(object, object)
    def _handle_voices_finished(self, result: object, error: object) -> None:
        self._voices_loading = False
        self.voicesLoadingChanged.emit()
        if error is not None:
            self._voices_error = "声音列表加载失败，可重试；当前声音仍可使用"
        else:
            self._voice_options = [
                {"label": voice.label, "short_name": voice.short_name}
                for voice in result
            ]
            self._voices_error = "" if self._voice_options else "暂无可用声音，当前声音仍可使用"
            self.voiceOptionsChanged.emit()
        self.voicesErrorChanged.emit()
        self._set_status(
            self._voices_error or f"声音列表刷新完成，共 {len(self._voice_options)} 个声音",
            generation=self._voices_generation,
        )

    def bind_services(
        self,
        *,
        credential_store: CredentialStoreProtocol | None = None,
        runtime: SettingsRuntimeProtocol | None = None,
    ) -> None:
        """在正式 Runtime 建立后补齐设置操作边界"""

        self._credential_store = credential_store
        self._runtime = runtime
        self._running_asr_model = self._config.asr.model
        self._wake_model_generation += 1
        self._set_wake_model_status("unknown")
        self.draftChanged.emit()

    @Property("QVariantMap", notify=fieldErrorsChanged)
    def field_errors(self) -> Mapping[str, str]:
        return dict(self._field_errors)

    @Property("QVariantMap", notify=fieldErrorsChanged)
    def fieldErrors(self) -> Mapping[str, str]:
        return self.field_errors

    def begin_edit(self, config: AppConfig) -> None:
        self._config = config
        self._draft = config.to_dict()
        self._clear_errors()
        self.configChanged.emit()
        self.draftChanged.emit()
        self.draftRestored.emit()

    # 使用 QVariant 接收 QML 的字符串、布尔值和数值
    @Slot(str, str, "QVariant")
    def set_field(self, section: str, field: str, value: object) -> None:
        section_data = self._draft.get(section)
        if not isinstance(section_data, dict) or field not in section_data:
            self._set_error(f"{section}.{field}", "设置字段不存在")
            return
        self._last_field = f"{section}.{field}"
        if section == "ui" and field in self._IMMEDIATE_UI_FIELDS:
            self._save_immediate(field, value)
            return
        if section_data[field] == value:
            return
        section_data[field] = value
        self._remove_error(self._last_field)
        self.draftChanged.emit()
        if section == "wake_word" and field == "enabled" and value is True:
            self.refresh_wake_model()

    @Slot(str, str, result="QVariant")
    def draft_value(self, section: str, field: str) -> object:
        section_data = self._draft.get(section, {})
        return section_data.get(field) if isinstance(section_data, dict) else None

    @Slot()
    def save_draft(self) -> None:
        try:
            candidate = AppConfig.from_dict(deepcopy(self._draft))
        except ConfigError:
            field = self._last_field or "config"
            self._set_error(field, "此设置值无效")
            return
        try:
            self._store.save(candidate)
        except (ConfigError, OSError):
            self._set_status("设置保存失败，请稍后重试")
            self.errorOccurred.emit("设置保存失败，请稍后重试")
            return
        self._config = candidate
        self._draft = candidate.to_dict()
        self._clear_errors()
        self.configChanged.emit()
        self.draftChanged.emit()
        self.saved.emit()
        self._set_status("设置已保存")

    @Slot()
    def discard_draft(self) -> None:
        self._draft = self._config.to_dict()
        self._clear_errors()
        self.draftChanged.emit()
        self.draftRestored.emit()
        self._set_status("未保存的更改已放弃，即时设置保持生效")

    @Slot(str)
    def save_api_key(self, value: str) -> None:
        normalized = value.strip() if isinstance(value, str) else ""
        if not normalized:
            self._set_status("请输入 API Key")
            return
        if self._credential_store is None:
            self._set_status("加密凭据存储当前不可用")
            return
        try:
            self._credential_store.set("openai_api_key", normalized)
        except (CredentialError, OSError, RuntimeError):
            self._set_status("API Key 加密保存失败")
            return
        self._set_status("API Key 已加密保存，重启 VoicePet 后生效")
        self._invalidate_memory_credentials()

    @Slot()
    def delete_api_key(self) -> None:
        if self._credential_store is None:
            self._set_status("加密凭据存储当前不可用")
            return
        try:
            deleted = self._credential_store.delete("openai_api_key")
        except (CredentialError, OSError, RuntimeError):
            self._set_status("API Key 移除失败")
            return
        self._set_status(
            "API Key 已移除，重启 VoicePet 后生效"
            if deleted
            else "未保存 API Key"
        )
        if deleted:
            self._invalidate_memory_credentials()

    def _invalidate_memory_credentials(self) -> None:
        if self._runtime is None:
            return
        generation = self._status_generation
        try:
            future = self._runtime.invalidate_memory()
        except (AttributeError, RuntimeError):
            self._handle_memory_invalidation(generation, False)
            return

        def done(completed: Future[Any]) -> None:
            try:
                completed.result()
            except Exception:  # noqa: BLE001 凭据变更失败不把服务异常正文带入界面
                self._memoryInvalidationFinished.emit(generation, False)
            else:
                self._memoryInvalidationFinished.emit(generation, True)

        future.add_done_callback(done)

    @Slot(int, bool)
    def _handle_memory_invalidation(self, generation: int, succeeded: bool) -> None:
        if not succeeded:
            self._set_status("旧服务的记忆整理暂停失败，请退出并重启", generation=generation)

    @Slot(str)
    def preview_voice(self, voice_name: str) -> None:
        normalized = voice_name.strip() if isinstance(voice_name, str) else ""
        if normalized:
            self._start_operation(
                "preview",
                lambda: self._runtime.preview_tts_voice(normalized),
            )

    @Slot()
    def download_asr_model(self) -> None:
        if self.draft_value("asr", "model") != self._running_asr_model:
            self._set_status(self.asrDownloadNote)
            return
        self._start_operation("asr_check", lambda: self._runtime.asr_model_state())

    @Slot()
    def download_wake_model(self) -> None:
        self._start_operation(
            "wake_check",
            lambda: self._runtime.wake_model_state(),
        )

    def _save_immediate(self, field: str, value: object) -> None:
        data = self._config.to_dict()
        data["ui"][field] = value
        key = f"ui.{field}"
        try:
            candidate = AppConfig.from_dict(data)
            self._store.save(candidate)
        except (ConfigError, OSError):
            self._set_error(key, "此设置值无效或无法保存")
            return
        self._config = candidate
        draft_ui = self._draft["ui"]
        assert isinstance(draft_ui, dict)
        draft_ui[field] = value
        self._remove_error(key)
        self.configChanged.emit()
        self.draftChanged.emit()

    def _start_operation(
        self,
        operation: str,
        starter: Callable[[], Future[Any]],
    ) -> None:
        if self._operation_busy:
            self._set_status("已有操作正在进行，请稍候", in_progress=True)
            return
        if self._runtime is None or not callable(starter):
            self._set_status("运行时服务当前不可用")
            return
        self._set_operation_busy(True)
        self._operation_generation = self._status_generation
        self._submit_operation(operation, starter)

    def _submit_operation(self, operation: str, starter: Callable[[], Future[Any]]) -> None:
        if operation in {"wake_check", "wake"}:
            self._wake_model_generation += 1
            self._set_wake_model_status(
                "checking" if operation == "wake_check" else "downloading"
            )
        messages = {
            "preview": "正在试听声音…",
            "asr_check": f"正在检查语音识别模型 {self._running_asr_model}…",
            "wake_check": "正在检查中文唤醒模型…",
            "asr": f"正在下载语音识别模型 {self._running_asr_model}…",
            "wake": "正在下载中文唤醒模型…",
        }
        self._set_operation_status(messages[operation], in_progress=True)
        try:
            future = starter()
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError):
            if operation in {"wake_check", "wake"}:
                self._set_wake_model_status("error")
            self._set_operation_busy(False)
            self._set_operation_status("操作未能提交，请稍后重试")
            return

        def done(completed: Future[Any]) -> None:
            try:
                result = completed.result()
            except Exception as error:  # noqa: BLE001
                self._operationFinished.emit(operation, None, error)
                return
            self._operationFinished.emit(operation, result, None)

        future.add_done_callback(done)

    @Slot(str, object, object)
    def _handle_operation_finished(self, operation: str, result: object, error: object) -> None:
        if error is not None:
            if operation in {"wake_check", "wake"}:
                self._set_wake_model_status("error")
            self._set_operation_busy(False)
            self._set_operation_status("操作失败，请检查网络或模型状态")
            return
        if operation in {"asr_check", "wake_check"}:
            if operation == "wake_check":
                self._handle_wake_model_check(self._wake_model_generation, result, None)
            if result in {"cached", "ready"}:
                self._set_operation_busy(False)
                name = (
                    f"语音识别模型 {self._running_asr_model}"
                    if operation == "asr_check" else "中文唤醒模型"
                )
                self._set_operation_status(f"{name}已下载，无需重复下载")
            elif result == "missing":
                if operation == "asr_check":
                    self._submit_operation("asr", lambda: self._runtime.download_asr_model())
                else:
                    self._submit_operation(
                        "wake", lambda: self._runtime.download_wake_model(lambda progress: None)
                    )
            else:
                self._set_operation_busy(False)
                self._set_operation_status("模型状态检查失败，请稍后重试")
            return
        self._set_operation_busy(False)
        messages = {
            "preview": "语音试听完成",
            "asr": f"语音识别模型 {self._running_asr_model} 已准备完成",
            "wake": "唤醒模型已准备完成",
        }
        self._set_operation_status(messages.get(operation, "操作已完成"))
        if operation == "wake":
            self._set_wake_model_status("unknown")
            self.refresh_wake_model()

    def _set_operation_busy(self, value: bool) -> None:
        if value != self._operation_busy:
            self._operation_busy = value
            self.operationBusyChanged.emit()

    def _set_operation_status(self, message: str, *, in_progress: bool = False) -> None:
        self._set_status(message, generation=self._operation_generation, in_progress=in_progress)

    def _set_status(
        self, message: str, *, generation: int | None = None, in_progress: bool = False,
    ) -> None:
        if generation is not None and generation != self._status_generation:
            return
        self._status_timer.stop()
        if message and not in_progress:
            self._status_timer.start()
        if message != self._status_message:
            self._status_message = message
            self.statusMessageChanged.emit()

    def _set_error(self, field: str, message: str) -> None:
        self._field_errors[field] = message
        self.fieldErrorsChanged.emit()
        self._set_status(message)

    def _remove_error(self, field: str) -> None:
        if field in self._field_errors:
            self._field_errors.pop(field)
            self.fieldErrorsChanged.emit()

    def _clear_errors(self) -> None:
        if self._field_errors:
            self._field_errors.clear()
            self.fieldErrorsChanged.emit()
