"""在线与本地语音合成及播放边界"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .cancellation import CancellationToken, CancelledError
from .network_resilience import NetworkCircuitOpenError, NetworkResilience


class TtsError(RuntimeError):
    """TTS 子系统可向运行时报告的基础错误"""

    code = "tts.error"
    retryable = False


class TtsConfigurationError(TtsError):
    """TTS 参数、依赖或系统能力配置错误"""

    code = "tts.configuration"


class TtsNetworkError(TtsError):
    """可重试的在线语音服务错误"""

    code = "tts.network"
    retryable = True


class TtsSynthesisError(TtsError):
    """语音合成未产生有效音频"""

    code = "tts.synthesis"


class TtsPlaybackError(TtsError):
    """系统播放器无法播放合成音频"""

    code = "tts.playback"


@dataclass(frozen=True, slots=True)
class SynthesizedAudio:
    """合成器返回的不可变音频数据"""

    data: bytes
    media_type: str
    file_suffix: str
    backend: str

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise TtsConfigurationError("合成音频不能为空")
        if not self.media_type.strip():
            raise TtsConfigurationError("音频媒体类型不能为空")
        if not self.file_suffix.startswith("."):
            raise TtsConfigurationError("音频文件后缀必须以点开头")
        if not self.backend.strip():
            raise TtsConfigurationError("音频后端名称不能为空")


class SpeechSynthesizer(Protocol):
    """异步语音合成器边界"""

    async def synthesize(
        self,
        text: str,
        token: CancellationToken,
    ) -> SynthesizedAudio: ...


class AudioPlayer(Protocol):
    """异步音频播放器边界"""

    async def play(
        self,
        audio: SynthesizedAudio,
        token: CancellationToken,
    ) -> None: ...


class EdgeTtsSynthesizer:
    """使用 Edge 在线服务生成 MP3 音频"""

    def __init__(
        self,
        *,
        communicate_factory: Any | None = None,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
    ) -> None:
        if not all(value.strip() for value in (voice, rate, volume, pitch)):
            raise TtsConfigurationError("Edge TTS 声音参数不能为空")
        if communicate_factory is None:
            try:
                from edge_tts import Communicate
            except (ImportError, ModuleNotFoundError) as error:
                raise TtsConfigurationError(
                    "缺少 edge-tts 依赖，请安装 voicepet[tts]"
                ) from error
            communicate_factory = Communicate
        self._communicate_factory = communicate_factory
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._pitch = pitch

    async def synthesize(
        self,
        text: str,
        token: CancellationToken,
    ) -> SynthesizedAudio:
        normalized_text = text.strip()
        if not normalized_text:
            raise TtsConfigurationError("语音合成文本不能为空")
        token.throw_if_cancelled()
        stream = None
        try:
            communicator = self._communicate_factory(
                normalized_text,
                voice=self._voice,
                rate=self._rate,
                volume=self._volume,
                pitch=self._pitch,
            )
            stream = communicator.stream()
            chunks: list[bytes] = []
            async for chunk in stream:
                token.throw_if_cancelled()
                if not isinstance(chunk, Mapping) or chunk.get("type") != "audio":
                    continue
                data = chunk.get("data")
                if not isinstance(data, bytes):
                    raise TtsSynthesisError("Edge TTS 返回了无效音频块")
                chunks.append(data)
                token.throw_if_cancelled()
            if not chunks:
                raise TtsSynthesisError("Edge TTS 未返回音频")
            return SynthesizedAudio(
                b"".join(chunks),
                "audio/mpeg",
                ".mp3",
                "edge-tts",
            )
        except (TtsError, CancelledError, asyncio.CancelledError):
            raise
        except Exception as error:
            raise self._map_error(error) from error
        finally:
            if stream is not None:
                close = getattr(stream, "aclose", None)
                if close is None:
                    close = getattr(stream, "close", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result

    @staticmethod
    def _map_error(error: Exception) -> TtsError:
        error_type = type(error).__name__
        name = error_type.lower()
        status = getattr(error, "status", None)
        if status is None:
            status = getattr(error, "status_code", None)
        status_detail = f" HTTP {status}" if isinstance(status, int) else ""
        if (
            status in {408, 429}
            or isinstance(status, int) and status >= 500
            or any(
                term in name
                for term in (
                    "connection",
                    "timeout",
                    "websocket",
                    "server",
                )
            )
        ):
            return TtsNetworkError(
                f"Edge TTS 网络请求失败: {error_type}{status_detail}"
            )
        return TtsSynthesisError(
            f"Edge TTS 合成失败: {error_type}{status_detail}"
        )


class WindowsSapiBackend:
    """在线程内部驱动 Windows SAPI 并生成 WAV"""

    def synthesize(
        self,
        text: str,
        voice_name: str | None,
        token: CancellationToken,
    ) -> bytes:
        try:
            import pythoncom
            import win32com.client
        except (ImportError, ModuleNotFoundError) as error:
            raise TtsConfigurationError(
                "缺少 pywin32 依赖，请安装 voicepet[tts]"
            ) from error

        descriptor, path = tempfile.mkstemp(
            prefix="voicepet-sapi-",
            suffix=".wav",
        )
        os.close(descriptor)
        os.remove(path)
        voice = None
        stream = None
        pythoncom.CoInitialize()
        try:
            token.throw_if_cancelled()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            if voice_name is not None:
                self._select_voice(voice, voice_name)
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(path, 3, False)
            voice.AudioOutputStream = stream
            voice.Speak(text, 1)
            while not voice.WaitUntilDone(50):
                token.throw_if_cancelled()
            token.throw_if_cancelled()
            stream.Close()
            stream = None
            with open(path, "rb") as audio_file:
                return audio_file.read()
        except (TtsError, CancelledError):
            raise
        except Exception as error:
            error_type = type(error).__name__
            raise TtsSynthesisError(
                f"Windows SAPI 合成失败: {error_type}"
            ) from error
        finally:
            if voice is not None and token.is_cancelled:
                try:
                    voice.Speak("", 3)
                except Exception as cleanup_error:  # noqa: BLE001 清理阶段不能覆盖原始错误
                    _ = cleanup_error
            if stream is not None:
                try:
                    stream.Close()
                except Exception as cleanup_error:  # noqa: BLE001 清理阶段不能覆盖原始错误
                    _ = cleanup_error
            pythoncom.CoUninitialize()
            if os.path.exists(path):
                os.remove(path)

    @staticmethod
    def _select_voice(voice: Any, voice_name: str) -> None:
        voices = voice.GetVoices()
        normalized_name = voice_name.casefold()
        for index in range(voices.Count):
            candidate = voices.Item(index)
            if normalized_name in candidate.GetDescription().casefold():
                voice.Voice = candidate
                return
        raise TtsConfigurationError(
            f"找不到 Windows SAPI 声音: {voice_name}"
        )


class WindowsSapiSynthesizer:
    """在专用单线程中串行执行 Windows SAPI 合成"""

    def __init__(
        self,
        *,
        backend: Any | None = None,
        voice_name: str | None = "Chinese (Simplified)",
    ) -> None:
        if voice_name is not None and not voice_name.strip():
            raise TtsConfigurationError("Windows SAPI 声音名称不能为空")
        self._backend = backend or WindowsSapiBackend()
        self._voice_name = voice_name
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="voicepet-sapi",
        )
        self._closed = False

    async def synthesize(
        self,
        text: str,
        token: CancellationToken,
    ) -> SynthesizedAudio:
        self._ensure_open()
        normalized_text = text.strip()
        if not normalized_text:
            raise TtsConfigurationError("语音合成文本不能为空")
        token.throw_if_cancelled()
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                self._executor,
                self._backend.synthesize,
                normalized_text,
                self._voice_name,
                token,
            )
        except (TtsError, CancelledError, asyncio.CancelledError):
            raise
        except Exception as error:
            raise TtsSynthesisError(
                f"Windows SAPI 合成失败: {type(error).__name__}"
            ) from error
        token.throw_if_cancelled()
        if not isinstance(data, bytes) or not data:
            raise TtsSynthesisError("Windows SAPI 未返回音频")
        return SynthesizedAudio(
            data,
            "audio/wav",
            ".wav",
            "windows-sapi",
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _ensure_open(self) -> None:
        if self._closed:
            raise TtsConfigurationError("Windows SAPI 合成器已关闭")


class ResilientSpeechSynthesizer:
    """为在线语音合成增加网络退避与熔断"""

    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        resilience: NetworkResilience | None = None,
    ) -> None:
        self._synthesizer = synthesizer
        self._resilience = resilience or NetworkResilience()

    async def synthesize(
        self,
        text: str,
        token: CancellationToken,
    ) -> SynthesizedAudio:
        """完整音频产生前按策略重试网络错误"""

        try:
            return await self._resilience.execute(
                lambda: self._synthesizer.synthesize(text, token),
                token,
                retryable=self._is_retryable,
            )
        except NetworkCircuitOpenError:
            raise TtsNetworkError("在线语音服务暂时熔断") from None

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        return isinstance(error, TtsNetworkError)


class FallbackSpeechSynthesizer:
    """在线合成失败时自动改用本地合成器"""

    def __init__(
        self,
        primary: SpeechSynthesizer,
        fallback: SpeechSynthesizer,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._last_backend: str | None = None

    @property
    def last_backend(self) -> str | None:
        return self._last_backend

    async def synthesize(
        self,
        text: str,
        token: CancellationToken,
    ) -> SynthesizedAudio:
        token.throw_if_cancelled()
        try:
            audio = await self._primary.synthesize(text, token)
        except TtsError:
            token.throw_if_cancelled()
            audio = await self._fallback.synthesize(text, token)
        token.throw_if_cancelled()
        self._last_backend = audio.backend
        return audio


class WindowsMciAudioPlayer:
    """使用 Windows MCI 播放临时 MP3 或 WAV 文件"""

    def __init__(
        self,
        *,
        command_runner: Any | None = None,
        temp_directory: str | Path | None = None,
        poll_interval: float = 0.05,
        playback_timeout: float = 300.0,
    ) -> None:
        if poll_interval < 0:
            raise TtsConfigurationError("播放器轮询间隔不能小于零")
        if playback_timeout <= 0:
            raise TtsConfigurationError("播放器超时必须大于零")
        self._command_runner = command_runner or self._run_mci_command
        self._temp_directory = temp_directory
        self._poll_interval = poll_interval
        self._playback_timeout = playback_timeout
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="voicepet-mci",
        )
        self._closed = False

    async def play(
        self,
        audio: SynthesizedAudio,
        token: CancellationToken,
    ) -> None:
        if self._closed:
            raise TtsConfigurationError("Windows MCI 播放器已关闭")
        token.throw_if_cancelled()
        alias = f"voicepet_{uuid4().hex}"
        path = self._write_temporary_audio(audio)
        opened = False
        try:
            await self._command(f'open "{path}" alias {alias}')
            opened = True
            token.throw_if_cancelled()
            await self._command(f"play {alias}")
            deadline = asyncio.get_running_loop().time() + self._playback_timeout
            while True:
                token.throw_if_cancelled()
                mode = (await self._command(f"status {alias} mode")).casefold()
                if mode in {"stopped", "not ready"}:
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise TtsPlaybackError("Windows MCI 播放超时")
                await asyncio.sleep(self._poll_interval)
        except (CancelledError, asyncio.CancelledError):
            if opened:
                await self._ignore_command_error(f"stop {alias}")
            raise
        except TtsError:
            raise
        except Exception as error:
            raise TtsPlaybackError(
                f"Windows MCI 播放失败: {type(error).__name__}"
            ) from error
        finally:
            if opened:
                await self._ignore_command_error(f"close {alias}")
            if os.path.exists(path):
                os.remove(path)

    def close(self) -> None:
        """幂等关闭播放器的专用命令线程"""

        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _write_temporary_audio(self, audio: SynthesizedAudio) -> str:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="voicepet-playback-",
            suffix=audio.file_suffix,
            dir=self._temp_directory,
            delete=False,
        ) as audio_file:
            audio_file.write(audio.data)
            return audio_file.name

    async def _command(self, command: str) -> str:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._executor,
            self._command_runner,
            command,
        )
        return "" if result is None else str(result).strip()

    async def _ignore_command_error(self, command: str) -> None:
        try:
            await self._command(command)
        except Exception as cleanup_error:  # noqa: BLE001 清理阶段不能覆盖原始错误
            _ = cleanup_error

    @staticmethod
    def _run_mci_command(command: str) -> str:
        if os.name != "nt":
            raise TtsConfigurationError("Windows MCI 只能在 Windows 上使用")
        import ctypes

        output = ctypes.create_unicode_buffer(256)
        result = ctypes.windll.winmm.mciSendStringW(
            command,
            output,
            len(output),
            0,
        )
        if result != 0:
            raise TtsPlaybackError(f"Windows MCI 命令失败: {result}")
        return output.value


def markdown_to_speech_text(text: str) -> str:
    """把 Markdown 回复转换为适合语音合成的纯文本"""

    if not text.strip():
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(
        r"(?m)^[ \t]{0,3}(?:`{3,}|~{3,})[^\n]*$",
        "",
        normalized,
    )
    normalized = re.sub(
        r"!?\[([^\]]*)\]\((?:\\.|[^)])*\)",
        r"\1",
        normalized,
    )
    normalized = re.sub(
        r"(?i)<(?:https?://|mailto:)[^>]+>",
        "",
        normalized,
    )
    block_prefix = re.compile(
        r"(?m)^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]?|[-+*][ \t]+|\d+[.)][ \t]+)"
    )
    while block_prefix.search(normalized):
        normalized = block_prefix.sub("", normalized)
    normalized = re.sub(
        r"(?im)^[ \t]*\[(?: |x)\][ \t]+",
        "",
        normalized,
    )
    normalized = re.sub(
        r"(?m)^[ \t]*(?:[-*_][ \t]*){3,}$",
        "",
        normalized,
    )
    normalized = re.sub(
        r"(?m)^[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*$",
        "",
        normalized,
    )
    normalized = re.sub(r"(`+)(.*?)\1", r"\2", normalized, flags=re.DOTALL)
    for pattern in (
        r"(\*\*|__)(?=\S)(.+?\S)\1",
        r"(~~)(?=\S)(.+?\S)\1",
        r"(?<!\w)([*_])(?=\S)(.+?\S)\1(?!\w)",
    ):
        normalized = re.sub(pattern, r"\2", normalized)
    normalized = re.sub(
        r"(?i)\b(?:https?://|www\.)\S+",
        "",
        normalized,
    )
    normalized = re.sub(
        r"\\([\\`*{}\[\]()#+\-.!_>~|])",
        r"\1",
        normalized,
    )
    normalized = re.sub(r"[*~`]+", "", normalized)
    normalized = re.sub(r"(?<!\w)_+|_+(?!\w)", "", normalized)
    normalized = normalized.replace("|", " ")
    lines = [line.strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def split_speech_text(
    text: str,
    *,
    max_chars: int = 1500,
) -> tuple[str, ...]:
    """只在语音文本过长时按自然边界分段"""

    if max_chars <= 0:
        raise TtsConfigurationError("语音分段上限必须大于零")
    remainder = text.strip()
    if not remainder:
        return ()
    chunks: list[str] = []
    while len(remainder) > max_chars:
        window = remainder[:max_chars]
        search_start = max(1, int(max_chars * 0.6))
        boundary = window.rfind("\n\n", search_start)
        if boundary < 0:
            punctuation = max(
                window.rfind(character, search_start)
                for character in "。！？!?"
            )
            if punctuation >= 0:
                boundary = punctuation + 1
        if boundary < 0:
            boundary = max(
                window.rfind(" ", search_start),
                window.rfind("\n", search_start),
            )
        if boundary <= 0:
            boundary = max_chars
        chunk = remainder[:boundary].strip()
        remainder = remainder[boundary:].strip()
        if chunk:
            chunks.append(chunk)
    if remainder:
        chunks.append(remainder)
    return tuple(chunks)


class SentenceChunker:
    """把流式文本整理为适合语音合成的完整句子"""

    _TERMINATORS = frozenset("。！？.!?")

    def __init__(self, *, max_chars: int = 200) -> None:
        if max_chars <= 0:
            raise TtsConfigurationError("语音分块上限必须大于零")
        self._max_chars = max_chars
        self._buffer = ""

    def push(self, text: str) -> tuple[str, ...]:
        if not text:
            return ()
        self._buffer += text
        return self._drain_complete()

    def finish(self) -> tuple[str, ...]:
        chunks = list(self._drain_complete())
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder:
            chunks.append(remainder)
        return tuple(chunks)

    def _drain_complete(self) -> tuple[str, ...]:
        chunks: list[str] = []
        while self._buffer:
            boundary = self._sentence_boundary()
            if boundary is not None and boundary <= self._max_chars:
                self._append_prefix(chunks, boundary)
                continue
            if len(self._buffer) > self._max_chars:
                self._append_prefix(chunks, self._limit_boundary())
                continue
            break
        return tuple(chunks)

    def _sentence_boundary(self) -> int | None:
        for index, character in enumerate(self._buffer):
            if character in self._TERMINATORS:
                return index + 1
        return None

    def _limit_boundary(self) -> int:
        candidate = self._buffer[: self._max_chars + 1]
        whitespace = max(candidate.rfind(" "), candidate.rfind("\n"))
        if whitespace > 0:
            return whitespace
        return self._max_chars

    def _append_prefix(self, chunks: list[str], boundary: int) -> None:
        chunk = self._buffer[:boundary].strip()
        self._buffer = self._buffer[boundary:].lstrip()
        if chunk:
            chunks.append(chunk)
