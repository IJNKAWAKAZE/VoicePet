"""基于语音活动检测的录音会话"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import Protocol

from .audio_input import AudioSubscription
from .audio_types import AudioConfigurationError, AudioFormat
from .cancellation import CancellationToken
from .vad import VoiceActivityDetector


class CaptureService(Protocol):
    """录音会话所需的采集服务最小边界"""

    def subscribe(
        self,
        *,
        include_preroll: bool = True,
        queue_capacity: int = 100,
        drop_oldest_on_overflow: bool = False,
    ) -> AudioSubscription: ...


class VadAudioSession:
    """从共享采集流中截取一段连续语音"""

    def __init__(
        self,
        capture_service: CaptureService,
        detector: VoiceActivityDetector,
        audio_format: AudioFormat | None = None,
        *,
        preroll_ms: int = 400,
        speech_start_timeout_ms: int = 5_000,
        silence_duration_ms: int = 800,
        max_duration_ms: int = 15_000,
        speech_start_frames: int = 3,
    ) -> None:
        self._capture_service = capture_service
        self._detector = detector
        self._audio_format = audio_format or AudioFormat()
        self._preroll_frames = self._audio_format.frames_for_ms(preroll_ms)
        self._speech_start_timeout_frames = self._audio_format.frames_for_ms(
            speech_start_timeout_ms
        )
        self._silence_frames = self._audio_format.frames_for_ms(
            silence_duration_ms
        )
        self._max_frames = self._audio_format.frames_for_ms(max_duration_ms)
        if speech_start_frames <= 0:
            raise AudioConfigurationError("语音开始确认帧数必须大于零")
        if speech_start_frames > self._speech_start_timeout_frames:
            raise AudioConfigurationError("语音开始确认帧数不能超过等待上限")
        self._speech_start_frames = speech_start_frames

    async def record_until_silence(
        self,
        token: CancellationToken,
        *,
        include_preroll: bool = True,
        on_started: Callable[[], Awaitable[None]] | None = None,
    ) -> bytes:
        subscription = self._capture_service.subscribe(
            include_preroll=include_preroll,
            queue_capacity=self._max_frames + self._preroll_frames,
        )
        leading = deque[bytes](maxlen=self._preroll_frames)
        recorded: list[bytes] = []
        frames_seen = 0
        voiced_run = 0
        silence_run = 0
        speech_started = False

        try:
            while frames_seen < self._max_frames:
                token.throw_if_cancelled()
                frame = await subscription.read(token)
                token.throw_if_cancelled()
                self._audio_format.validate_frame(frame.pcm)
                # 收到有效音频帧后才通知界面，设备未就绪时不显示正在听
                if frames_seen == 0 and on_started is not None:
                    await on_started()
                    token.throw_if_cancelled()
                frames_seen += 1
                is_speech = self._detector.is_speech(
                    frame.pcm,
                    self._audio_format,
                )

                if not speech_started:
                    leading.append(frame.pcm)
                    voiced_run = voiced_run + 1 if is_speech else 0
                    if voiced_run >= self._speech_start_frames:
                        speech_started = True
                        recorded.extend(leading)
                        leading.clear()
                    elif frames_seen >= self._speech_start_timeout_frames:
                        return b""
                    continue

                recorded.append(frame.pcm)
                silence_run = 0 if is_speech else silence_run + 1
                if silence_run >= self._silence_frames:
                    break

            return b"".join(recorded)
        finally:
            # 无论正常结束、错误还是取消都必须释放订阅
            subscription.close()
