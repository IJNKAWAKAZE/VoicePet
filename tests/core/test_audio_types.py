from dataclasses import FrozenInstanceError

import pytest

from core.audio_types import (
    AudioConfigurationError,
    AudioDeviceError,
    AudioFormat,
    AudioFrameError,
    AudioOverflowError,
    CapturedFrame,
)


def test_default_audio_format_has_expected_frame_geometry():
    audio_format = AudioFormat()

    assert audio_format.sample_rate == 16_000
    assert audio_format.channels == 1
    assert audio_format.sample_width == 2
    assert audio_format.frame_duration_ms == 20
    assert audio_format.frame_samples == 320
    assert audio_format.frame_bytes == 640
    assert audio_format.frames_for_ms(400) == 20


@pytest.mark.parametrize(
    "values",
    [
        {"sample_rate": 44_100},
        {"channels": 2},
        {"sample_width": 1},
        {"frame_duration_ms": 15},
    ],
)
def test_audio_format_rejects_unsupported_values(values):
    with pytest.raises(AudioConfigurationError):
        AudioFormat(**values)


def test_duration_must_be_positive_and_divisible_by_frame_duration():
    audio_format = AudioFormat()

    with pytest.raises(AudioConfigurationError):
        audio_format.frames_for_ms(0)
    with pytest.raises(AudioConfigurationError):
        audio_format.frames_for_ms(25)


def test_validate_frame_rejects_incorrect_byte_length():
    audio_format = AudioFormat()

    audio_format.validate_frame(bytes(audio_format.frame_bytes))
    with pytest.raises(AudioFrameError, match="640"):
        audio_format.validate_frame(b"short")


def test_captured_frame_and_format_are_immutable():
    audio_format = AudioFormat()
    frame = CapturedFrame(1, 12.5, bytes(audio_format.frame_bytes))

    with pytest.raises(FrozenInstanceError):
        frame.sequence = 2
    with pytest.raises(FrozenInstanceError):
        audio_format.sample_rate = 8_000


def test_audio_errors_expose_stable_codes():
    assert AudioConfigurationError.code == "audio.configuration"
    assert AudioDeviceError.code == "audio.device"
    assert AudioOverflowError.code == "audio.overflow"
    assert AudioFrameError.code == "audio.frame"
