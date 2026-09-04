"""音频采集共享的数据类型和错误"""

from dataclasses import dataclass


class AudioError(RuntimeError):
    """音频子系统可向运行时报告的基础错误"""

    code = "audio.error"
    retryable = False


class AudioConfigurationError(AudioError):
    """音频参数或可选依赖配置错误"""

    code = "audio.configuration"


class AudioDeviceError(AudioError):
    """音频设备启动、读取或停止失败"""

    code = "audio.device"
    retryable = True


class AudioOverflowError(AudioError):
    """录音消费者落后导致有界缓冲区溢出"""

    code = "audio.overflow"
    retryable = True


class AudioFrameError(AudioError):
    """PCM 帧尺寸或内容不符合内部格式"""

    code = "audio.frame"


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """VoicePet 内部使用的固定 PCM 帧格式"""

    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    frame_duration_ms: int = 20

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise AudioConfigurationError("内部采样率必须为 16000Hz")
        if self.channels != 1:
            raise AudioConfigurationError("内部音频必须为单声道")
        if self.sample_width != 2:
            raise AudioConfigurationError("内部音频必须为 PCM16")
        if self.frame_duration_ms != 20:
            raise AudioConfigurationError("内部帧长必须为 20ms")

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_duration_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.channels * self.sample_width

    def frames_for_ms(self, duration_ms: int) -> int:
        if duration_ms <= 0:
            raise AudioConfigurationError("音频时长必须大于零")
        if duration_ms % self.frame_duration_ms:
            raise AudioConfigurationError(
                f"音频时长必须是 {self.frame_duration_ms}ms 的整数倍"
            )
        return duration_ms // self.frame_duration_ms

    def validate_frame(self, pcm: bytes) -> None:
        if len(pcm) != self.frame_bytes:
            raise AudioFrameError(
                f"PCM 帧必须为 {self.frame_bytes} 字节，实际为 {len(pcm)} 字节"
            )


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """带采集顺序和单调时钟时间的 PCM 帧"""

    sequence: int
    captured_at: float
    pcm: bytes
