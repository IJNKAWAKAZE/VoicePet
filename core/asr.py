"""本地 faster-whisper 语音转写适配器"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken


@lru_cache(maxsize=1)
def _simplified_converter():
    from opencc import OpenCC

    return OpenCC("t2s")

_SILENCE_HALLUCINATION_PATTERNS = (
    re.compile(r"(?i)^by\s*[\w\u3400-\u9fff·._-]{1,32}[。.!！]?$"),
    re.compile(
        r"(?i)^(?:本|中[英文]?|双语)?字幕\s*"
        r"(?:由|来自|提供|制作|by)\s*[:：]?\s*.*$"
    ),
    re.compile(r"(?i)^(?:subtitles?|captions?)\s+(?:by|provided\s+by).*$"),
    re.compile(r"^.*(?:字幕组|字幕社区|字幕社群)(?:制作|提供).*$"),
    re.compile(
        r"^(?:感谢|谢谢)(?:大家|各位|您|你们)?(?:的)?"
        r"(?:收看|观看|聆听|支持)[。.!！]?$"
    ),
    re.compile(r"(?i)^thank\s+you\s+for\s+watching[.!！。]?$"),
    re.compile(r"^请(?:不吝)?(?:点赞|订阅|转发|打赏|支持).*$"),
    re.compile(r"^[♪♫♬♩…。，、！？!?·\s]+$"),
)


class AsrError(RuntimeError):
    """ASR 子系统可向运行时报告的基础错误"""

    code = "asr.error"
    retryable = False


class AsrConfigurationError(AsrError):
    """ASR 参数、依赖或生命周期配置错误"""

    code = "asr.configuration"


def project_asr_directory(data_root: str | Path, model_name: str) -> Path | None:
    """将下载模型限制在项目目录内，保留显式本地模型路径。"""

    if Path(model_name).expanduser().is_dir():
        return None
    parts = model_name.split("/")
    if len(parts) not in {1, 2} or any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) is None
        for part in parts
    ):
        raise AsrConfigurationError("ASR 模型名称或本地路径无效")
    root = (Path(data_root).expanduser().resolve() / "models" / "asr").resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise AsrConfigurationError("ASR 模型目录超出项目模型目录")
    return target


class AsrModelError(AsrError):
    """ASR 模型无法加载"""

    code = "asr.model"
    retryable = True


class AsrTranscriptionError(AsrError):
    """ASR 原生推理执行失败"""

    code = "asr.transcription"
    retryable = True


class AsrModelState(str, Enum):
    """ASR 模型文件与当前进程的准备状态"""

    MISSING = "missing"
    CACHED = "cached"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class AsrRuntimeStatus:
    """ASR 当前实际后端和降级状态"""

    device: str
    compute_type: str
    degraded: bool
    reason: str | None


class FasterWhisperTranscriptAdapter:
    """串行执行模型加载与本地语音转写"""

    def __init__(
        self,
        model_name: str = "small",
        *,
        language: str = "zh",
        device: str = "cuda",
        cuda_compute_type: str = "float16",
        cpu_compute_type: str = "int8",
        model_downloader: Any | None = None,
        cache_miss_errors: tuple[type[BaseException], ...] | None = None,
        model_directory: str | Path | None = None,
    ) -> None:
        if not model_name.strip():
            raise AsrConfigurationError("ASR 模型名称不能为空")
        if not language.strip():
            raise AsrConfigurationError("ASR 语言不能为空")
        if device not in {"cuda", "cpu"}:
            raise AsrConfigurationError("ASR 设备必须为 cuda 或 cpu")
        try:
            import numpy
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
            from huggingface_hub.errors import LocalEntryNotFoundError
        except (ImportError, ModuleNotFoundError) as error:
            raise AsrConfigurationError(
                "缺少 ASR 依赖，请安装 voicepet[asr]"
            ) from error

        self._numpy = numpy
        self._model_factory = WhisperModel
        self._model_downloader = model_downloader or download_model
        self._cache_miss_errors = cache_miss_errors or (
            LocalEntryNotFoundError,
        )
        self._model_name = model_name
        self._model_directory = (
            None
            if model_directory is None
            else Path(model_directory).expanduser().resolve()
        )
        self._language = language
        self._requested_device = device
        self._cuda_compute_type = cuda_compute_type
        self._cpu_compute_type = cpu_compute_type
        requested_compute = (
            cuda_compute_type if device == "cuda" else cpu_compute_type
        )
        self._status = AsrRuntimeStatus(
            device,
            requested_compute,
            False,
            None,
        )
        self._model: Any | None = None
        self._preload_lock = asyncio.Lock()
        self._download_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="voicepet-asr",
        )
        self._closed = False

    @property
    def status(self) -> AsrRuntimeStatus:
        return self._status

    def model_state(self) -> AsrModelState:
        """不访问网络地检查模型是否已缓存或载入"""

        self._ensure_open()
        if self._model is not None:
            return AsrModelState.READY
        if self._model_directory is not None:
            return (
                AsrModelState.CACHED
                if self._managed_model_complete()
                else AsrModelState.MISSING
            )
        local_directory = self._model_directory
        if local_directory is None:
            local_directory = Path(self._model_name).expanduser()
        if local_directory.is_dir():
            required = ("config.json", "model.bin", "tokenizer.json")
            if not all((local_directory / name).is_file() for name in required):
                raise AsrModelError("本地 ASR 模型文件不完整")
            return AsrModelState.CACHED
        try:
            self._download_from_backend(local_files_only=True)
        except self._cache_miss_errors:
            return AsrModelState.MISSING
        except Exception as error:
            raise AsrModelError("ASR 模型缓存检查失败") from error
        return AsrModelState.CACHED

    async def download_model(self) -> None:
        """在显式授权后下载当前 ASR 模型"""

        self._ensure_open()
        async with self._download_lock:
            if self.model_state() is not AsrModelState.MISSING:
                return
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    self._executor,
                    self._download_model_sync,
                )
            except AsrError:
                raise
            except Exception as error:
                raise AsrModelError("ASR 模型下载失败") from error

    async def preload(self) -> None:
        self._ensure_open()
        if self._model is not None:
            return
        async with self._preload_lock:
            self._ensure_open()
            if self._model is not None:
                return
            loop = asyncio.get_running_loop()
            self._model, self._status = await loop.run_in_executor(
                self._executor,
                self._load_model,
            )

    async def transcribe(
        self,
        audio: bytes,
        token: CancellationToken,
    ) -> str:
        self._ensure_open()
        token.throw_if_cancelled()
        if not audio:
            raise AsrConfigurationError("待转写音频不能为空")
        if len(audio) % 2:
            raise AsrConfigurationError("PCM16 音频字节数必须为偶数")

        await self.preload()
        token.throw_if_cancelled()
        samples = self._numpy.frombuffer(audio, dtype="<i2").astype(
            self._numpy.float32
        )
        samples /= self._numpy.float32(32768.0)
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(
                self._executor,
                self._transcribe_sync,
                samples,
            )
        except AsrError:
            raise
        except Exception as error:
            raise AsrTranscriptionError(f"本地语音转写失败: {error}") from error
        token.throw_if_cancelled()
        return text

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._model = None

    def _load_model(self) -> tuple[Any, AsrRuntimeStatus]:
        if self._requested_device == "cpu":
            try:
                model = self._create_model("cpu", self._cpu_compute_type)
            except Exception as error:
                raise AsrModelError(
                    f"CPU ASR 模型加载失败: {error}"
                ) from error
            return model, AsrRuntimeStatus(
                "cpu",
                self._cpu_compute_type,
                False,
                None,
            )

        try:
            model = self._create_model("cuda", self._cuda_compute_type)
        except Exception as cuda_error:  # noqa: BLE001 模型工厂属于外部边界
            reason = f"{type(cuda_error).__name__}: {cuda_error}"
            try:
                model = self._create_model("cpu", self._cpu_compute_type)
            except Exception as cpu_error:
                raise AsrModelError(
                    f"CUDA 与 CPU ASR 模型均加载失败，CPU 错误: {cpu_error}"
                ) from cpu_error
            return model, AsrRuntimeStatus(
                "cpu",
                self._cpu_compute_type,
                True,
                reason,
            )
        return model, AsrRuntimeStatus(
            "cuda",
            self._cuda_compute_type,
            False,
            None,
        )

    def _create_model(self, device: str, compute_type: str) -> Any:
        if self._model_directory is not None and not self._managed_model_complete():
            raise AsrModelError("本地 ASR 模型文件不完整，请先下载模型")
        return self._model_factory(
            str(self._model_directory or self._model_name),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )

    def _download_model_sync(self) -> None:
        self._download_from_backend(local_files_only=False)
        if self._model_directory is not None and not self._managed_model_complete():
            raise AsrModelError("下载的 ASR 模型文件不完整，请重试")

    def _managed_model_complete(self) -> bool:
        assert self._model_directory is not None
        try:
            return all(
                (self._model_directory / name).is_file()
                and (self._model_directory / name).stat().st_size > 0
                for name in ("config.json", "model.bin", "tokenizer.json")
            )
        except OSError as error:
            raise AsrModelError("本地 ASR 模型目录无法访问") from error

    def _download_from_backend(self, *, local_files_only: bool) -> Any:
        kwargs: dict[str, Any] = {"local_files_only": local_files_only}
        if self._model_directory is not None:
            self._model_directory.mkdir(parents=True, exist_ok=True)
            kwargs["output_dir"] = str(self._model_directory)
        return self._model_downloader(self._model_name, **kwargs)

    def _transcribe_sync(self, samples: Any) -> str:
        assert self._model is not None
        try:
            return self._transcribe_with_model(self._model, samples)
        except AsrError:
            raise
        except Exception as cuda_error:
            if self._status.device != "cuda":
                raise
            reason = f"{type(cuda_error).__name__}: {cuda_error}"

        cpu_model = self._create_model("cpu", self._cpu_compute_type)
        self._model = cpu_model
        self._status = AsrRuntimeStatus(
            "cpu",
            self._cpu_compute_type,
            True,
            reason,
        )
        return self._transcribe_with_model(cpu_model, samples)

    def _transcribe_with_model(self, model: Any, samples: Any) -> str:
        segments, _ = model.transcribe(
            samples,
            language=self._language,
            beam_size=5,
            vad_filter=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            condition_on_previous_text=False,
        )
        parts = [
            segment.text.strip()
            for segment in segments
            if self._segment_is_reliable(segment)
            and segment.text.strip()
            and not self._is_silence_hallucination(segment.text.strip())
        ]
        text = " ".join(parts).strip()
        if text:
            text = _simplified_converter().convert(text)
        if self._is_silence_hallucination(text):
            return ""
        return text

    @staticmethod
    def _is_silence_hallucination(text: str) -> bool:
        if not text:
            return False
        return any(
            pattern.fullmatch(text)
            for pattern in _SILENCE_HALLUCINATION_PATTERNS
        )

    @staticmethod
    def _segment_is_reliable(segment: Any) -> bool:
        """丢弃模型明确判断为静音且置信度很低的片段"""

        no_speech_prob = getattr(segment, "no_speech_prob", None)
        avg_logprob = getattr(segment, "avg_logprob", None)
        if not isinstance(no_speech_prob, (int, float)) or not isinstance(
            avg_logprob,
            (int, float),
        ):
            return True
        return not (no_speech_prob > 0.6 and avg_logprob < -1.0)

    def _ensure_open(self) -> None:
        if self._closed:
            raise AsrConfigurationError("ASR 适配器已关闭")
