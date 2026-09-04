"""共享麦克风采集服务和有界音频订阅"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from .audio_types import (
    AudioConfigurationError,
    AudioDeviceError,
    AudioError,
    AudioFormat,
    AudioOverflowError,
    CapturedFrame,
)
from .cancellation import CancellationToken

FrameCallback = Callable[[bytes], None]
ErrorCallback = Callable[[Exception], None]
_WAKE_READER = object()


class AudioInputBackend(Protocol):
    """底层音频输入库需要实现的同步边界"""

    def start(
        self,
        on_frame: FrameCallback,
        on_error: ErrorCallback,
    ) -> None: ...

    def stop(self) -> None: ...


class SoundDeviceInputBackend:
    """基于 sounddevice 的 PCM16 麦克风输入适配器"""

    def __init__(
        self,
        audio_format: AudioFormat | None = None,
        *,
        device: int | str | None = None,
    ) -> None:
        try:
            import sounddevice
        except (ImportError, ModuleNotFoundError) as error:
            raise AudioConfigurationError(
                "缺少音频依赖，请安装 voicepet[audio]"
            ) from error

        self._sounddevice = sounddevice
        self._audio_format = audio_format or AudioFormat()
        self._device = device
        self._stream: object | None = None

    def start(
        self,
        on_frame: FrameCallback,
        on_error: ErrorCallback,
    ) -> None:
        if self._stream is not None:
            return

        def callback(indata, frames, time_info, status) -> None:
            del frames, time_info
            if status:
                on_error(AudioDeviceError(f"麦克风采集状态异常: {status}"))
                return
            on_frame(bytes(indata))

        try:
            stream = self._sounddevice.RawInputStream(
                samplerate=self._audio_format.sample_rate,
                blocksize=self._audio_format.frame_samples,
                channels=self._audio_format.channels,
                dtype="int16",
                device=self._device,
                callback=callback,
            )
            stream.start()
        except Exception as error:
            raise AudioDeviceError(f"麦克风启动失败: {error}") from error
        self._stream = stream

    def stop(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        stop_error: Exception | None = None
        try:
            stream.stop()
        except Exception as error:  # noqa: BLE001 原生音频库属于外部边界
            stop_error = error
        try:
            stream.close()
        except Exception as error:  # noqa: BLE001 原生音频库属于外部边界
            if stop_error is None:
                stop_error = error
        if stop_error is not None:
            raise AudioDeviceError(f"麦克风停止失败: {stop_error}") from stop_error


class AudioSubscription:
    """按预录音和实时顺序读取 PCM 帧的订阅"""

    def __init__(
        self,
        preroll: tuple[CapturedFrame, ...],
        queue_capacity: int,
        on_close: Callable[[AudioSubscription], None],
        *,
        drop_oldest_on_overflow: bool = False,
    ) -> None:
        if queue_capacity <= 0:
            raise AudioConfigurationError("订阅队列容量必须大于零")
        self._preroll = deque(preroll)
        self._queue: asyncio.Queue[CapturedFrame | object] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._on_close = on_close
        self._drop_oldest_on_overflow = drop_oldest_on_overflow
        self._error: AudioError | None = None
        self._closed = False

    async def read(self, token: CancellationToken) -> CapturedFrame:
        token.throw_if_cancelled()
        if self._error is not None:
            raise self._error
        if self._preroll:
            return self._preroll.popleft()
        if self._closed:
            raise AudioDeviceError("订阅已关闭")

        read_task = asyncio.create_task(self._queue.get())
        cancel_task = asyncio.create_task(token.wait())
        try:
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
            item = read_task.result()
            if item is _WAKE_READER:
                if self._error is not None:
                    raise self._error
                raise AudioDeviceError("订阅已关闭")
            assert isinstance(item, CapturedFrame)
            return item
        finally:
            for task in (read_task, cancel_task):
                if not task.done():
                    task.cancel()

    def close(self) -> None:
        self._close_with_error(AudioDeviceError("订阅已关闭"))

    def _feed(self, frame: CapturedFrame) -> None:
        if self._closed:
            return
        if self._queue.full():
            if self._drop_oldest_on_overflow:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
                return
            self._close_with_error(
                AudioOverflowError("录音订阅队列已满，音频连续性无法保证")
            )
            return
        self._queue.put_nowait(frame)

    def _close_with_error(self, error: AudioError) -> None:
        if self._closed:
            return
        self._closed = True
        self._error = error
        self._preroll.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(_WAKE_READER)
        self._on_close(self)


class AudioCaptureService:
    """维护预录音环形缓冲并向多个消费者分发音频帧"""

    def __init__(
        self,
        backend: AudioInputBackend,
        audio_format: AudioFormat | None = None,
        *,
        preroll_ms: int = 400,
    ) -> None:
        self.audio_format = audio_format or AudioFormat()
        self._backend = backend
        self._preroll = deque[
            CapturedFrame
        ](maxlen=self.audio_format.frames_for_ms(preroll_ms))
        self._subscriptions: set[AudioSubscription] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sequence = 0
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._started = True
        try:
            await asyncio.to_thread(
                self._backend.start,
                self._receive_backend_frame,
                self._receive_backend_error,
            )
        except AudioError:
            self._started = False
            raise
        except Exception as error:
            self._started = False
            raise AudioDeviceError(f"音频输入启动失败: {error}") from error

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        stop_error: AudioError | None = None
        try:
            await asyncio.to_thread(self._backend.stop)
        except AudioError as error:
            stop_error = error
        except Exception as error:  # noqa: BLE001 底层音频库属于外部边界
            stop_error = AudioDeviceError(f"音频输入停止失败: {error}")
        finally:
            for subscription in tuple(self._subscriptions):
                subscription._close_with_error(
                    AudioDeviceError("采集服务已停止")
                )
            self._subscriptions.clear()
            self._loop = None

        if stop_error is not None:
            raise stop_error

    def subscribe(
        self,
        *,
        include_preroll: bool = True,
        queue_capacity: int = 100,
        drop_oldest_on_overflow: bool = False,
    ) -> AudioSubscription:
        if not self._started:
            raise AudioDeviceError("采集服务尚未启动")
        preroll = tuple(self._preroll) if include_preroll else ()
        subscription = AudioSubscription(
            preroll,
            queue_capacity,
            self._remove_subscription,
            drop_oldest_on_overflow=drop_oldest_on_overflow,
        )
        self._subscriptions.add(subscription)
        return subscription

    def _remove_subscription(self, subscription: AudioSubscription) -> None:
        self._subscriptions.discard(subscription)

    def _receive_backend_frame(self, pcm: bytes) -> None:
        loop = self._loop
        if loop is None or not self._started:
            return
        captured_at = time.monotonic()
        try:
            loop.call_soon_threadsafe(self._ingest_frame, bytes(pcm), captured_at)
        except RuntimeError:
            return

    def _receive_backend_error(self, error: Exception) -> None:
        loop = self._loop
        if loop is None or not self._started:
            return
        mapped = (
            error
            if isinstance(error, AudioError)
            else AudioDeviceError(f"音频输入失败: {error}")
        )
        try:
            loop.call_soon_threadsafe(self._fail_subscriptions, mapped)
        except RuntimeError:
            return

    def _ingest_frame(self, pcm: bytes, captured_at: float) -> None:
        if not self._started:
            return
        try:
            self.audio_format.validate_frame(pcm)
        except AudioError as error:
            self._fail_subscriptions(error)
            return

        self._sequence += 1
        frame = CapturedFrame(self._sequence, captured_at, pcm)
        self._preroll.append(frame)
        for subscription in tuple(self._subscriptions):
            subscription._feed(frame)

    def _fail_subscriptions(self, error: AudioError) -> None:
        for subscription in tuple(self._subscriptions):
            subscription._close_with_error(error)
