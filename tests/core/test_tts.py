import asyncio
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from core.cancellation import CancellationSource, CancelledError
from core.network_resilience import NetworkResilience, NetworkResilienceSettings
from core.tts import (
    EdgeTtsSynthesizer,
    EdgeTtsVoiceService,
    FallbackSpeechSynthesizer,
    ResilientSpeechSynthesizer,
    SentenceChunker,
    SynthesizedAudio,
    TtsConfigurationError,
    TtsNetworkError,
    TtsPlaybackError,
    TtsSynthesisError,
    TtsVoiceOption,
    WindowsMciAudioPlayer,
    WindowsSapiSynthesizer,
    markdown_to_speech_text,
    split_speech_text,
)


class FakeEdgeStream:
    def __init__(self, chunks, source=None) -> None:
        self.chunks = list(chunks)
        self.source = source
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        chunk = self.chunks.pop(0)
        if self.source is not None:
            self.source.cancel("interrupt")
        return chunk

    async def aclose(self):
        self.closed += 1


class FakeCommunicate:
    def __init__(self, stream) -> None:
        self._stream = stream

    def stream(self):
        return self._stream


def test_synthesized_audio_is_immutable_and_validated():
    audio = SynthesizedAudio(
        data=b"audio",
        media_type="audio/mpeg",
        file_suffix=".mp3",
        backend="edge-tts",
    )

    assert audio.data == b"audio"
    with pytest.raises(FrozenInstanceError):
        audio.data = b"changed"
    with pytest.raises(TtsConfigurationError):
        SynthesizedAudio(b"", "audio/mpeg", ".mp3", "edge-tts")
    with pytest.raises(TtsConfigurationError):
        SynthesizedAudio(b"audio", "", ".mp3", "edge-tts")
    with pytest.raises(TtsConfigurationError):
        SynthesizedAudio(b"audio", "audio/mpeg", "mp3", "edge-tts")


def test_tts_errors_expose_stable_codes_and_retryability():
    assert TtsConfigurationError.code == "tts.configuration"
    assert TtsNetworkError.code == "tts.network"
    assert TtsNetworkError.retryable is True
    assert TtsSynthesisError.code == "tts.synthesis"
    assert TtsPlaybackError.code == "tts.playback"


def test_sentence_chunker_emits_complete_sentences_across_deltas():
    chunker = SentenceChunker(max_chars=100)

    assert chunker.push("你好。今天") == ("你好。",)
    assert chunker.push("很好！Really") == ("今天很好！",)
    assert chunker.push(" good? 剩余") == ("Really good?",)
    assert chunker.finish() == ("剩余",)
    assert chunker.finish() == ()


def test_sentence_chunker_enforces_maximum_chunk_size():
    chunker = SentenceChunker(max_chars=5)

    assert chunker.push("abcdefghijk") == ("abcde", "fghij")
    assert chunker.finish() == ("k",)


def test_sentence_chunker_rejects_invalid_limit_and_ignores_empty_delta():
    with pytest.raises(TtsConfigurationError):
        SentenceChunker(max_chars=0)

    chunker = SentenceChunker()
    assert chunker.push("") == ()
    assert chunker.finish() == ()


def test_markdown_to_speech_text_keeps_readable_content_without_syntax():
    source = (
        "# 结果\n\n"
        "- **已经完成** [查看详情](https://example.com/private)\n"
        "> 使用 `VoicePet`"
    )

    assert markdown_to_speech_text(source) == (
        "结果\n已经完成 查看详情\n使用 VoicePet"
    )


def test_markdown_to_speech_text_keeps_image_alt_and_drops_empty_markup():
    assert (
        markdown_to_speech_text(
            "![桌宠](https://example.com/pet.png)"
        )
        == "桌宠"
    )
    assert markdown_to_speech_text("** ~~ ` ```") == ""


def test_speech_text_stays_in_one_chunk_with_sentences_and_version_numbers():
    text = "系统版本是 Windows 11，Python 版本是 3.12.14。已经读取完成。"

    assert split_speech_text(text) == (text,)


def test_speech_text_splits_long_content_at_natural_boundary():
    first = "甲" * 900
    second = "乙" * 700

    assert split_speech_text(
        f"{first}\n\n{second}",
        max_chars=1500,
    ) == (first, second)


def test_speech_text_rejects_invalid_limit_and_drops_blank_text():
    with pytest.raises(TtsConfigurationError):
        split_speech_text("内容", max_chars=0)

    assert split_speech_text("  \n ") == ()


def test_edge_tts_sends_voice_settings_and_collects_only_audio_chunks():
    calls = []
    stream = FakeEdgeStream(
        [
            {"type": "audio", "data": b"first"},
            {"type": "SentenceBoundary", "text": "你好"},
            {"type": "audio", "data": b"second"},
        ]
    )

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeCommunicate(stream)

    synthesizer = EdgeTtsSynthesizer(
        communicate_factory=factory,
        voice="zh-CN-XiaoxiaoNeural",
        rate="+5%",
        volume="-2%",
        pitch="+1Hz",
    )
    source = CancellationSource()

    audio = asyncio.run(synthesizer.synthesize(" 你好 ", source.token))

    assert audio == SynthesizedAudio(
        b"firstsecond",
        "audio/mpeg",
        ".mp3",
        "edge-tts",
    )
    assert calls == [
        (
            ("你好",),
            {
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "+5%",
                "volume": "-2%",
                "pitch": "+1Hz",
            },
        )
    ]
    assert stream.closed == 1


def test_edge_tts_rejects_blank_text_and_empty_audio_stream():
    source = CancellationSource()
    synthesizer = EdgeTtsSynthesizer(
        communicate_factory=lambda *args, **kwargs: FakeCommunicate(
            FakeEdgeStream([])
        )
    )

    with pytest.raises(TtsConfigurationError):
        asyncio.run(synthesizer.synthesize("  ", source.token))
    with pytest.raises(TtsSynthesisError, match="音频"):
        asyncio.run(synthesizer.synthesize("你好", source.token))


def test_edge_tts_cancellation_closes_stream():
    source = CancellationSource()
    stream = FakeEdgeStream(
        [{"type": "audio", "data": b"stale"}],
        source,
    )
    synthesizer = EdgeTtsSynthesizer(
        communicate_factory=lambda *args, **kwargs: FakeCommunicate(stream)
    )

    with pytest.raises(CancelledError):
        asyncio.run(synthesizer.synthesize("你好", source.token))

    assert stream.closed == 1


@pytest.mark.parametrize("error_name", ["ClientConnectionError", "TimeoutError"])
def test_edge_tts_maps_transient_failures_without_leaking_text(error_name):
    error_type = type(error_name, (RuntimeError,), {})

    def factory(*args, **kwargs):
        del args, kwargs
        raise error_type("包含不应泄漏的请求正文")

    synthesizer = EdgeTtsSynthesizer(communicate_factory=factory)
    source = CancellationSource()

    with pytest.raises(TtsNetworkError) as captured:
        asyncio.run(synthesizer.synthesize("秘密正文", source.token))

    assert "请求正文" not in str(captured.value)
    assert "秘密正文" not in str(captured.value)


@pytest.mark.parametrize("status", [408, 429, 500])
def test_edge_tts_maps_transient_http_status_to_network_error(status):
    error = RuntimeError("包含不应泄漏的服务正文")
    error.status = status

    def factory(*args, **kwargs):
        del args, kwargs
        raise error

    synthesizer = EdgeTtsSynthesizer(communicate_factory=factory)

    with pytest.raises(TtsNetworkError) as captured:
        asyncio.run(
            synthesizer.synthesize("秘密正文", CancellationSource().token)
        )

    assert "服务正文" not in str(captured.value)
    assert "秘密正文" not in str(captured.value)


class FakeSapiBackend:
    def __init__(self, result=b"RIFF-wav") -> None:
        self.result = result
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def synthesize(self, text, voice_name, token):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((text, voice_name, token, threading.get_ident()))
            time.sleep(0.01)
            token.throw_if_cancelled()
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        finally:
            with self.lock:
                self.active -= 1


def test_windows_sapi_synthesizer_runs_serially_off_event_loop():
    async def scenario():
        backend = FakeSapiBackend()
        synthesizer = WindowsSapiSynthesizer(
            backend=backend,
            voice_name="Chinese (Simplified)",
        )
        source = CancellationSource()
        event_loop_thread = threading.get_ident()

        first, second = await asyncio.gather(
            synthesizer.synthesize("第一句", source.token),
            synthesizer.synthesize("第二句", source.token),
        )
        synthesizer.close()

        assert first.backend == "windows-sapi"
        assert first.media_type == "audio/wav"
        assert second.data == b"RIFF-wav"
        assert backend.max_active == 1
        assert all(call[3] != event_loop_thread for call in backend.calls)
        assert [call[:2] for call in backend.calls] == [
            ("第一句", "Chinese (Simplified)"),
            ("第二句", "Chinese (Simplified)"),
        ]

    asyncio.run(scenario())


def test_windows_sapi_rejects_blank_empty_and_closed_requests():
    async def scenario():
        source = CancellationSource()
        empty = WindowsSapiSynthesizer(backend=FakeSapiBackend(b""))
        with pytest.raises(TtsConfigurationError):
            await empty.synthesize(" ", source.token)
        with pytest.raises(TtsSynthesisError, match="音频"):
            await empty.synthesize("你好", source.token)
        empty.close()
        with pytest.raises(TtsConfigurationError, match="关闭"):
            await empty.synthesize("你好", source.token)

    asyncio.run(scenario())


def test_windows_sapi_honors_cancellation_and_sanitizes_backend_errors():
    async def scenario():
        cancelled_source = CancellationSource()
        cancelled_source.cancel("interrupt")
        backend = FakeSapiBackend()
        synthesizer = WindowsSapiSynthesizer(backend=backend)
        with pytest.raises(CancelledError):
            await synthesizer.synthesize("你好", cancelled_source.token)
        assert backend.calls == []

        failing = WindowsSapiSynthesizer(
            backend=FakeSapiBackend(RuntimeError("秘密正文"))
        )
        source = CancellationSource()
        with pytest.raises(TtsSynthesisError) as captured:
            await failing.synthesize("秘密正文", source.token)
        assert "秘密正文" not in str(captured.value)
        synthesizer.close()
        failing.close()

    asyncio.run(scenario())


class FakeSynthesizer:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    async def synthesize(self, text, token):
        self.calls.append((text, token))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ScriptedSynthesizer:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls = []

    async def synthesize(self, text, token):
        self.calls.append((text, token))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def test_voice_option_formats_chinese_label():
    voice = TtsVoiceOption("zh-CN-XiaoxiaoNeural", "zh-CN", "Female")

    assert voice.label == "中国大陆 · Xiaoxiao · 女声"


def test_edge_voice_service_lists_chinese_voices_and_plays_preview():
    async def scenario():
        async def load_catalog():
            return [
                {
                    "ShortName": "en-US-JennyNeural",
                    "Locale": "en-US",
                    "Gender": "Female",
                },
                {
                    "ShortName": "zh-TW-YunJheNeural",
                    "Locale": "zh-TW",
                    "Gender": "Male",
                },
                {
                    "ShortName": "zh-CN-XiaoxiaoNeural",
                    "Locale": "zh-CN",
                    "Gender": "Female",
                },
            ]

        synthesized = []
        played = []

        def create_synthesizer(voice_name):
            synthesizer = FakeSynthesizer(
                SynthesizedAudio(b"preview", "audio/mpeg", ".mp3", "edge-tts")
            )
            synthesized.append((voice_name, synthesizer))
            return synthesizer

        class Player:
            async def play(self, audio, token):
                played.append((audio, token))

        service = EdgeTtsVoiceService(
            Player(),
            catalog_loader=load_catalog,
            synthesizer_factory=create_synthesizer,
        )

        voices = await service.list_chinese_voices()
        await service.preview("zh-CN-XiaoxiaoNeural")

        assert [voice.short_name for voice in voices] == [
            "zh-CN-XiaoxiaoNeural",
            "zh-TW-YunJheNeural",
        ]
        assert synthesized[0][0] == "zh-CN-XiaoxiaoNeural"
        assert synthesized[0][1].calls[0][0] == (
            "你好，我是 VoicePet，很高兴认识你"
        )
        assert played[0][0].data == b"preview"

    asyncio.run(scenario())


def test_resilient_tts_retries_transient_network_failure():
    async def scenario():
        delays = []

        async def sleeper(delay):
            delays.append(delay)

        online_audio = SynthesizedAudio(
            b"online",
            "audio/mpeg",
            ".mp3",
            "edge-tts",
        )
        online = ScriptedSynthesizer(
            [TtsNetworkError("private"), online_audio]
        )
        resilient = ResilientSpeechSynthesizer(
            online,
            NetworkResilience(sleeper=sleeper),
        )

        result = await resilient.synthesize(
            "你好",
            CancellationSource().token,
        )

        assert result is online_audio
        assert len(online.calls) == 2
        assert delays == [0.25]

    asyncio.run(scenario())


def test_resilient_tts_does_not_retry_non_network_error():
    async def scenario():
        error = TtsSynthesisError("invalid audio")
        online = ScriptedSynthesizer([error])
        resilient = ResilientSpeechSynthesizer(online)

        with pytest.raises(TtsSynthesisError) as captured:
            await resilient.synthesize("你好", CancellationSource().token)

        assert captured.value is error
        assert len(online.calls) == 1

    asyncio.run(scenario())


def test_resilient_tts_exhaustion_falls_back_to_local_speech():
    async def scenario():
        async def sleeper(delay):
            del delay

        local_audio = SynthesizedAudio(
            b"local",
            "audio/wav",
            ".wav",
            "windows-sapi",
        )
        online = ScriptedSynthesizer(
            [TtsNetworkError("first"), TtsNetworkError("second")]
        )
        local = FakeSynthesizer(local_audio)
        speech = FallbackSpeechSynthesizer(
            ResilientSpeechSynthesizer(
                online,
                NetworkResilience(
                    NetworkResilienceSettings(
                        max_attempts=2,
                        failure_threshold=2,
                    ),
                    sleeper=sleeper,
                ),
            ),
            local,
        )

        result = await speech.synthesize("你好", CancellationSource().token)

        assert result is local_audio
        assert len(online.calls) == 2
        assert len(local.calls) == 1

    asyncio.run(scenario())


def test_open_tts_circuit_uses_local_without_another_online_call():
    async def scenario():
        local_audio = SynthesizedAudio(
            b"local",
            "audio/wav",
            ".wav",
            "windows-sapi",
        )
        online = ScriptedSynthesizer([TtsNetworkError("private")])
        local = FakeSynthesizer(local_audio)
        speech = FallbackSpeechSynthesizer(
            ResilientSpeechSynthesizer(
                online,
                NetworkResilience(
                    NetworkResilienceSettings(
                        max_attempts=1,
                        failure_threshold=1,
                    )
                ),
            ),
            local,
        )
        token = CancellationSource().token

        assert await speech.synthesize("第一次", token) is local_audio
        assert await speech.synthesize("第二次", token) is local_audio
        assert len(online.calls) == 1
        assert len(local.calls) == 2

    asyncio.run(scenario())


def test_fallback_synthesizer_prefers_primary_and_reports_actual_backend():
    async def scenario():
        primary_audio = SynthesizedAudio(
            b"online",
            "audio/mpeg",
            ".mp3",
            "edge-tts",
        )
        primary = FakeSynthesizer(primary_audio)
        fallback = FakeSynthesizer(
            SynthesizedAudio(b"local", "audio/wav", ".wav", "windows-sapi")
        )
        synthesizer = FallbackSpeechSynthesizer(primary, fallback)
        source = CancellationSource()

        result = await synthesizer.synthesize("你好", source.token)

        assert result is primary_audio
        assert synthesizer.last_backend == "edge-tts"
        assert len(primary.calls) == 1
        assert fallback.calls == []

    asyncio.run(scenario())


def test_fallback_synthesizer_uses_local_after_online_error():
    async def scenario():
        primary = FakeSynthesizer(TtsNetworkError("offline"))
        local_audio = SynthesizedAudio(
            b"local",
            "audio/wav",
            ".wav",
            "windows-sapi",
        )
        fallback = FakeSynthesizer(local_audio)
        synthesizer = FallbackSpeechSynthesizer(primary, fallback)
        source = CancellationSource()

        result = await synthesizer.synthesize("你好", source.token)

        assert result is local_audio
        assert synthesizer.last_backend == "windows-sapi"
        assert len(fallback.calls) == 1

    asyncio.run(scenario())


def test_fallback_synthesizer_propagates_local_error_and_never_fallbacks_cancel():
    async def scenario():
        local_error = TtsSynthesisError("local failed")
        synthesizer = FallbackSpeechSynthesizer(
            FakeSynthesizer(TtsNetworkError("offline")),
            FakeSynthesizer(local_error),
        )
        source = CancellationSource()
        with pytest.raises(TtsSynthesisError) as captured:
            await synthesizer.synthesize("你好", source.token)
        assert captured.value is local_error

        cancelled = CancelledError("interrupt")
        primary = FakeSynthesizer(cancelled)
        fallback = FakeSynthesizer(
            SynthesizedAudio(b"local", "audio/wav", ".wav", "windows-sapi")
        )
        synthesizer = FallbackSpeechSynthesizer(primary, fallback)
        with pytest.raises(CancelledError):
            await synthesizer.synthesize("你好", source.token)
        assert fallback.calls == []

    asyncio.run(scenario())


def test_windows_mci_player_uses_one_owned_command_thread(tmp_path, monkeypatch):
    async def scenario():
        command_threads = []
        modes = iter(["playing", "stopped"])

        def runner(command):
            command_threads.append(threading.get_ident())
            if command.startswith("status "):
                return next(modes)
            return ""

        async def reject_shared_thread(*args, **kwargs):
            del args, kwargs
            raise AssertionError("MCI 命令不能使用共享线程池")

        monkeypatch.setattr(asyncio, "to_thread", reject_shared_thread)
        player = WindowsMciAudioPlayer(
            command_runner=runner,
            temp_directory=tmp_path,
            poll_interval=0,
        )
        source = CancellationSource()
        audio = SynthesizedAudio(b"mp3", "audio/mpeg", ".mp3", "edge-tts")

        await player.play(audio, source.token)

        assert len(set(command_threads)) == 1
        assert command_threads[0] != threading.get_ident()
        player.close()

    asyncio.run(scenario())


def test_windows_mci_player_close_is_idempotent_and_rejects_new_playback(
    tmp_path,
):
    player = WindowsMciAudioPlayer(
        command_runner=lambda command: command,
        temp_directory=tmp_path,
    )

    player.close()
    player.close()

    source = CancellationSource()
    audio = SynthesizedAudio(b"mp3", "audio/mpeg", ".mp3", "edge-tts")
    with pytest.raises(TtsConfigurationError, match="已关闭"):
        asyncio.run(player.play(audio, source.token))
    assert list(tmp_path.iterdir()) == []


def test_windows_mci_player_uses_unique_alias_and_cleans_temporary_file(tmp_path):
    async def scenario():
        commands = []
        modes = iter(["playing", "stopped"])

        def runner(command):
            commands.append(command)
            if command.startswith("status "):
                return next(modes)
            return ""

        player = WindowsMciAudioPlayer(
            command_runner=runner,
            temp_directory=tmp_path,
            poll_interval=0,
        )
        source = CancellationSource()
        audio = SynthesizedAudio(b"mp3", "audio/mpeg", ".mp3", "edge-tts")

        await player.play(audio, source.token)

        assert commands[0].startswith('open "')
        assert " alias voicepet_" in commands[0]
        alias = commands[0].rsplit(" ", 1)[1]
        assert commands[1] == f"play {alias}"
        assert commands[2:4] == [
            f"status {alias} mode",
            f"status {alias} mode",
        ]
        assert commands[-1] == f"close {alias}"
        assert list(tmp_path.iterdir()) == []

    asyncio.run(scenario())


def test_windows_mci_player_stops_on_cancellation_and_sanitizes_errors(tmp_path):
    async def cancellation_scenario():
        commands = []
        source = CancellationSource()

        def runner(command):
            commands.append(command)
            if command.startswith("status "):
                source.cancel("interrupt")
                return "playing"
            return ""

        player = WindowsMciAudioPlayer(
            command_runner=runner,
            temp_directory=tmp_path,
            poll_interval=0,
        )
        audio = SynthesizedAudio(b"wav", "audio/wav", ".wav", "windows-sapi")

        with pytest.raises(CancelledError):
            await player.play(audio, source.token)

        alias = commands[0].rsplit(" ", 1)[1]
        assert f"stop {alias}" in commands
        assert commands[-1] == f"close {alias}"
        assert list(tmp_path.iterdir()) == []

    async def error_scenario():
        def runner(command):
            raise RuntimeError(f"秘密路径 {command}")

        player = WindowsMciAudioPlayer(
            command_runner=runner,
            temp_directory=tmp_path,
            poll_interval=0,
        )
        source = CancellationSource()
        audio = SynthesizedAudio(b"wav", "audio/wav", ".wav", "windows-sapi")

        with pytest.raises(TtsPlaybackError) as captured:
            await player.play(audio, source.token)
        assert "秘密路径" not in str(captured.value)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(cancellation_scenario())
    asyncio.run(error_scenario())
