import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from core.audio_input import AudioCaptureService, SoundDeviceInputBackend
from core.audio_types import (
    AudioConfigurationError,
    AudioDeviceError,
    AudioFormat,
    AudioFrameError,
    AudioOverflowError,
)
from core.cancellation import CancellationSource


class FakeAudioInputBackend:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.on_frame = None
        self.on_error = None

    def start(self, on_frame, on_error) -> None:
        self.start_calls += 1
        self.on_frame = on_frame
        self.on_error = on_error

    def stop(self) -> None:
        self.stop_calls += 1

    def emit(self, pcm: bytes) -> None:
        assert self.on_frame is not None
        self.on_frame(pcm)

    def fail(self, error: Exception) -> None:
        assert self.on_error is not None
        self.on_error(error)


def pcm_frame(value: int, audio_format: AudioFormat | None = None) -> bytes:
    audio_format = audio_format or AudioFormat()
    return bytes([value % 256]) * audio_format.frame_bytes


async def flush_callbacks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_capture_start_and_stop_are_idempotent():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)

        await service.start()
        await service.start()
        await service.stop()
        await service.stop()

        assert backend.start_calls == 1
        assert backend.stop_calls == 1

    asyncio.run(scenario())


def test_subscription_receives_preroll_before_live_frames():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)
        token = CancellationSource().token
        await service.start()

        for value in range(1, 26):
            backend.emit(pcm_frame(value))
        await flush_callbacks()

        subscription = service.subscribe()
        preroll = [await subscription.read(token) for _ in range(20)]
        backend.emit(pcm_frame(26))
        await flush_callbacks()
        live = await subscription.read(token)
        await service.stop()
        return preroll, live

    preroll, live = asyncio.run(scenario())

    assert [frame.sequence for frame in preroll] == list(range(6, 26))
    assert live.sequence == 26
    assert live.pcm == pcm_frame(26)


def test_callback_from_helper_thread_is_delivered_to_subscription():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)
        token = CancellationSource().token
        await service.start()
        subscription = service.subscribe(include_preroll=False)

        thread = threading.Thread(target=backend.emit, args=(pcm_frame(7),))
        thread.start()
        thread.join()
        frame = await asyncio.wait_for(subscription.read(token), timeout=1)
        await service.stop()
        return frame

    assert asyncio.run(scenario()).pcm == pcm_frame(7)


def test_invalid_frame_fails_recording_subscription():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)
        token = CancellationSource().token
        await service.start()
        subscription = service.subscribe(include_preroll=False)

        backend.emit(b"short")
        await flush_callbacks()
        with pytest.raises(AudioFrameError):
            await subscription.read(token)
        await service.stop()

    asyncio.run(scenario())


def test_backend_error_wakes_pending_reader():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)
        token = CancellationSource().token
        await service.start()
        subscription = service.subscribe(include_preroll=False)
        reader = asyncio.create_task(subscription.read(token))
        await asyncio.sleep(0)

        backend.fail(RuntimeError("device lost"))
        with pytest.raises(AudioDeviceError, match="device lost"):
            await reader
        await service.stop()

    asyncio.run(scenario())


def test_subscription_overflow_is_reported_without_silent_drop():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)
        token = CancellationSource().token
        await service.start()
        subscription = service.subscribe(
            include_preroll=False,
            queue_capacity=1,
        )

        backend.emit(pcm_frame(1))
        backend.emit(pcm_frame(2))
        await flush_callbacks()
        with pytest.raises(AudioOverflowError):
            await subscription.read(token)
        await service.stop()

    asyncio.run(scenario())


def test_realtime_subscription_drops_oldest_frame_without_closing():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)
        token = CancellationSource().token
        await service.start()
        subscription = service.subscribe(
            include_preroll=False,
            queue_capacity=1,
            drop_oldest_on_overflow=True,
        )

        backend.emit(pcm_frame(1))
        backend.emit(pcm_frame(2))
        await flush_callbacks()
        frame = await subscription.read(token)
        subscription.close()
        await service.stop()
        return frame

    assert asyncio.run(scenario()).pcm == pcm_frame(2)


def test_closed_subscription_and_stopped_service_wake_readers():
    async def scenario():
        backend = FakeAudioInputBackend()
        service = AudioCaptureService(backend)
        token = CancellationSource().token
        await service.start()

        closed = service.subscribe(include_preroll=False)
        closed.close()
        closed.close()
        with pytest.raises(AudioDeviceError, match="订阅已关闭"):
            await closed.read(token)

        pending = service.subscribe(include_preroll=False)
        reader = asyncio.create_task(pending.read(token))
        await asyncio.sleep(0)
        await service.stop()
        with pytest.raises(AudioDeviceError, match="采集服务已停止"):
            await reader

    asyncio.run(scenario())


def test_sounddevice_backend_reports_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    with pytest.raises(AudioConfigurationError, match="audio"):
        SoundDeviceInputBackend()


def test_sounddevice_backend_configures_and_closes_raw_stream(monkeypatch):
    streams = []

    class FakeRawInputStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.start_calls = 0
            self.stop_calls = 0
            self.close_calls = 0
            streams.append(self)

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

        def close(self):
            self.close_calls += 1

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawInputStream=FakeRawInputStream),
    )
    backend = SoundDeviceInputBackend(device=3)
    frames = []
    errors = []

    backend.start(frames.append, errors.append)
    backend.start(frames.append, errors.append)
    stream = streams[0]
    stream.kwargs["callback"](
        memoryview(pcm_frame(4)),
        320,
        None,
        None,
    )
    backend.stop()
    backend.stop()

    assert stream.kwargs["samplerate"] == 16_000
    assert stream.kwargs["blocksize"] == 320
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "int16"
    assert stream.kwargs["device"] == 3
    assert frames == [pcm_frame(4)]
    assert errors == []
    assert stream.start_calls == 1
    assert stream.stop_calls == 1
    assert stream.close_calls == 1


def test_sounddevice_backend_maps_native_start_failure(monkeypatch):
    class FailingRawInputStream:
        def __init__(self, **kwargs):
            raise OSError("microphone unavailable")

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawInputStream=FailingRawInputStream),
    )
    backend = SoundDeviceInputBackend()

    with pytest.raises(AudioDeviceError, match="microphone unavailable"):
        backend.start(lambda frame: None, lambda error: None)
