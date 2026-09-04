import asyncio
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.audio_types import (
    AudioDeviceError,
    AudioFormat,
    AudioFrameError,
    CapturedFrame,
)
from core.config import WakeWordConfig
from core.event_bus import EventBus
from core.events import (
    ConversationPhase,
    CorrelationId,
    StateChanged,
    TurnId,
)
from core.wake import (
    ActivationController,
    SherpaOnnxKeywordDetector,
    WakeConfigurationError,
    WakeDetectionError,
    WakeRuntimeStatus,
    WakeWordMonitor,
    WakeWordRuntimeService,
    prepare_transcript_for_source,
)
from core.wake_models import (
    WakeDownloadProgress,
    WakeModelFiles,
    WakeModelState,
)


async def wait_until(predicate, attempts: int = 100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class FakeActivationTarget:
    def __init__(self) -> None:
        self.phase = ConversationPhase.IDLE
        self.calls = []

    async def start_listening(self, source="click"):
        self.calls.append(("start", source))
        self.phase = ConversationPhase.LISTENING
        await asyncio.sleep(0)
        return TurnId.new()

    async def interrupt(self, source="click"):
        self.calls.append(("interrupt", source))
        self.phase = ConversationPhase.LISTENING
        await asyncio.sleep(0)
        return TurnId.new()


class ControlledSubscription:
    def __init__(self) -> None:
        self.queue = asyncio.Queue()
        self.close_calls = 0

    async def read(self, token):
        read_task = asyncio.create_task(self.queue.get())
        cancel_task = asyncio.create_task(token.wait())
        done, _ = await asyncio.wait(
            {read_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
            token.throw_if_cancelled()
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        return read_task.result()

    def feed(self, audio_frame):
        self.queue.put_nowait(audio_frame)

    def close(self):
        self.close_calls += 1


class FailedSubscription:
    def __init__(self, error) -> None:
        self.error = error
        self.close_calls = 0

    async def read(self, token):
        del token
        raise self.error

    def close(self):
        self.close_calls += 1


class FakeCaptureService:
    def __init__(self, subscription) -> None:
        self.subscription = subscription
        self.calls = []

    def subscribe(self, **kwargs):
        self.calls.append(kwargs)
        return self.subscription


class SequencedCaptureService:
    def __init__(self, subscriptions) -> None:
        self.subscriptions = deque(subscriptions)
        self.calls = []

    def subscribe(self, **kwargs):
        self.calls.append(kwargs)
        return self.subscriptions.popleft()


class ResultDetector:
    def __init__(self, results) -> None:
        self.results = deque(results)
        self.frames = []
        self.reset_calls = 0
        self.speaking = []
        self.close_calls = 0

    def accept(self, pcm, audio_format):
        self.frames.append(pcm)
        return self.results.popleft() if self.results else None

    def reset(self):
        self.reset_calls += 1

    def set_speaking(self, speaking):
        self.speaking.append(speaking)

    def close(self):
        self.close_calls += 1


def frame(sequence: int, captured_at: float | None = None) -> CapturedFrame:
    audio_format = AudioFormat()
    return CapturedFrame(
        sequence,
        captured_at if captured_at is not None else sequence * 0.02,
        bytes([sequence % 256]) * audio_format.frame_bytes,
    )


def make_model_files(tmp_path):
    paths = {}
    for name in ("tokens", "encoder", "decoder", "joiner"):
        path = tmp_path / f"{name}.onnx"
        path.write_bytes(name.encode())
        paths[name] = path
    return WakeModelFiles(**paths)


def install_fake_sherpa(monkeypatch, *, result="你好，小蓝"):
    created = []
    token_calls = []

    class Stream:
        def __init__(self):
            self.waveforms = []
            self.ready = False
            self.result = result

        def accept_waveform(self, sample_rate, samples):
            self.waveforms.append((sample_rate, samples))
            self.ready = True

    class Spotter:
        def __init__(self, **settings):
            self.settings = settings
            keywords_file = Path(settings.get("keywords_file", ""))
            self.keywords_file_existed = keywords_file.is_file()
            self.keywords_file_content = (
                keywords_file.read_text(encoding="utf-8")
                if self.keywords_file_existed
                else None
            )
            self.stream_specs = []
            self.streams = []
            self.reset_calls = 0
            created.append(self)

        def create_stream(self, specification):
            self.stream_specs.append(specification)
            stream = Stream()
            self.streams.append(stream)
            return stream

        def is_ready(self, stream):
            return stream.ready

        def decode_stream(self, stream):
            stream.ready = False

        def get_result(self, stream):
            value, stream.result = stream.result, ""
            return value

        def reset_stream(self, stream):
            self.reset_calls += 1
            stream.result = ""

    def text2token(texts, *, tokens, tokens_type):
        token_calls.append((texts, tokens, tokens_type))
        return [["n", "ǐ", "h", "ǎo", "x", "iǎo", "l", "án"]]

    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(KeywordSpotter=Spotter, text2token=text2token),
    )
    return created, token_calls


def test_activation_forwards_source_when_starting_and_interrupting():
    async def scenario():
        target = FakeActivationTarget()
        controller = ActivationController(target)

        first = await controller.activate("click")
        second = await controller.activate("wake_word")

        assert first != second
        assert target.calls == [
            ("start", "click"),
            ("interrupt", "wake_word"),
        ]

    asyncio.run(scenario())


def test_concurrent_activation_is_serialized():
    async def scenario():
        target = FakeActivationTarget()
        controller = ActivationController(target)

        await asyncio.gather(
            controller.activate("wake_word"),
            controller.activate("shortcut"),
        )

        assert [call[0] for call in target.calls] == ["start", "interrupt"]

    asyncio.run(scenario())


def test_monitor_feeds_frames_and_applies_debounce():
    async def scenario():
        subscription = ControlledSubscription()
        capture = FakeCaptureService(subscription)
        detector = ResultDetector(["你好，小蓝", "你好，小蓝", "你好，小蓝"])
        activations = []

        async def on_wake(source):
            activations.append(source)

        monitor = WakeWordMonitor(
            capture,
            detector,
            on_wake,
            debounce_sec=1.5,
        )
        await monitor.start()
        for sequence, captured_at in enumerate((0.1, 0.2, 1.7), start=1):
            subscription.feed(frame(sequence, captured_at))
        await wait_until(lambda: len(detector.frames) == 3)
        await monitor.stop()
        return capture, detector, activations, subscription

    capture, detector, activations, subscription = asyncio.run(scenario())

    assert capture.calls == [
        {
            "include_preroll": False,
            "queue_capacity": 100,
            "drop_oldest_on_overflow": True,
        }
    ]
    assert activations == ["wake_word", "wake_word"]
    assert detector.reset_calls == 2
    assert subscription.close_calls == 1


def test_monitor_lifecycle_is_idempotent_and_stop_cancels_reader():
    async def scenario():
        subscription = ControlledSubscription()
        monitor = WakeWordMonitor(
            FakeCaptureService(subscription),
            ResultDetector([]),
            lambda source: None,
        )

        await monitor.start()
        await monitor.start()
        assert monitor.running
        await monitor.stop()
        await monitor.stop()

        assert not monitor.running
        assert subscription.close_calls == 1

    asyncio.run(scenario())


def test_monitor_resubscribes_after_transient_audio_interruption():
    async def scenario():
        first_subscription = FailedSubscription(
            AudioDeviceError("temporary input overflow")
        )
        second_subscription = ControlledSubscription()
        capture = SequencedCaptureService(
            [first_subscription, second_subscription]
        )
        errors = []
        activations = []

        async def on_error(error):
            errors.append(error)

        monitor = WakeWordMonitor(
            capture,
            ResultDetector(["你好，小蓝"]),
            activations.append,
            on_error=on_error,
            retry_delay_sec=0,
        )
        await monitor.start()
        await wait_until(lambda: len(errors) == 1)
        await wait_until(lambda: len(capture.calls) == 2)
        second_subscription.feed(frame(2, 2.0))
        await wait_until(lambda: len(activations) == 1)

        assert isinstance(errors[0], AudioDeviceError)
        assert monitor.running
        await monitor.stop()
        assert first_subscription.close_calls == 1
        assert second_subscription.close_calls == 1
        assert activations == ["wake_word"]

    asyncio.run(scenario())


def test_monitor_rejects_invalid_configuration():
    capture = FakeCaptureService(ControlledSubscription())

    with pytest.raises(WakeConfigurationError):
        WakeWordMonitor(capture, ResultDetector([]), lambda source: None, debounce_sec=-1)
    with pytest.raises(WakeConfigurationError):
        WakeWordMonitor(
            capture,
            ResultDetector([]),
            lambda source: None,
            retry_delay_sec=-1,
        )


def test_sherpa_detector_encodes_keyword_and_streams_audio(monkeypatch, tmp_path):
    created, token_calls = install_fake_sherpa(monkeypatch)
    files = make_model_files(tmp_path)
    detector = SherpaOnnxKeywordDetector(
        files,
        keyword="你好，小蓝",
        sensitivity=0.5,
    )
    audio_format = AudioFormat()

    detected = detector.accept(bytes(audio_format.frame_bytes), audio_format)

    assert detected == "你好，小蓝"
    assert token_calls == [(["你好小蓝"], str(files.tokens), "ppinyin")]
    assert "#0.250" in created[0].stream_specs[0]
    assert "@你好，小蓝" in created[0].stream_specs[0]
    assert created[0].settings["num_threads"] == 1
    assert created[0].keywords_file_existed is True
    assert created[0].keywords_file_content == ""
    assert not Path(created[0].settings["keywords_file"]).exists()
    sample_rate, samples = created[0].streams[0].waveforms[0]
    assert sample_rate == 16_000
    assert samples.dtype.name == "float32"
    assert len(samples) == 320


def test_sherpa_detector_uses_stricter_threshold_while_speaking(
    monkeypatch,
    tmp_path,
):
    created, _ = install_fake_sherpa(monkeypatch)
    detector = SherpaOnnxKeywordDetector(
        make_model_files(tmp_path),
        keyword="你好，小蓝",
        sensitivity=0.5,
    )

    detector.set_speaking(True)

    assert "#0.400" in created[0].stream_specs[-1]


def test_sherpa_detector_rejects_bad_audio_and_native_errors(
    monkeypatch,
    tmp_path,
):
    created, _ = install_fake_sherpa(monkeypatch)
    detector = SherpaOnnxKeywordDetector(
        make_model_files(tmp_path),
        keyword="你好，小蓝",
        sensitivity=0.5,
    )

    with pytest.raises(AudioFrameError):
        detector.accept(b"short", AudioFormat())

    created[0].is_ready = lambda stream: (_ for _ in ()).throw(OSError("native"))
    with pytest.raises(WakeDetectionError, match="native"):
        detector.accept(bytes(AudioFormat().frame_bytes), AudioFormat())


def test_sherpa_detector_rejects_empty_tokenization(monkeypatch, tmp_path):
    module = SimpleNamespace(
        KeywordSpotter=lambda **settings: None,
        text2token=lambda *args, **kwargs: [[]],
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)

    with pytest.raises(WakeConfigurationError, match="转换"):
        SherpaOnnxKeywordDetector(
            make_model_files(tmp_path),
            keyword="你好，小蓝",
        )


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("你好小蓝，打开设置", "打开设置"),
        ("你好，小蓝 打开设置", "打开设置"),
        ("打开你好小蓝的设置", "打开你好小蓝的设置"),
    ],
)
def test_wake_source_removes_only_leading_keyword(transcript, expected):
    assert (
        prepare_transcript_for_source(
            transcript,
            source="wake_word",
            keyword="你好，小蓝",
        )
        == expected
    )


def test_click_source_never_removes_wake_keyword():
    transcript = "你好小蓝，打开设置"

    assert (
        prepare_transcript_for_source(
            transcript,
            source="click",
            keyword="你好，小蓝",
        )
        == transcript
    )


class FakeWakeModelStore:
    def __init__(self, state=WakeModelState.READY):
        self.current_state = state
        self.install_calls = []
        self.downloads = 0

    def state(self):
        return self.current_state

    def files(self):
        return SimpleNamespace(
            tokens="tokens",
            encoder="encoder",
            decoder="decoder",
            joiner="joiner",
        )

    async def download(self, progress, token):
        self.downloads += 1
        token.throw_if_cancelled()
        progress(WakeDownloadProgress(5, 10))
        return "package"

    def install_package(self, package):
        self.install_calls.append(package)
        self.current_state = WakeModelState.READY
        return self.files()


class FakeManagedMonitor:
    def __init__(self, detector):
        self.detector = detector
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


def build_wake_service(*, state=WakeModelState.READY, enabled=True):
    store = FakeWakeModelStore(state)
    detectors = []
    monitors = []

    def detector_factory(files, config):
        detector = ResultDetector([])
        detector.keyword = config.keyword
        detectors.append(detector)
        return detector

    def monitor_factory(detector, config):
        monitor = FakeManagedMonitor(detector)
        monitors.append(monitor)
        return monitor

    service = WakeWordRuntimeService(
        store,
        EventBus(),
        WakeWordConfig(enabled=enabled),
        detector_factory=detector_factory,
        monitor_factory=monitor_factory,
    )
    return service, store, detectors, monitors


def test_wake_service_starts_ready_enabled_model():
    async def scenario():
        service, _, detectors, monitors = build_wake_service()

        await service.start()

        assert service.status is WakeRuntimeStatus.LISTENING
        assert detectors[-1].keyword == "你好，小蓝"
        assert monitors[-1].started == 1
        await service.stop()
        assert detectors[-1].close_calls == 1

    asyncio.run(scenario())


def test_wake_service_reports_missing_and_disabled_without_detector():
    async def scenario():
        missing, _, missing_detectors, _ = build_wake_service(
            state=WakeModelState.MISSING
        )
        disabled, _, disabled_detectors, _ = build_wake_service(enabled=False)

        await missing.start()
        await disabled.start()

        assert missing.status is WakeRuntimeStatus.MISSING
        assert disabled.status is WakeRuntimeStatus.DISABLED
        assert missing_detectors == []
        assert disabled_detectors == []
        await missing.stop()
        await disabled.stop()

    asyncio.run(scenario())


def test_wake_service_start_failure_degrades_and_allows_retry():
    async def scenario():
        service, _, detectors, _ = build_wake_service()
        working_factory = service._detector_factory
        service._detector_factory = lambda files, config: (_ for _ in ()).throw(
            WakeConfigurationError("bad keyword")
        )

        await service.start()

        assert service.status is WakeRuntimeStatus.FAILED
        service._detector_factory = working_factory
        await service.configure(WakeWordConfig(keyword="你好海蓝"))
        assert service.status is WakeRuntimeStatus.LISTENING
        assert detectors[-1].keyword == "你好海蓝"
        await service.stop()

    asyncio.run(scenario())


def test_wake_service_reconfigures_and_preserves_old_on_factory_failure():
    async def scenario():
        service, _, detectors, monitors = build_wake_service()
        await service.start()
        old_detector = detectors[-1]
        old_monitor = monitors[-1]

        await service.configure(WakeWordConfig(keyword="你好海蓝"))

        assert old_monitor.stopped == 1
        assert old_detector.close_calls == 1
        assert detectors[-1].keyword == "你好海蓝"
        active_detector = detectors[-1]
        active_monitor = monitors[-1]

        working_factory = service._detector_factory
        service._detector_factory = lambda files, config: (_ for _ in ()).throw(
            WakeConfigurationError("bad keyword")
        )
        with pytest.raises(WakeConfigurationError):
            await service.configure(WakeWordConfig(keyword="你好深蓝"))

        assert active_monitor.stopped == 0
        assert active_detector.close_calls == 0
        assert service.status is WakeRuntimeStatus.LISTENING
        await service.stop()
        service._detector_factory = working_factory
        await service.start()
        assert detectors[-1].keyword == "你好海蓝"
        await service.stop()

    asyncio.run(scenario())


def test_wake_service_updates_speaking_threshold_from_state_events():
    async def scenario():
        bus = EventBus()
        store = FakeWakeModelStore()
        detector = ResultDetector([])
        service = WakeWordRuntimeService(
            store,
            bus,
            WakeWordConfig(),
            detector_factory=lambda files, config: detector,
            monitor_factory=lambda current, config: FakeManagedMonitor(current),
        )
        await service.start()
        turn_id = TurnId.new()
        correlation_id = CorrelationId.new()

        await bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.THINKING,
                ConversationPhase.SPEAKING,
            )
        )
        await bus.publish(
            StateChanged(
                turn_id,
                correlation_id,
                ConversationPhase.SPEAKING,
                ConversationPhase.IDLE,
            )
        )

        assert detector.speaking == [True, False]
        assert detector.reset_calls == 1
        await service.stop()

    asyncio.run(scenario())


def test_wake_service_downloads_installs_and_starts_model():
    async def scenario():
        service, store, _, monitors = build_wake_service(
            state=WakeModelState.MISSING
        )
        progress = []
        await service.start()

        await service.download_model(progress.append)

        assert store.downloads == 1
        assert store.install_calls == ["package"]
        assert progress == [WakeDownloadProgress(5, 10)]
        assert monitors[-1].started == 1
        assert service.status is WakeRuntimeStatus.LISTENING
        await service.stop()

    asyncio.run(scenario())
