import threading
import time

import pytest

import core
from core.asr import AsrModelState
from core.config import WakeWordConfig
from core.event_bus import EventBus
from core.events import TurnId
from core.runtime import RuntimeHost, RuntimeHostError, RuntimeServices
from core.wake import WakeRuntimeStatus
from core.wake_models import WakeDownloadProgress, WakeModelState


def test_runtime_host_types_are_publicly_exported():
    assert core.RuntimeHost is RuntimeHost
    assert core.RuntimeHostError is RuntimeHostError
    assert core.RuntimeServices is RuntimeServices


class FakeLifecycle:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    async def start(self):
        self.events.append((f"start:{self.name}", threading.get_ident()))

    async def stop(self):
        self.events.append((f"stop:{self.name}", threading.get_ident()))


class FailingLifecycle(FakeLifecycle):
    async def start(self):
        await super().start()
        raise RuntimeError("startup failed")


class BlockingLifecycle(FakeLifecycle):
    async def start(self):
        self.events.append((f"start:{self.name}", threading.get_ident()))
        await __import__("asyncio").Event().wait()


class FakeCoordinator:
    def __init__(self, events):
        self.events = events

    async def stop(self):
        self.events.append(("stop:coordinator", threading.get_ident()))

    async def speak_notice(self, text):
        self.events.append((f"notice:{text}", threading.get_ident()))
        return TurnId.new()

    async def set_speech_enabled(self, enabled):
        self.events.append((f"speech:{enabled}", threading.get_ident()))


class FakeActivation:
    def __init__(self, events):
        self.events = events

    async def activate(self, source):
        self.events.append((f"activate:{source}", threading.get_ident()))
        return TurnId.new()


class FakeMemory:
    def __init__(self, events):
        self.events = events

    def list_records(self):
        self.events.append(("memory:list", threading.get_ident()))
        return ("record",)

    def delete(self, memory_id):
        self.events.append((f"memory:delete:{memory_id}", threading.get_ident()))
        return True

    def confirm(self, memory_id):
        self.events.append((f"memory:confirm:{memory_id}", threading.get_ident()))
        return "confirmed"

    def resolve_conflict(self, memory_id):
        self.events.append((f"memory:resolve:{memory_id}", threading.get_ident()))
        return "resolved"

    def export_json(self, destination):
        self.events.append((f"memory:export:{destination}", threading.get_ident()))
        return 1


class FakePetInstaller:
    def __init__(self, events):
        self.events = events

    def install(self, source):
        self.events.append((f"pet:install:{source}", threading.get_ident()))
        return "pets/new-pet"


class FakeAsrPreparer:
    def __init__(self, events):
        self.events = events

    def model_state(self):
        self.events.append(("asr:state", threading.get_ident()))
        return AsrModelState.MISSING

    async def download_model(self):
        self.events.append(("asr:download", threading.get_ident()))

    async def preload(self):
        self.events.append(("asr:load", threading.get_ident()))


class FakeWakeService(FakeLifecycle):
    def __init__(self, events):
        super().__init__("wake", events)
        self.configurations = []
        self.cancelled = threading.Event()

    def model_state(self):
        self.events.append(("wake:state", threading.get_ident()))
        return WakeModelState.MISSING

    @property
    def status(self):
        self.events.append(("wake:status", threading.get_ident()))
        return WakeRuntimeStatus.MISSING

    async def configure(self, config):
        self.configurations.append(config)
        self.events.append(("wake:configure", threading.get_ident()))

    async def download_model(self, progress):
        progress(WakeDownloadProgress(5, 10))
        self.events.append(("wake:download", threading.get_ident()))

    def cancel_download(self):
        self.events.append(("wake:cancel", threading.get_ident()))
        self.cancelled.set()


def test_runtime_host_starts_services_and_runs_activation_off_ui_thread():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        FakeLifecycle("worker", events),
        FakeLifecycle("wake", events),
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    turn_id = host.activate("click").result(timeout=1)
    host.close()

    assert isinstance(turn_id, TurnId)
    assert [name for name, _ in events] == [
        "start:capture",
        "start:worker",
        "start:wake",
        "activate:click",
        "stop:wake",
        "stop:coordinator",
        "stop:worker",
        "stop:capture",
    ]
    assert all(thread_id != caller_thread for _, thread_id in events)


def test_runtime_host_dispatches_speech_toggle_off_ui_thread():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    try:
        host.set_speech_enabled(False).result(timeout=1)
    finally:
        host.close()

    speech_event = next(item for item in events if item[0].startswith("speech:"))
    assert speech_event[0] == "speech:False"
    assert speech_event[1] != caller_thread


def test_runtime_host_dispatches_spoken_notice_off_ui_thread():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    turn_id = host.speak_notice("请先配置 API Key").result(timeout=1)
    host.close()

    assert isinstance(turn_id, TurnId)
    notice = next(item for item in events if item[0].startswith("notice:"))
    assert notice[1] != caller_thread


def test_runtime_host_dispatches_asr_preparation_off_ui_thread():
    events = []
    preparer = FakeAsrPreparer(events)
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        asr_preparer=preparer,
        llm_configured=False,
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    assert host.llm_configured is False
    assert host.asr_model_state().result(timeout=1) is AsrModelState.MISSING
    host.download_asr_model().result(timeout=1)
    host.load_asr_model().result(timeout=1)
    host.close()

    asr_events = [item for item in events if item[0].startswith("asr:")]
    assert [name for name, _ in asr_events] == [
        "asr:state",
        "asr:download",
        "asr:load",
    ]
    assert all(thread_id != caller_thread for _, thread_id in asr_events)


def test_runtime_host_rolls_back_started_services_on_startup_failure():
    events = []
    caller_thread = threading.get_ident()
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        FailingLifecycle("worker", events),
        FakeLifecycle("wake", events),
        (lambda: events.append(("close:resources", threading.get_ident())),),
    )
    host = RuntimeHost(services)

    with pytest.raises(RuntimeHostError, match="启动失败"):
        host.start()

    assert [name for name, _ in events] == [
        "start:capture",
        "start:worker",
        "stop:capture",
        "close:resources",
    ]
    assert all(thread_id != caller_thread for _, thread_id in events)
    assert host.is_running is False


def test_runtime_host_start_close_are_idempotent_and_reject_after_close():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
    )
    host = RuntimeHost(services)

    host.start()
    host.start()
    host.close()
    host.close()

    assert [name for name, _ in events] == [
        "start:capture",
        "stop:coordinator",
        "stop:capture",
    ]
    with pytest.raises(RuntimeHostError, match="不可用"):
        host.activate("click")


def test_runtime_host_cancels_startup_task_after_timeout():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        BlockingLifecycle("capture", events),
        closers=(
            lambda: events.append(("close:resources", threading.get_ident())),
        ),
    )
    host = RuntimeHost(services, lifecycle_timeout=0.05)

    with pytest.raises(RuntimeHostError, match="启动超时"):
        host.start()

    deadline = time.monotonic() + 1
    while host.thread_id is not None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert host.thread_id is None
    assert [name for name, _ in events] == [
        "start:capture",
        "close:resources",
    ]


def test_runtime_host_dispatches_memory_data_operations_off_ui_thread():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        memory=FakeMemory(events),
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    assert host.list_memories().result(timeout=1) == ("record",)
    assert host.delete_memory("memory-1").result(timeout=1) is True
    assert host.confirm_memory("memory-2").result(timeout=1) == "confirmed"
    assert host.resolve_memory_conflict("memory-3").result(timeout=1) == "resolved"
    assert host.export_memories("memory.json").result(timeout=1) == 1
    host.close()

    memory_events = [item for item in events if item[0].startswith("memory:")]
    assert len(memory_events) == 5
    assert all(thread_id != caller_thread for _, thread_id in memory_events)


def test_runtime_host_dispatches_pet_install_off_ui_thread():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        pet_installer=FakePetInstaller(events),
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    result = host.install_pet("source.codex-pet").result(timeout=1)
    host.close()

    assert result == "pets/new-pet"
    pet_event = next(item for item in events if item[0].startswith("pet:"))
    assert pet_event[1] != caller_thread


def test_runtime_host_dispatches_managed_wake_operations_off_ui_thread():
    events = []
    wake = FakeWakeService(events)
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        wake_service=wake,
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()
    progress = []
    config = WakeWordConfig(keyword="你好海蓝")

    host.start()
    assert host.wake_model_state().result(timeout=1) is WakeModelState.MISSING
    assert host.wake_runtime_status().result(timeout=1) is WakeRuntimeStatus.MISSING
    host.configure_wake_word(config).result(timeout=1)
    host.download_wake_model(progress.append).result(timeout=1)
    host.cancel_wake_model_download()
    assert wake.cancelled.wait(1)
    host.close()

    wake_events = [item for item in events if item[0].startswith("wake:")]
    assert wake.configurations == [config]
    assert progress == [WakeDownloadProgress(5, 10)]
    assert all(thread_id != caller_thread for _, thread_id in wake_events)
