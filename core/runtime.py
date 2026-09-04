"""在独立 asyncio 线程中管理 VoicePet Runtime 生命周期"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .asr import AsrModelState
from .config import WakeWordConfig
from .event_bus import EventBus
from .events import TurnId
from .policy import ConfirmationMode
from .wake import WakeRuntimeStatus
from .wake_models import WakeDownloadProgress, WakeModelState

ResultT = TypeVar("ResultT")


class RuntimeHostError(RuntimeError):
    """Runtime 工作线程启动、调度或关闭失败"""

    code = "runtime.host"


class AsyncLifecycle(Protocol):
    """Runtime 可异步启动和停止的服务边界"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class CoordinatorService(Protocol):
    """RuntimeHost 使用的 Coordinator 生命周期边界"""

    async def stop(self) -> None: ...

    async def approve_pending(self, mode: ConfirmationMode) -> None: ...

    async def reject_pending(self) -> None: ...

    async def speak_notice(self, text: str) -> TurnId: ...

    async def submit_text(self, text: str) -> TurnId: ...

    async def set_speech_enabled(self, enabled: bool) -> None: ...


class ActivationService(Protocol):
    """RuntimeHost 使用的统一激活边界"""

    async def activate(self, source: str) -> TurnId: ...


class MemoryDataService(Protocol):
    """RuntimeHost 使用的同步记忆数据管理边界"""

    def list_records(self) -> tuple[Any, ...]: ...

    def delete(self, memory_id: str) -> bool: ...

    def confirm(self, memory_id: str) -> Any: ...

    def resolve_conflict(self, memory_id: str) -> Any: ...

    def export_json(self, destination: str) -> int: ...


class SessionDataService(Protocol):
    """RuntimeHost 使用的同步会话数据管理边界"""

    def list_records(self) -> tuple[Any, ...]: ...

    @property
    def current_session_id(self) -> str: ...

    def list_turns(self, session_id: str) -> tuple[Any, ...]: ...

    def activate(self, session_id: str) -> tuple[Any, ...]: ...

    def new(self) -> str: ...

    def delete(self, turn_id: str) -> bool: ...

    def clear(self) -> int: ...


class PetInstallerService(Protocol):
    """RuntimeHost 使用的同步桌宠包安装边界"""

    def install(self, source: str) -> Path: ...


class DiagnosticServiceProtocol(Protocol):
    """RuntimeHost 使用的异步诊断边界"""

    async def run(self) -> Any: ...

    async def export(self, destination: str) -> None: ...


class TtsVoiceServiceProtocol(Protocol):
    """RuntimeHost 使用的中文声音目录与试听边界"""

    async def list_chinese_voices(self) -> tuple[Any, ...]: ...

    async def preview(self, voice_name: str) -> None: ...


class AsrPreparationService(Protocol):
    """RuntimeHost 使用的 ASR 模型准备边界"""

    def model_state(self) -> AsrModelState: ...

    async def download_model(self) -> None: ...

    async def preload(self) -> None: ...


class WakeWordRuntimeServiceProtocol(Protocol):
    """RuntimeHost 使用的中文唤醒服务边界"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def model_state(self) -> WakeModelState: ...

    @property
    def status(self) -> WakeRuntimeStatus: ...

    async def configure(self, config: WakeWordConfig) -> None: ...

    async def download_model(
        self,
        progress: Callable[[WakeDownloadProgress], None],
    ) -> None: ...

    def cancel_download(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """由同一个 Runtime 工作线程拥有的服务集合"""

    event_bus: EventBus
    coordinator: CoordinatorService
    activation: ActivationService
    capture: AsyncLifecycle
    worker: AsyncLifecycle | None = None
    wake_service: WakeWordRuntimeServiceProtocol | None = None
    closers: tuple[Callable[[], None], ...] = ()
    memory: MemoryDataService | None = None
    sessions: SessionDataService | None = None
    pet_installer: PetInstallerService | None = None
    diagnostics: DiagnosticServiceProtocol | None = None
    asr_preparer: AsrPreparationService | None = None
    tts_voice_service: TtsVoiceServiceProtocol | None = None
    llm_configured: bool = False


class RuntimeHost:
    """从调用线程向专属 asyncio 线程提交 Runtime 操作"""

    def __init__(
        self,
        services: RuntimeServices,
        *,
        lifecycle_timeout: float = 10.0,
    ) -> None:
        if lifecycle_timeout <= 0:
            raise RuntimeHostError("Runtime 生命周期超时必须大于零")
        self._services = services
        self._lifecycle_timeout = lifecycle_timeout
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._thread_id: int | None = None
        self._startup_error: BaseException | None = None
        self._started: list[AsyncLifecycle] = []
        self._closed = False
        self._shutdown_complete = False

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return (
            not self._closed
            and thread is not None
            and thread.is_alive()
            and self._ready.is_set()
            and self._startup_error is None
        )

    @property
    def llm_configured(self) -> bool:
        return self._services.llm_configured

    @property
    def thread_id(self) -> int | None:
        return self._thread_id

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeHostError("Runtime Host 已关闭")
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name="voicepet-runtime",
                daemon=False,
            )
            self._thread.start()
        if not self._ready.wait(self._lifecycle_timeout):
            with self._state_lock:
                loop = self._loop
                startup_task = self._startup_task
            if loop is not None and startup_task is not None:
                loop.call_soon_threadsafe(startup_task.cancel)
                self._ready.wait(self._lifecycle_timeout)
                self._thread.join(self._lifecycle_timeout)
            raise RuntimeHostError("Runtime 工作线程启动超时")
        if self._startup_error is not None:
            self._thread.join(self._lifecycle_timeout)
            raise RuntimeHostError("Runtime 服务启动失败") from self._startup_error

    def activate(self, source: str) -> Future[TurnId]:
        return self._submit(self._services.activation.activate(source))

    def speak_notice(self, text: str) -> Future[TurnId]:
        return self._submit(self._services.coordinator.speak_notice(text))

    def submit_text(self, text: str) -> Future[TurnId]:
        return self._submit(self._services.coordinator.submit_text(text))

    def set_speech_enabled(self, enabled: bool) -> Future[None]:
        return self._submit(
            self._services.coordinator.set_speech_enabled(enabled)
        )

    def list_tts_voices(self) -> Future[tuple[Any, ...]]:
        service = self._require_tts_voice_service()
        return self._submit(service.list_chinese_voices())

    def preview_tts_voice(self, voice_name: str) -> Future[None]:
        service = self._require_tts_voice_service()
        return self._submit(service.preview(voice_name))

    def asr_model_state(self) -> Future[AsrModelState]:
        preparer = self._require_asr_preparer()
        return self._submit(asyncio.to_thread(preparer.model_state))

    def download_asr_model(self) -> Future[None]:
        preparer = self._require_asr_preparer()
        return self._submit(preparer.download_model())

    def load_asr_model(self) -> Future[None]:
        preparer = self._require_asr_preparer()
        return self._submit(preparer.preload())

    def approve(self, mode: ConfirmationMode) -> Future[None]:
        return self._submit(self._services.coordinator.approve_pending(mode))

    def reject(self) -> Future[None]:
        return self._submit(self._services.coordinator.reject_pending())

    def list_memories(self) -> Future[tuple[Any, ...]]:
        memory = self._require_memory()
        return self._submit(asyncio.to_thread(memory.list_records))

    def delete_memory(self, memory_id: str) -> Future[bool]:
        memory = self._require_memory()
        return self._submit(asyncio.to_thread(memory.delete, memory_id))

    def confirm_memory(self, memory_id: str) -> Future[Any]:
        memory = self._require_memory()
        return self._submit(asyncio.to_thread(memory.confirm, memory_id))

    def resolve_memory_conflict(self, memory_id: str) -> Future[Any]:
        memory = self._require_memory()
        return self._submit(
            asyncio.to_thread(memory.resolve_conflict, memory_id)
        )

    def export_memories(self, destination: str) -> Future[int]:
        memory = self._require_memory()
        return self._submit(
            asyncio.to_thread(memory.export_json, destination)
        )

    def list_sessions(self) -> Future[tuple[Any, ...]]:
        sessions = self._require_sessions()
        return self._submit(asyncio.to_thread(sessions.list_records))

    def current_session_id(self) -> Future[str]:
        sessions = self._require_sessions()

        async def read_current() -> str:
            return sessions.current_session_id

        return self._submit(read_current())

    def list_session_turns(self, session_id: str) -> Future[tuple[Any, ...]]:
        sessions = self._require_sessions()
        return self._submit(
            asyncio.to_thread(sessions.list_turns, session_id)
        )

    def activate_session(self, session_id: str) -> Future[tuple[Any, ...]]:
        sessions = self._require_sessions()
        return self._submit(
            asyncio.to_thread(sessions.activate, session_id)
        )

    def new_session(self) -> Future[str]:
        sessions = self._require_sessions()
        return self._submit(asyncio.to_thread(sessions.new))

    def delete_session(self, turn_id: str) -> Future[bool]:
        sessions = self._require_sessions()
        return self._submit(asyncio.to_thread(sessions.delete, turn_id))

    def clear_sessions(self) -> Future[int]:
        sessions = self._require_sessions()
        return self._submit(asyncio.to_thread(sessions.clear))

    def install_pet(self, source: str) -> Future[Path]:
        installer = self._services.pet_installer
        if installer is None:
            raise RuntimeHostError("桌宠导入当前不可用")
        return self._submit(asyncio.to_thread(installer.install, source))

    def wake_model_state(self) -> Future[WakeModelState]:
        service = self._require_wake_service()

        async def read_state() -> WakeModelState:
            return service.model_state()

        return self._submit(read_state())

    def wake_runtime_status(self) -> Future[WakeRuntimeStatus]:
        service = self._require_wake_service()

        async def read_status() -> WakeRuntimeStatus:
            return service.status

        return self._submit(read_status())

    def configure_wake_word(self, config: WakeWordConfig) -> Future[None]:
        service = self._require_wake_service()
        return self._submit(service.configure(config))

    def download_wake_model(
        self,
        progress: Callable[[WakeDownloadProgress], None],
    ) -> Future[None]:
        service = self._require_wake_service()
        return self._submit(service.download_model(progress))

    def cancel_wake_model_download(self) -> None:
        service = self._require_wake_service()
        loop = self._loop
        if not self.is_running or loop is None:
            raise RuntimeHostError("Runtime 当前不可用")
        loop.call_soon_threadsafe(service.cancel_download)

    def run_diagnostics(self) -> Future[Any]:
        diagnostics = self._require_diagnostics()
        return self._submit(diagnostics.run())

    def export_diagnostics(self, destination: str) -> Future[None]:
        diagnostics = self._require_diagnostics()
        return self._submit(diagnostics.export(destination))

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is None or thread is None:
            self._close_sync_resources()
            return
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
        shutdown_error: BaseException | None = None
        try:
            future.result(timeout=self._lifecycle_timeout)
        except BaseException as error:  # noqa: BLE001 关闭仍需继续收敛工作线程
            shutdown_error = error
        loop.call_soon_threadsafe(loop.stop)
        thread.join(self._lifecycle_timeout)
        if thread.is_alive():
            raise RuntimeHostError("Runtime 工作线程关闭超时")
        if shutdown_error is not None:
            raise RuntimeHostError("Runtime 服务关闭失败") from shutdown_error

    def _submit(
        self,
        operation: Coroutine[Any, Any, ResultT],
    ) -> Future[ResultT]:
        loop = self._loop
        if not self.is_running or loop is None:
            operation.close()
            raise RuntimeHostError("Runtime 当前不可用")
        return asyncio.run_coroutine_threadsafe(operation, loop)

    def _require_memory(self) -> MemoryDataService:
        memory = self._services.memory
        if memory is None:
            raise RuntimeHostError("记忆数据管理当前不可用")
        return memory

    def _require_sessions(self) -> SessionDataService:
        sessions = self._services.sessions
        if sessions is None:
            raise RuntimeHostError("会话数据管理当前不可用")
        return sessions

    def _require_diagnostics(self) -> DiagnosticServiceProtocol:
        diagnostics = self._services.diagnostics
        if diagnostics is None:
            raise RuntimeHostError("诊断服务当前不可用")
        return diagnostics

    def _require_asr_preparer(self) -> AsrPreparationService:
        preparer = self._services.asr_preparer
        if preparer is None:
            raise RuntimeHostError("ASR 模型准备当前不可用")
        return preparer

    def _require_tts_voice_service(self) -> TtsVoiceServiceProtocol:
        service = self._services.tts_voice_service
        if service is None:
            raise RuntimeHostError("中文声音目录当前不可用")
        return service

    def _require_wake_service(self) -> WakeWordRuntimeServiceProtocol:
        service = self._services.wake_service
        if service is None:
            raise RuntimeHostError("中文语音唤醒当前不可用")
        return service

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._state_lock:
            self._loop = loop
            self._thread_id = threading.get_ident()
            self._startup_task = loop.create_task(self._startup())
        try:
            loop.run_until_complete(self._startup_task)
        except BaseException as error:  # noqa: BLE001 启动错误由调用线程稳定映射
            self._startup_error = error
            self._ready.set()
        else:
            self._ready.set()
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            with self._state_lock:
                self._loop = None
                self._startup_task = None
                self._thread_id = None

    async def _startup(self) -> None:
        try:
            for service in (
                self._services.capture,
                self._services.worker,
                self._services.wake_service,
            ):
                if service is None:
                    continue
                await service.start()
                self._started.append(service)
        except BaseException as startup_error:
            rollback_errors: list[BaseException] = []
            for service in reversed(self._started):
                await self._stop_service(service, rollback_errors)
            self._close_sync_resources(rollback_errors)
            self._shutdown_complete = True
            if rollback_errors:
                raise ExceptionGroup(
                    "Runtime 启动与回滚失败",
                    [startup_error, *rollback_errors],
                )
            raise

    async def _shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        errors: list[BaseException] = []
        wake = self._services.wake_service
        if wake is not None and wake in self._started:
            await self._stop_service(wake, errors)
        try:
            await self._services.coordinator.stop()
        except BaseException as error:  # noqa: BLE001 关闭必须继续释放其余资源
            errors.append(error)
        for service in (self._services.worker, self._services.capture):
            if service is not None and service in self._started:
                await self._stop_service(service, errors)
        self._close_sync_resources(errors)
        if errors:
            raise ExceptionGroup("Runtime 服务关闭失败", errors)

    async def _stop_service(
        self,
        service: AsyncLifecycle,
        errors: list[BaseException],
    ) -> None:
        try:
            await service.stop()
        except BaseException as error:  # noqa: BLE001 关闭必须继续释放其余资源
            errors.append(error)

    def _close_sync_resources(
        self,
        errors: list[BaseException] | None = None,
    ) -> None:
        if self._shutdown_complete and errors is None:
            return
        local_errors = errors if errors is not None else []
        for closer in self._services.closers:
            try:
                closer()
            except BaseException as error:  # noqa: BLE001 关闭必须继续释放其余资源
                local_errors.append(error)
        if errors is None:
            self._shutdown_complete = True
            if local_errors:
                raise RuntimeHostError("Runtime 同步资源关闭失败") from local_errors[0]
