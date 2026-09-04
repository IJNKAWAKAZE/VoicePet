import asyncio
from collections import deque

import pytest

from core.audio_session import VadAudioSession
from core.audio_types import AudioConfigurationError, AudioFormat, CapturedFrame
from core.cancellation import CancellationSource, CancelledError


def make_frame(sequence: int, *, speech: bool = False) -> CapturedFrame:
    value = 220 if speech else sequence % 100
    audio_format = AudioFormat()
    return CapturedFrame(
        sequence,
        sequence * 0.02,
        bytes([value]) * audio_format.frame_bytes,
    )


class SequenceSubscription:
    def __init__(self, frames) -> None:
        self.frames = deque(frames)
        self.close_calls = 0

    async def read(self, token):
        token.throw_if_cancelled()
        if not self.frames:
            raise AssertionError("test frame sequence exhausted")
        return self.frames.popleft()

    def close(self) -> None:
        self.close_calls += 1


class SequenceCaptureService:
    def __init__(self, subscription) -> None:
        self.subscription = subscription
        self.subscribe_calls = []

    def subscribe(self, **kwargs):
        self.subscribe_calls.append(kwargs)
        return self.subscription


class ThresholdDetector:
    def is_speech(self, pcm, audio_format):
        audio_format.validate_frame(pcm)
        return pcm[0] >= 200


class BlockingSubscription:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.close_calls = 0

    async def read(self, token):
        self.started.set()
        await token.wait()
        token.throw_if_cancelled()
        raise AssertionError("cancelled read must not continue")

    def close(self) -> None:
        self.close_calls += 1


def test_session_keeps_latest_400ms_before_confirmed_speech():
    frames = [make_frame(index) for index in range(1, 26)]
    frames += [make_frame(index, speech=True) for index in range(26, 29)]
    frames += [make_frame(index) for index in range(29, 69)]
    subscription = SequenceSubscription(frames)
    session = VadAudioSession(
        SequenceCaptureService(subscription),
        ThresholdDetector(),
    )

    pcm = asyncio.run(
        session.record_until_silence(CancellationSource().token)
    )
    audio_format = AudioFormat()

    assert len(pcm) // audio_format.frame_bytes == 60
    assert pcm[0] == 9
    assert subscription.close_calls == 1


def test_speech_start_requires_three_consecutive_voiced_frames():
    frames = [
        make_frame(1, speech=True),
        make_frame(2, speech=True),
        make_frame(3),
        make_frame(4, speech=True),
        make_frame(5, speech=True),
        make_frame(6, speech=True),
        make_frame(7),
        make_frame(8),
        make_frame(9),
    ]
    subscription = SequenceSubscription(frames)
    session = VadAudioSession(
        SequenceCaptureService(subscription),
        ThresholdDetector(),
        preroll_ms=200,
        silence_duration_ms=60,
        speech_start_timeout_ms=200,
        max_duration_ms=400,
    )

    pcm = asyncio.run(
        session.record_until_silence(CancellationSource().token)
    )

    assert len(pcm) // AudioFormat().frame_bytes == 9


def test_voiced_frame_resets_consecutive_silence_counter():
    frames = [make_frame(index, speech=True) for index in range(1, 4)]
    frames += [make_frame(4), make_frame(5)]
    frames += [make_frame(6, speech=True)]
    frames += [make_frame(7), make_frame(8), make_frame(9)]
    session = VadAudioSession(
        SequenceCaptureService(SequenceSubscription(frames)),
        ThresholdDetector(),
        silence_duration_ms=60,
        max_duration_ms=400,
    )

    pcm = asyncio.run(
        session.record_until_silence(CancellationSource().token)
    )

    assert len(pcm) // AudioFormat().frame_bytes == 9


def test_no_speech_for_five_seconds_returns_empty_audio():
    frames = [make_frame(index) for index in range(1, 251)]
    subscription = SequenceSubscription(frames)
    session = VadAudioSession(
        SequenceCaptureService(subscription),
        ThresholdDetector(),
    )

    pcm = asyncio.run(
        session.record_until_silence(CancellationSource().token)
    )

    assert pcm == b""
    assert subscription.close_calls == 1


def test_session_can_start_without_shared_preroll():
    frames = [make_frame(index, speech=True) for index in range(1, 4)]
    frames += [make_frame(index) for index in range(4, 7)]
    capture = SequenceCaptureService(SequenceSubscription(frames))
    session = VadAudioSession(
        capture,
        ThresholdDetector(),
        silence_duration_ms=60,
        max_duration_ms=400,
    )

    asyncio.run(
        session.record_until_silence(
            CancellationSource().token,
            include_preroll=False,
        )
    )

    assert capture.subscribe_calls[0]["include_preroll"] is False


def test_recording_stops_at_fifteen_second_limit():
    frames = [make_frame(index, speech=True) for index in range(1, 751)]
    session = VadAudioSession(
        SequenceCaptureService(SequenceSubscription(frames)),
        ThresholdDetector(),
    )

    pcm = asyncio.run(
        session.record_until_silence(CancellationSource().token)
    )

    assert len(pcm) // AudioFormat().frame_bytes == 750


def test_cancellation_propagates_and_closes_subscription():
    async def scenario():
        subscription = BlockingSubscription()
        source = CancellationSource()
        session = VadAudioSession(
            SequenceCaptureService(subscription),
            ThresholdDetector(),
        )
        task = asyncio.create_task(session.record_until_silence(source.token))
        await subscription.started.wait()
        source.cancel("user_interrupt")

        with pytest.raises(CancelledError):
            await task
        assert subscription.close_calls == 1

    asyncio.run(scenario())


def test_session_rejects_invalid_timing_configuration():
    capture = SequenceCaptureService(SequenceSubscription([]))
    detector = ThresholdDetector()

    with pytest.raises(AudioConfigurationError):
        VadAudioSession(capture, detector, silence_duration_ms=810)
    with pytest.raises(AudioConfigurationError):
        VadAudioSession(capture, detector, speech_start_frames=0)
