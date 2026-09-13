import threading
import time

import pytest

import core
from core.asr import AsrModelState
from core.audio_types import AudioDeviceError
from core.config import WakeWordConfig
from core.event_bus import EventBus
from core.events import RuntimeErrorEvent, TurnId
from core.runtime import RuntimeHost, RuntimeHostError, RuntimeServices
from core.tts import TtsVoiceOption
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


class DegradedAudioLifecycle(FakeLifecycle):
    async def start(self):
        await super().start()
        raise AudioDeviceError("麦克风启动失败: Error querying device -1")


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

    async def submit_text(self, text):
        self.events.append((f"text:{text}", threading.get_ident()))
        return TurnId.new()

    async def cancel_active_turn(self):
        self.events.append(("cancel:turn", threading.get_ident()))

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

    def edit(self, memory_id, content, expected_version):
        self.events.append((f"memory:edit:{memory_id}", threading.get_ident()))
        return "edited"

    def undo(self, change_id):
        self.events.append((f"memory:undo:{change_id}", threading.get_ident()))
        return True

    def list_changes(self, session_id=None):
        return ()

    def export_json(self, destination):
        self.events.append((f"memory:export:{destination}", threading.get_ident()))
        return 1


class FakeSessions:
    def __init__(self, events):
        self.events = events
        self.current_session_id = "session-current"

    def list_records(self):
        self.events.append(("session:list", threading.get_ident()))
        return ("session",)

    def delete(self, turn_id):
        self.events.append((f"session:delete:{turn_id}", threading.get_ident()))
        return True

    def list_turns(self, session_id):
        self.events.append((f"session:turns:{session_id}", threading.get_ident()))
        return ("turn",)

    def activate(self, session_id):
        self.current_session_id = session_id
        self.events.append((f"session:activate:{session_id}", threading.get_ident()))
        return ("turn",)

    def new(self):
        self.current_session_id = "session-new"
        self.events.append(("session:new", threading.get_ident()))
        return self.current_session_id

    def clear(self):
        self.events.append(("session:clear", threading.get_ident()))
        return 3


class FakePetInstaller:
    def __init__(self, events):
        self.events = events

    def install(self, source):
        self.events.append((f"pet:install:{source}", threading.get_ident()))
        return "pets/new-pet"

    def remove(self, pet_id):
        self.events.append((f"pet:remove:{pet_id}", threading.get_ident()))
        return True


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


class FakeTtsVoiceService:
    def __init__(self, events):
        self.events = events

    async def list_chinese_voices(self):
        self.events.append(("tts:voices", threading.get_ident()))
        return (TtsVoiceOption("zh-CN-XiaoxiaoNeural", "zh-CN", "Female"),)

    async def preview(self, voice_name):
        self.events.append((f"tts:preview:{voice_name}", threading.get_ident()))


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
        wake_service=FakeLifecycle("wake", events),
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    turn_id = host.activate("click").result(timeout=1)
    host.close()

    assert isinstance(turn_id, TurnId)
    assert [name for name, _ in events] == [
        "start:capture",
        "start:wake",
        "activate:click",
        "stop:wake",
        "stop:coordinator",
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


@pytest.mark.parametrize("fails", [False, True])
def test_runtime_host_audio_mute_is_independent_and_reports_completion(fails):
    events = []
    error = RuntimeError("audio output failed")

    class AudioOutput:
        async def set_muted(self, muted):
            events.append((f"muted:{muted}", threading.get_ident()))
            if fails:
                raise error

    services = RuntimeServices(
        EventBus(), FakeCoordinator(events), FakeActivation(events),
        FakeLifecycle("capture", events), audio_output=AudioOutput(),
    )
    host = RuntimeHost(services)
    host.start()
    try:
        future = host.set_audio_muted(True)
        if fails:
            with pytest.raises(RuntimeError) as captured:
                future.result(timeout=1)
            assert captured.value is error
        else:
            assert future.result(timeout=1) is None
        assert ("muted:True", host.thread_id) in events
        assert not any(name.startswith("speech:") for name, _ in events)
    finally:
        host.close()


def test_runtime_host_audio_mute_requires_audio_output():
    events = []
    host = RuntimeHost(RuntimeServices(
        EventBus(), FakeCoordinator(events), FakeActivation(events),
        FakeLifecycle("capture", events),
    ))
    try:
        with pytest.raises(RuntimeHostError, match="音频"):
            host.set_audio_muted(True)
    finally:
        host.close()


def test_runtime_host_dispatches_active_turn_cancellation_off_ui_thread():
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
        host.cancel_active_turn().result(timeout=1)
    finally:
        host.close()

    cancellation = next(item for item in events if item[0] == "cancel:turn")
    assert cancellation[1] != caller_thread


def test_runtime_host_dispatches_voice_catalog_and_preview_off_ui_thread():
    events = []
    voice_service = FakeTtsVoiceService(events)
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        tts_voice_service=voice_service,
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    try:
        voices = host.list_tts_voices().result(timeout=1)
        host.preview_tts_voice(voices[0].short_name).result(timeout=1)
    finally:
        host.close()

    voice_events = [item for item in events if item[0].startswith("tts:")]
    assert [item[0] for item in voice_events] == [
        "tts:voices",
        "tts:preview:zh-CN-XiaoxiaoNeural",
    ]
    assert all(thread_id != caller_thread for _, thread_id in voice_events)


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
        wake_service=FailingLifecycle("wake", events),
        closers=(
            lambda: events.append(("close:resources", threading.get_ident())),
        ),
    )
    host = RuntimeHost(services)

    with pytest.raises(RuntimeHostError, match="启动失败"):
        host.start()

    assert [name for name, _ in events] == [
        "start:capture",
        "start:wake",
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
    assert len(memory_events) == 6
    assert all(thread_id != caller_thread for _, thread_id in memory_events)


def test_runtime_host_dispatches_text_and_session_operations_off_ui_thread():
    events = []
    services = RuntimeServices(
        EventBus(),
        FakeCoordinator(events),
        FakeActivation(events),
        FakeLifecycle("capture", events),
        sessions=FakeSessions(events),
    )
    host = RuntimeHost(services)
    caller_thread = threading.get_ident()

    host.start()
    assert isinstance(host.submit_text("你好").result(timeout=1), TurnId)
    assert host.list_sessions().result(timeout=1) == ("session",)
    assert host.current_session_id().result(timeout=1) == "session-current"
    assert host.list_session_turns("session-1").result(timeout=1) == ("turn",)
    assert host.activate_session("session-1").result(timeout=1) == ("turn",)
    assert host.new_session().result(timeout=1) == "session-new"
    assert host.delete_session("turn-1").result(timeout=1) is True
    assert host.clear_sessions().result(timeout=1) == 3
    host.close()

    operation_events = [
        item
        for item in events
        if item[0].startswith(("text:", "session:"))
    ]
    assert len(operation_events) == 7
    assert all(thread_id != caller_thread for _, thread_id in operation_events)


def test_runtime_host_dispatches_pet_install_and_remove_off_ui_thread():
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
    removed = host.remove_pet("new-pet").result(timeout=1)
    host.close()

    assert result == "pets/new-pet"
    assert removed is True
    pet_events = [item for item in events if item[0].startswith("pet:")]
    assert len(pet_events) == 2
    assert all(item[1] != caller_thread for item in pet_events)


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


def test_runtime_host_degrades_missing_microphone_and_keeps_running():
    events = []
    bus = EventBus()
    published = []
    bus.subscribe(RuntimeErrorEvent, published.append)
    services = RuntimeServices(
        bus,
        FakeCoordinator(events),
        FakeActivation(events),
        DegradedAudioLifecycle("capture", events),
        wake_service=FakeLifecycle("wake", events),
        scheduler=FakeLifecycle("scheduler", events),
    )
    host = RuntimeHost(services)

    host.start()
    try:
        assert host.is_running is True
        assert host.audio_available is False
        # 采集失败后跳过依赖麦克风的唤醒监听，其余服务照常启动
        assert [name for name, _ in events] == ["start:capture", "start:scheduler"]
        assert [event.error_code for event in published] == ["audio.device"]
        with pytest.raises(RuntimeHostError, match="麦克风不可用") as failure:
            host.activate("click")
        assert "麦克风不可用" in failure.value.safe_message
    finally:
        host.close()

    assert [name for name, _ in events] == [
        "start:capture",
        "start:scheduler",
        "stop:coordinator",
        "stop:scheduler",
    ]
