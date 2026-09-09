"""在线与本地语音合成及播放边界"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .cancellation import CancellationSource, CancellationToken, CancelledError
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


@dataclass(frozen=True, slots=True)
class TtsVoiceOption:
    """设置页展示的在线中文声音"""

    short_name: str
    locale: str
    gender: str

    def __post_init__(self) -> None:
        if not self.short_name.strip() or not self.locale.strip():
            raise TtsConfigurationError("语音目录条目不能为空")

    @property
    def label(self) -> str:
        regions = {
            "zh-CN": "中国大陆",
            "zh-HK": "中国香港",
            "zh-TW": "中国台湾",
            "zh-CN-liaoning": "辽宁",
            "zh-CN-shaanxi": "陕西",
        }
        region = regions.get(self.locale, self.locale)
        voice_name = self.short_name
        prefix = f"{self.locale}-"
        voice_name = voice_name.removeprefix(prefix).removesuffix("Neural")
        gender = {"Female": "女声", "Male": "男声"}.get(
            self.gender,
            self.gender or "未知",
        )
        return f"{region} · {voice_name} · {gender}"


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


VoiceCatalogLoader = Callable[
    [],
    Awaitable[Sequence[Mapping[str, object]]],
]
VoiceSynthesizerFactory = Callable[[str], SpeechSynthesizer]


class EdgeTtsVoiceService:
    """动态获取中文声音并提供设置页试听"""

    def __init__(
        self,
        audio_player: AudioPlayer,
        *,
        catalog_loader: VoiceCatalogLoader | None = None,
        synthesizer_factory: VoiceSynthesizerFactory | None = None,
    ) -> None:
        if catalog_loader is None:
            try:
                from edge_tts import list_voices
            except (ImportError, ModuleNotFoundError) as error:
                raise TtsConfigurationError(
                    "缺少 edge-tts 依赖，请安装 voicepet[tts]"
                ) from error
            catalog_loader = list_voices
        self._audio_player = audio_player
        self._catalog_loader = catalog_loader
        self._synthesizer_factory = synthesizer_factory or (
            lambda voice: EdgeTtsSynthesizer(voice=voice)
        )
        self._cache: tuple[TtsVoiceOption, ...] | None = None
        self._preview_lock = asyncio.Lock()

    async def list_chinese_voices(self) -> tuple[TtsVoiceOption, ...]:
        if self._cache is not None:
            return self._cache
        try:
            entries = await self._catalog_loader()
        except TtsError:
            raise
        except Exception as error:
            raise TtsNetworkError(
                f"中文声音目录获取失败: {type(error).__name__}"
            ) from error
        voices: dict[str, TtsVoiceOption] = {}
        for entry in entries:
            short_name = entry.get("ShortName")
            locale = entry.get("Locale")
            gender = entry.get("Gender", "")
            if (
                not isinstance(short_name, str)
                or not isinstance(locale, str)
                or not locale.casefold().startswith("zh-")
            ):
                continue
            voices[short_name] = TtsVoiceOption(
                short_name,
                locale,
                gender if isinstance(gender, str) else "",
            )
        if not voices:
            raise TtsSynthesisError("在线服务未返回中文声音")
        self._cache = tuple(
            sorted(
                voices.values(),
                key=lambda voice: (voice.locale, voice.short_name),
            )
        )
        return self._cache

    async def preview(self, voice_name: str) -> None:
        normalized = voice_name.strip()
        if not normalized or not normalized.casefold().startswith("zh-"):
            raise TtsConfigurationError("只能试听中文声音")
        async with self._preview_lock:
            source = CancellationSource()
            synthesizer = self._synthesizer_factory(normalized)
            audio = await synthesizer.synthesize(
                "你好，我是 VoicePet，很高兴认识你",
                source.token,
            )
            await self._audio_player.play(audio, source.token)


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
        self._muted = False
        self._playback_generation = 0
        self._active_aliases: set[str] = set()
        self._command_lock = asyncio.Lock()
        self._mute_lock = asyncio.Lock()

    async def set_muted(self, muted: bool) -> None:
        """临时静音并等待当前音频停止，恢复时不重播旧音频"""

        async with self._mute_lock:
            if self._closed:
                raise TtsConfigurationError("Windows MCI 播放器已关闭")
            previous_muted = self._muted
            self._muted = muted
            if not muted:
                return
            self._playback_generation += 1
            try:
                await self._wait_for_completion(
                    asyncio.create_task(self._stop_active_audio())
                )
            except (Exception, asyncio.CancelledError):
                # 失败时回滚开关，保留代际变化以阻止旧音频重新开始
                self._muted = previous_muted
                raise

    async def _stop_active_audio(self) -> None:
        errors: list[Exception] = []
        async with self._command_lock:
            for alias in tuple(self._active_aliases):
                try:
                    await self._command(f"stop {alias}")
                except Exception as error:  # noqa: BLE001 仍需尝试停止其他音频
                    errors.append(error)
        if errors:
            raise TtsPlaybackError("Windows MCI 静音失败") from errors[0]

    async def play(
        self,
        audio: SynthesizedAudio,
        token: CancellationToken,
    ) -> None:
        if self._closed:
            raise TtsConfigurationError("Windows MCI 播放器已关闭")
        token.throw_if_cancelled()
        if self._muted:
            return
        generation = self._playback_generation
        alias = f"voicepet_{uuid4().hex}"
        path = self._write_temporary_audio(audio)
        cancelled = False
        try:
            async with self._command_lock:
                if self._muted or generation != self._playback_generation:
                    return
                # 提前登记以覆盖 open 执行中取消或静音的清理
                self._active_aliases.add(alias)
                await self._command(f'open "{path}" alias {alias}')
            async with self._command_lock:
                token.throw_if_cancelled()
                if self._muted or generation != self._playback_generation:
                    return
                await self._command(f"play {alias}")
            deadline = asyncio.get_running_loop().time() + self._playback_timeout
            while True:
                token.throw_if_cancelled()
                async with self._command_lock:
                    if self._muted or generation != self._playback_generation:
                        return
                    mode = (await self._command(f"status {alias} mode")).casefold()
                if mode in {"stopped", "not ready"}:
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise TtsPlaybackError("Windows MCI 播放超时")
                await asyncio.sleep(self._poll_interval)
        except (CancelledError, asyncio.CancelledError):
            cancelled = True
            raise
        except TtsError:
            raise
        except Exception as error:
            raise TtsPlaybackError(
                f"Windows MCI 播放失败: {type(error).__name__}"
            ) from error
        finally:
            await self._wait_for_completion(
                asyncio.create_task(self._cleanup_playback(alias, path, cancelled))
            )

    async def _cleanup_playback(self, alias: str, path: str, cancelled: bool) -> None:
        try:
            async with self._command_lock:
                if alias in self._active_aliases:
                    if cancelled:
                        await self._ignore_command_error(f"stop {alias}")
                    await self._ignore_command_error(f"close {alias}")
                    self._active_aliases.discard(alias)
        finally:
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
        result = await self._wait_for_completion(
            loop.run_in_executor(self._executor, self._command_runner, command)
        )
        return "" if result is None else str(result).strip()

    @staticmethod
    async def _wait_for_completion(operation: asyncio.Future[Any]) -> Any:
        """取消不能让线程中的命令逃离锁或打断资源清理"""

        cancellation: asyncio.CancelledError | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as error:
                cancellation = error
        result = operation.result()
        if cancellation is not None:
            raise cancellation
        return result

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
    normalized = remove_non_speech_symbols(normalized)
    lines = [line.strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line).strip()


_EMOJI_RE = re.compile(
    "[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF\\uFE0F\\u200D]"
)
_KAOMOJI_RE = re.compile(r"(?<![A-Za-z0-9_])[\\(（][^\\(（）\\)）\\n]{1,32}[\\)）]")
_KAOMOJI_ALLOWED_RE = re.compile(r"^[^A-Za-z0-9_]+$")
_KAOMOJI_FACE_RE = re.compile(r"[Tツ；;╥QД°^＾][\\s._*^＾°:；;ω▽▼□口皿益Д╥ノへつ\\-]*[Tツ；;╥QД°^＾]")


def remove_non_speech_symbols(text: str) -> str:
    """移除不会产生有效读音的 emoji 与常见颜文字"""

    if not text:
        return ""
    text = text.replace("(๑•̀ㅂ•́)و✧", " ")
    text = _EMOJI_RE.sub("", text)

    def replace_kaomoji(match: re.Match[str]) -> str:
        value = match.group(0)
        inner = value[1:-1]
        if _KAOMOJI_ALLOWED_RE.fullmatch(inner) and _KAOMOJI_FACE_RE.search(inner):
            return " "
        return value

    text = re.sub(r"\\([^()\\n]*[๑][^()\\n]*\\)[^A-Za-z0-9\\s]*", " ", text)
    text = _KAOMOJI_RE.sub(replace_kaomoji, text)
    text = text.replace("(๑•̀ㅂ•́)و✧", " ")
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"(?<![A-Za-z0-9/:])(?::[-^']?[)D(]|[)D])", "", text)
    return re.sub(r"[ \\t]{2,}", " ", text)


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
