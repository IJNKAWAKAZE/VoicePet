"""语音活动检测器协议和 WebRTC 适配器"""

from __future__ import annotations

from typing import Protocol

from .audio_types import AudioConfigurationError, AudioFormat


class VoiceActivityDetector(Protocol):
    """判断单个固定长度 PCM 帧是否包含语音"""

    def is_speech(self, pcm: bytes, audio_format: AudioFormat) -> bool: ...


class WebRtcVadDetector:
    """延迟加载 webrtcvad 的本地语音检测器"""

    def __init__(self, aggressiveness: int = 2) -> None:
        if aggressiveness not in range(4):
            raise AudioConfigurationError("VAD 激进程度必须在 0 到 3 之间")
        try:
            import webrtcvad
        except (ImportError, ModuleNotFoundError) as error:
            raise AudioConfigurationError(
                "缺少 VAD 依赖，请安装 voicepet[audio]"
            ) from error

        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, pcm: bytes, audio_format: AudioFormat) -> bool:
        audio_format.validate_frame(pcm)
        return bool(self._vad.is_speech(pcm, audio_format.sample_rate))
