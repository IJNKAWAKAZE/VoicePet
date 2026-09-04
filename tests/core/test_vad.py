import sys
from types import SimpleNamespace

import pytest

from core.audio_types import AudioConfigurationError, AudioFormat, AudioFrameError
from core.vad import WebRtcVadDetector


def test_webrtc_vad_reports_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "webrtcvad", None)

    with pytest.raises(AudioConfigurationError, match="audio"):
        WebRtcVadDetector()


@pytest.mark.parametrize("aggressiveness", [-1, 4])
def test_webrtc_vad_rejects_invalid_aggressiveness(aggressiveness):
    with pytest.raises(AudioConfigurationError, match="0 到 3"):
        WebRtcVadDetector(aggressiveness)


def test_webrtc_vad_forwards_pcm_and_sample_rate(monkeypatch):
    calls = []

    class FakeVad:
        def __init__(self, mode):
            self.mode = mode

        def is_speech(self, pcm, sample_rate):
            calls.append((pcm, sample_rate, self.mode))
            return True

    monkeypatch.setitem(sys.modules, "webrtcvad", SimpleNamespace(Vad=FakeVad))
    detector = WebRtcVadDetector(3)
    audio_format = AudioFormat()
    pcm = bytes(audio_format.frame_bytes)

    assert detector.is_speech(pcm, audio_format)
    assert calls == [(pcm, 16_000, 3)]


def test_webrtc_vad_rejects_invalid_frame_before_native_call(monkeypatch):
    class FakeVad:
        def __init__(self, mode):
            self.mode = mode

        def is_speech(self, pcm, sample_rate):
            raise AssertionError("native VAD must not receive invalid frames")

    monkeypatch.setitem(sys.modules, "webrtcvad", SimpleNamespace(Vad=FakeVad))
    detector = WebRtcVadDetector()

    with pytest.raises(AudioFrameError):
        detector.is_speech(b"short", AudioFormat())
