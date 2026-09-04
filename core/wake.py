"""本地唤醒检测和统一激活入口"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .audio_session import CaptureService
from .audio_types import AudioError, AudioFormat, AudioFrameError
from .cancellation import CancellationSource, CancelledError
from .config import WakeWordConfig, normalize_wake_keyword
from .event_bus import EventBus, Subscription
from .events import ConversationPhase, StateChanged, TurnId
from .wake_models import (
    WakeDownloadProgress,
    WakeModelFiles,
    WakeModelState,
    WakeModelStore,
)


class WakeError(RuntimeError):
    """唤醒子系统可报告的基础错误"""

    code = "wake.error"
    retryable = False


class WakeConfigurationError(WakeError):
    """唤醒参数、依赖或模型配置错误"""

    code = "wake.configuration"


class WakeDetectionError(WakeError):
    """唤醒模型执行失败"""

    code = "wake.detection"
    retryable = True


class WakeRuntimeStatus(str, Enum):
    """中文唤醒服务当前可展示的运行状态"""

    DISABLED = "disabled"
    MISSING = "missing"
    DOWNLOADING = "downloading"
    LOADING = "loading"
    LISTENING = "listening"
    FAILED = "failed"


class ActivationTarget(Protocol):
    """统一激活控制器需要的 Coordinator 边界"""

    @property
    def phase(self) -> ConversationPhase: ...

    async def start_listening(self, source: str = "click") -> TurnId: ...

    async def interrupt(self, source: str = "click") -> TurnId: ...


class WakeWordDetector(Protocol):
    """消费增量 PCM 并返回匹配的唤醒词"""

    def accept(self, pcm: bytes, audio_format: AudioFormat) -> str | None: ...

    def set_speaking(self, speaking: bool) -> None: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


WakeCallback = Callable[[str], object | Awaitable[object]]
WakeErrorCallback = Callable[[Exception], object | Awaitable[object]]


def strip_wake_keyword_prefix(transcript: str, keyword: str) -> str:
    """只移除转写句首匹配的中文唤醒词"""

    normalized_keyword = normalize_wake_keyword(keyword)
    compact = transcript.lstrip()
    separators = " ，。！？、,.!?：:；;"
    cursor = 0
    matched = 0
    while cursor < len(compact) and matched < len(normalized_keyword):
        character = compact[cursor]
        if character.isspace() or character in separators:
            cursor += 1
            continue
        if character != normalized_keyword[matched]:
            return transcript.strip()
        cursor += 1
        matched += 1
    if matched != len(normalized_keyword):
        return transcript.strip()
    return compact[cursor:].lstrip(separators + "啊呀呢吧")


def prepare_transcript_for_source(
    transcript: str,
    *,
    source: str,
    keyword: str,
) -> str:
    """仅为语音唤醒来源清理转写句首"""

    if source != "wake_word":
        return transcript.strip()
    return strip_wake_keyword_prefix(transcript, keyword)


class ActivationController:
    """串行化唤醒词、点击和快捷键激活"""

    def __init__(self, target: ActivationTarget) -> None:
        self._target = target
        self._lock = asyncio.Lock()

    async def activate(self, source: str) -> TurnId:
        async with self._lock:
            if self._target.phase is ConversationPhase.IDLE:
                return await self._target.start_listening(source)
            return await self._target.interrupt(source)


class WakeWordMonitor:
    """从共享实时音频流连续检测唤醒词"""

    def __init__(
        self,
        capture_service: CaptureService,
        detector: WakeWordDetector,
        on_wake: WakeCallback,
        audio_format: AudioFormat | None = None,
        *,
        debounce_sec: float = 1.5,
        retry_delay_sec: float = 0.5,
        on_error: WakeErrorCallback | None = None,
    ) -> None:
        if debounce_sec < 0:
            raise WakeConfigurationError("唤醒防抖时长不能为负数")
        if retry_delay_sec < 0:
            raise WakeConfigurationError("唤醒恢复等待时长不能为负数")
        self._audio_format = audio_format or AudioFormat()
        self._capture_service = capture_service
        self._detector = detector
        self._on_wake = on_wake
        self._on_error = on_error
        self._debounce_sec = debounce_sec
        self._retry_delay_sec = retry_delay_sec
        self._source: CancellationSource | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        source = CancellationSource()
        self._source = source
        self._task = asyncio.create_task(
            self._run(source),
            name="voicepet-wake-monitor",
        )
        self._task.add_done_callback(self._task_finished)

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        source = self._source
        if source is not None:
            source.cancel("wake_monitor_stop")
        await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._source = None

    async def _run(self, source: CancellationSource) -> None:
        last_wake_at = float("-inf")
        while True:
            subscription = None
            try:
                subscription = self._capture_service.subscribe(
                    include_preroll=False,
                    queue_capacity=100,
                    drop_oldest_on_overflow=True,
                )
                while True:
                    frame = await subscription.read(source.token)
                    self._audio_format.validate_frame(frame.pcm)
                    try:
                        keyword = self._detector.accept(
                            frame.pcm,
                            self._audio_format,
                        )
                    except (AudioError, WakeError):
                        raise
                    except Exception as error:
                        raise WakeDetectionError(
                            f"本地唤醒检测失败: {error}"
                        ) from error

                    if keyword is None:
                        continue
                    if frame.captured_at - last_wake_at < self._debounce_sec:
                        continue
                    last_wake_at = frame.captured_at
                    self._detector.reset()
                    result = self._on_wake("wake_word")
                    if inspect.isawaitable(result):
                        await result
            except CancelledError:
                return
            except Exception as error:  # noqa: BLE001 监听中断后需要自动恢复
                mapped = (
                    error
                    if isinstance(error, (AudioError, WakeError))
                    else WakeDetectionError(f"本地唤醒监视失败: {error}")
                )
                await self._report_error(mapped)
                try:
                    self._detector.reset()
                except Exception as reset_error:  # noqa: BLE001 原生模型边界
                    await self._report_error(
                        WakeDetectionError(
                            f"本地唤醒检测器复位失败: {reset_error}"
                        )
                    )
                if not await self._wait_before_retry(source):
                    return
            finally:
                if subscription is not None:
                    subscription.close()

    async def _wait_before_retry(self, source: CancellationSource) -> bool:
        if self._retry_delay_sec == 0:
            if source.token.is_cancelled:
                return False
            await asyncio.sleep(0)
            return not source.token.is_cancelled
        try:
            await asyncio.wait_for(
                source.token.wait(),
                timeout=self._retry_delay_sec,
            )
        except TimeoutError:
            return True
        return False

    async def _report_error(self, error: Exception) -> None:
        if self._on_error is None:
            return
        try:
            result = self._on_error(error)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 日志回调不能终止唤醒监听
            return

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()


DetectorFactory = Callable[[WakeModelFiles, WakeWordConfig], WakeWordDetector]
MonitorFactory = Callable[[WakeWordDetector, WakeWordConfig], WakeWordMonitor]


class WakeWordRuntimeService:
    """原子管理中文检测器、监听任务与模型下载"""

    def __init__(
        self,
        model_store: WakeModelStore,
        event_bus: EventBus,
        config: WakeWordConfig,
        *,
        detector_factory: DetectorFactory,
        monitor_factory: MonitorFactory,
    ) -> None:
        self._validate_config(config)
        self._model_store = model_store
        self._event_bus = event_bus
        self._config = config
        self._detector_factory = detector_factory
        self._monitor_factory = monitor_factory
        self._detector: WakeWordDetector | None = None
        self._monitor: WakeWordMonitor | None = None
        self._state_subscription: Subscription | None = None
        self._download_source: CancellationSource | None = None
        self._started = False
        self._status = (
            WakeRuntimeStatus.DISABLED
            if not config.enabled
            else WakeRuntimeStatus.MISSING
        )

    @property
    def status(self) -> WakeRuntimeStatus:
        return self._status

    def model_state(self) -> WakeModelState:
        return self._model_store.state()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._state_subscription = self._event_bus.subscribe(
            StateChanged,
            self._state_changed,
        )
        try:
            await self._apply_config()
        except Exception:  # noqa: BLE001 唤醒启动失败必须降级而不能阻断 Runtime
            self._status = WakeRuntimeStatus.FAILED

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.cancel_download()
        subscription, self._state_subscription = self._state_subscription, None
        if subscription is not None:
            subscription.close()
        await self._stop_active()

    async def configure(self, config: WakeWordConfig) -> None:
        self._validate_config(config)
        previous = self._config
        self._config = config
        if self._started:
            try:
                await self._apply_config()
            except Exception:
                self._config = previous
                raise

    async def download_model(
        self,
        progress: Callable[[WakeDownloadProgress], None],
    ) -> None:
        if self._download_source is not None:
            raise WakeConfigurationError("中文唤醒模型正在下载")
        source = CancellationSource()
        self._download_source = source
        self._status = WakeRuntimeStatus.DOWNLOADING
        try:
            package = await self._model_store.download(progress, source.token)
            source.token.throw_if_cancelled()
            self._model_store.install_package(package)
            self._status = WakeRuntimeStatus.LOADING
            if self._started:
                await self._apply_config()
        except CancelledError:
            self._status = self._status_for_available_model()
            raise
        except Exception:
            self._status = WakeRuntimeStatus.FAILED
            raise
        finally:
            self._download_source = None

    def cancel_download(self) -> None:
        if self._download_source is not None:
            self._download_source.cancel("wake_model_download_cancelled")

    async def _apply_config(self) -> None:
        if not self._config.enabled:
            await self._stop_active()
            self._status = WakeRuntimeStatus.DISABLED
            return
        state = self._model_store.state()
        if state is WakeModelState.CACHED:
            self._status = WakeRuntimeStatus.LOADING
            self._model_store.install_package(self._model_store.package_path)
            state = self._model_store.state()
        if state is not WakeModelState.READY:
            await self._stop_active()
            self._status = WakeRuntimeStatus.MISSING
            return
        self._status = WakeRuntimeStatus.LOADING
        old_detector = self._detector
        old_monitor = self._monitor
        new_detector: WakeWordDetector | None = None
        try:
            new_detector = self._detector_factory(
                self._model_store.files(),
                self._config,
            )
            new_monitor = self._monitor_factory(new_detector, self._config)
            await new_monitor.start()
        except Exception:
            if new_detector is not None:
                new_detector.close()
            self._status = (
                WakeRuntimeStatus.LISTENING
                if old_monitor is not None
                else WakeRuntimeStatus.FAILED
            )
            raise
        self._detector = new_detector
        self._monitor = new_monitor
        self._status = WakeRuntimeStatus.LISTENING
        if old_monitor is not None:
            await old_monitor.stop()
        if old_detector is not None:
            old_detector.close()

    async def _stop_active(self) -> None:
        monitor, self._monitor = self._monitor, None
        detector, self._detector = self._detector, None
        if monitor is not None:
            await monitor.stop()
        if detector is not None:
            detector.close()

    def _state_changed(self, event: StateChanged) -> None:
        detector = self._detector
        if detector is None:
            return
        detector.set_speaking(
            event.current
            in {
                ConversationPhase.SYNTHESIZING,
                ConversationPhase.SPEAKING,
            }
        )
        if event.current is ConversationPhase.IDLE:
            detector.reset()

    def _status_for_available_model(self) -> WakeRuntimeStatus:
        if not self._config.enabled:
            return WakeRuntimeStatus.DISABLED
        if self._monitor is not None:
            return WakeRuntimeStatus.LISTENING
        return WakeRuntimeStatus.MISSING

    @staticmethod
    def _validate_config(config: WakeWordConfig) -> None:
        if type(config.enabled) is not bool:
            raise WakeConfigurationError("语音唤醒开关必须是布尔值")
        normalize_wake_keyword(config.keyword)
        if not 0 <= config.sensitivity <= 1:
            raise WakeConfigurationError("唤醒灵敏度必须在零到一之间")
        if config.debounce_sec <= 0:
            raise WakeConfigurationError("唤醒防抖时长必须大于零")


class SherpaOnnxKeywordDetector:
    """使用通用中文模型执行增量关键词识别"""

    def __init__(
        self,
        files: WakeModelFiles,
        *,
        keyword: str,
        sensitivity: float = 0.5,
        num_threads: int = 1,
    ) -> None:
        if not 0 <= sensitivity <= 1:
            raise WakeConfigurationError("唤醒灵敏度必须在零到一之间")
        if num_threads <= 0:
            raise WakeConfigurationError("唤醒推理线程数必须大于零")
        for path in (files.tokens, files.encoder, files.decoder, files.joiner):
            if not path.is_file():
                raise WakeConfigurationError("中文唤醒模型文件不存在")
        try:
            import numpy
            import sherpa_onnx
        except (ImportError, ModuleNotFoundError) as error:
            raise WakeConfigurationError(
                "缺少中文唤醒依赖，请安装 voicepet[wake]"
            ) from error
        normalized = normalize_wake_keyword(keyword)
        try:
            encoded = sherpa_onnx.text2token(
                [normalized],
                tokens=str(files.tokens),
                tokens_type="ppinyin",
            )
        except Exception as error:
            raise WakeConfigurationError("中文唤醒词转换失败") from error
        if not encoded or not encoded[0] or any(
            not isinstance(token, str) or not token.strip()
            for token in encoded[0]
        ):
            raise WakeConfigurationError("中文唤醒词转换失败")
        keywords_file: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                delete=False,
            ) as handle:
                keywords_file = Path(handle.name)
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(files.tokens),
                encoder=str(files.encoder),
                decoder=str(files.decoder),
                joiner=str(files.joiner),
                keywords_file=str(keywords_file),
                num_threads=num_threads,
                max_active_paths=4,
                provider="cpu",
            )
        except Exception as error:
            raise WakeConfigurationError("中文唤醒模型加载失败") from error
        finally:
            if keywords_file is not None:
                with suppress(OSError):
                    keywords_file.unlink(missing_ok=True)
        self._numpy = numpy
        self._keyword = keyword
        self._tokens = tuple(encoded[0])
        self._base_threshold = 0.45 - 0.4 * sensitivity
        self._speaking = False
        self._closed = False
        self._stream = self._create_stream()

    def accept(self, pcm: bytes, audio_format: AudioFormat) -> str | None:
        if self._closed:
            raise WakeDetectionError("中文唤醒检测器已关闭")
        if audio_format.sample_rate != 16_000 or audio_format.channels != 1:
            raise AudioFrameError("中文唤醒只支持 16kHz 单声道音频")
        audio_format.validate_frame(pcm)
        samples = self._numpy.frombuffer(pcm, dtype="<i2").astype(
            self._numpy.float32
        )
        samples /= 32768.0
        try:
            self._stream.accept_waveform(audio_format.sample_rate, samples)
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
                if self._spotter.get_result(self._stream):
                    return self._keyword
        except Exception as error:
            raise WakeDetectionError(f"中文唤醒推理失败: {error}") from error
        return None

    def set_speaking(self, speaking: bool) -> None:
        if self._closed or speaking == self._speaking:
            return
        self._speaking = speaking
        self._stream = self._create_stream()

    def reset(self) -> None:
        if not self._closed:
            self._spotter.reset_stream(self._stream)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream = None
        self._spotter = None

    def _create_stream(self):
        threshold = min(
            0.95,
            self._base_threshold + (0.15 if self._speaking else 0.0),
        )
        specification = (
            f"{' '.join(self._tokens)} :1.0 #{threshold:.3f} @{self._keyword}"
        )
        try:
            return self._spotter.create_stream(specification)
        except Exception as error:
            raise WakeConfigurationError("中文唤醒流创建失败") from error
