import asyncio
import sys
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest

import core.asr as asr_module
from core.asr import (
    AsrConfigurationError,
    AsrModelError,
    AsrRuntimeStatus,
    AsrTranscriptionError,
    FasterWhisperTranscriptAdapter,
)
from core.cancellation import CancellationSource, CancelledError


class FakeSegment:
    def __init__(self, text, *, no_speech_prob=None, avg_logprob=None):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


class CacheMissError(Exception):
    pass


def install_fake_modules(monkeypatch, model_class, downloader=None):
    downloader = downloader or (lambda *args, **kwargs: "cached-model")
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=model_class),
    )
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper.utils",
        SimpleNamespace(download_model=downloader),
    )


def test_adapter_constructs_model_lazily_with_cuda_defaults(monkeypatch):
    created = []

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            created.append((model_name, kwargs))

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()

    assert created == []
    asyncio.run(adapter.preload())

    assert created == [
        (
            "small",
            {
                "device": "cuda",
                "compute_type": "float16",
                "local_files_only": True,
            },
        )
    ]
    assert adapter.status == AsrRuntimeStatus(
        device="cuda",
        compute_type="float16",
        degraded=False,
        reason=None,
    )
    adapter.close()


def test_concurrent_preload_constructs_model_once(monkeypatch):
    created = []

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            created.append((model_name, kwargs))

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter(device="cpu")

    async def scenario():
        await asyncio.gather(adapter.preload(), adapter.preload())

    asyncio.run(scenario())

    assert created == [
        (
            "small",
            {
                "device": "cpu",
                "compute_type": "int8",
                "local_files_only": True,
            },
        )
    ]
    adapter.close()


def test_cuda_failure_falls_back_to_cpu_and_exposes_degradation(monkeypatch):
    created = []

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            created.append((model_name, kwargs))
            if kwargs["device"] == "cuda":
                raise RuntimeError("CUDA driver unavailable")

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()

    asyncio.run(adapter.preload())

    assert [options["device"] for _, options in created] == ["cuda", "cpu"]
    assert adapter.status.device == "cpu"
    assert adapter.status.compute_type == "int8"
    assert adapter.status.degraded is True
    assert "RuntimeError" in adapter.status.reason
    adapter.close()


def test_load_failure_on_cuda_and_cpu_raises_model_error(monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            raise RuntimeError(kwargs["device"])

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()

    with pytest.raises(AsrModelError, match="CPU"):
        asyncio.run(adapter.preload())
    adapter.close()


def test_cpu_load_failure_raises_model_error(monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            raise RuntimeError("local model missing")

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter(device="cpu")

    with pytest.raises(AsrModelError, match="CPU"):
        asyncio.run(adapter.preload())
    adapter.close()


def test_status_is_immutable():
    status = AsrRuntimeStatus("cpu", "int8", True, "fallback")

    with pytest.raises(FrozenInstanceError):
        status.device = "cuda"


def test_model_state_distinguishes_missing_cached_and_ready(monkeypatch):
    cached = False
    calls = []

    def download(model_name, *, local_files_only):
        nonlocal cached
        calls.append((model_name, local_files_only))
        if local_files_only and not cached:
            raise CacheMissError
        cached = True
        return "cached-model"

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

    install_fake_modules(monkeypatch, FakeModel, download)
    adapter = FasterWhisperTranscriptAdapter(
        device="cpu",
        cache_miss_errors=(CacheMissError,),
    )

    assert adapter.model_state() is asr_module.AsrModelState.MISSING
    asyncio.run(adapter.download_model())
    assert adapter.model_state() is asr_module.AsrModelState.CACHED
    asyncio.run(adapter.preload())
    assert adapter.model_state() is asr_module.AsrModelState.READY
    assert ("small", False) in calls
    adapter.close()


def test_local_model_directory_is_cached_without_downloader(
    monkeypatch,
    tmp_path,
):
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (tmp_path / name).write_bytes(b"model")

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

    install_fake_modules(
        monkeypatch,
        FakeModel,
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("本地模型不应调用下载器")
        ),
    )
    adapter = FasterWhisperTranscriptAdapter(str(tmp_path), device="cpu")

    assert adapter.model_state() is asr_module.AsrModelState.CACHED
    adapter.close()


def test_concurrent_downloads_coalesce_after_cache_becomes_available(monkeypatch):
    cached = False
    network_calls = 0

    def download(model_name, *, local_files_only):
        nonlocal cached, network_calls
        if local_files_only and not cached:
            raise CacheMissError
        if not local_files_only:
            network_calls += 1
            cached = True
        return "cached-model"

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

    install_fake_modules(monkeypatch, FakeModel, download)
    adapter = FasterWhisperTranscriptAdapter(
        device="cpu",
        cache_miss_errors=(CacheMissError,),
    )

    async def scenario():
        await asyncio.gather(adapter.download_model(), adapter.download_model())

    asyncio.run(scenario())

    assert network_calls == 1
    adapter.close()


def test_cache_inspection_failure_is_mapped(monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

    install_fake_modules(
        monkeypatch,
        FakeModel,
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cache broken")),
    )
    adapter = FasterWhisperTranscriptAdapter(
        device="cpu",
        cache_miss_errors=(CacheMissError,),
    )

    with pytest.raises(AsrModelError, match="缓存"):
        adapter.model_state()
    adapter.close()


def test_transcribe_normalizes_pcm_and_uses_fixed_options(monkeypatch):
    calls = []

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            calls.append((audio.copy(), kwargs))
            return [FakeSegment(" 你好 "), FakeSegment(" 世界 ")], {"x": 1}

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter(device="cpu")
    pcm = np.array([-32768, 0, 32767], dtype="<i2").tobytes()

    text = asyncio.run(
        adapter.transcribe(pcm, CancellationSource().token)
    )

    audio, options = calls[0]
    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, [-1.0, 0.0, 32767 / 32768])
    assert options == {
        "language": "zh",
        "beam_size": 5,
        "vad_filter": False,
        "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0,
        "condition_on_previous_text": False,
    }
    assert text == "你好 世界"
    adapter.close()


def test_transcribe_discards_low_confidence_silence_and_credit_hallucinations(
    monkeypatch,
):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            return [
                FakeSegment(
                    "by索兰娅",
                    no_speech_prob=0.92,
                    avg_logprob=-1.4,
                ),
                FakeSegment(
                    "字幕by索兰娅",
                    no_speech_prob=0.05,
                    avg_logprob=-0.2,
                ),
                FakeSegment(
                    "谢谢大家观看",
                    no_speech_prob=0.05,
                    avg_logprob=-0.2,
                ),
            ], {}

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter(device="cpu")

    text = asyncio.run(
        adapter.transcribe(b"\x00\x00", CancellationSource().token)
    )

    assert text == ""
    adapter.close()


def test_transcribe_keeps_real_subtitle_request(monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            return [FakeSegment("帮我打开字幕设置")], {}

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter(device="cpu")

    text = asyncio.run(
        adapter.transcribe(b"\x00\x00", CancellationSource().token)
    )

    assert text == "帮我打开字幕设置"
    adapter.close()


@pytest.mark.parametrize("audio", [b"", b"x"])
def test_transcribe_rejects_invalid_pcm(audio, monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            raise AssertionError("invalid audio must not load the model")

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()

    with pytest.raises(AsrConfigurationError):
        asyncio.run(adapter.transcribe(audio, CancellationSource().token))
    adapter.close()


def test_native_transcription_failure_is_mapped(monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            raise OSError("native inference failed")

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter(device="cpu")

    with pytest.raises(AsrTranscriptionError, match="native inference failed"):
        asyncio.run(
            adapter.transcribe(b"\x00\x00", CancellationSource().token)
        )
    adapter.close()


def test_cuda_inference_failure_retries_once_on_cpu(monkeypatch):
    created = []
    calls = []

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            self.device = kwargs["device"]
            created.append(self.device)

        def transcribe(self, audio, **kwargs):
            calls.append((self.device, audio.copy()))
            if self.device == "cuda":
                raise OSError("CUDA runtime unavailable")
            return [FakeSegment(" CPU 转写成功 ")], {}

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()
    pcm = np.array([1, -1], dtype="<i2").tobytes()

    text = asyncio.run(
        adapter.transcribe(pcm, CancellationSource().token)
    )

    assert text == "CPU 转写成功"
    assert created == ["cuda", "cpu"]
    assert [device for device, _ in calls] == ["cuda", "cpu"]
    np.testing.assert_array_equal(calls[0][1], calls[1][1])
    assert adapter.status.device == "cpu"
    assert adapter.status.compute_type == "int8"
    assert adapter.status.degraded is True
    assert "OSError" in adapter.status.reason
    adapter.close()


def test_cuda_and_cpu_inference_failures_are_mapped(monkeypatch):
    created = []

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            self.device = kwargs["device"]
            created.append(self.device)

        def transcribe(self, audio, **kwargs):
            raise OSError(f"{self.device} inference failed")

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()

    with pytest.raises(AsrTranscriptionError, match="cpu inference failed"):
        asyncio.run(
            adapter.transcribe(b"\x00\x00", CancellationSource().token)
        )

    assert created == ["cuda", "cpu"]
    assert adapter.status.device == "cpu"
    assert adapter.status.degraded is True
    adapter.close()


def test_cancelled_token_rejects_before_loading(monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            raise AssertionError("cancelled request must not load the model")

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()
    source = CancellationSource()
    source.cancel("user_interrupt")

    with pytest.raises(CancelledError):
        asyncio.run(adapter.transcribe(b"\x00\x00", source.token))
    adapter.close()


def test_cancellation_after_native_inference_discards_result(monkeypatch):
    started = None
    release = None

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            started.set()
            release.wait()
            return [FakeSegment("stale")], None

    install_fake_modules(monkeypatch, FakeModel)

    async def scenario():
        nonlocal started, release
        import threading

        started = threading.Event()
        release = threading.Event()
        adapter = FasterWhisperTranscriptAdapter(device="cpu")
        source = CancellationSource()
        task = asyncio.create_task(
            adapter.transcribe(b"\x00\x00", source.token)
        )
        await asyncio.to_thread(started.wait)
        source.cancel("user_interrupt")
        release.set()
        with pytest.raises(CancelledError):
            await task
        adapter.close()

    asyncio.run(scenario())


def test_close_is_idempotent_and_rejects_new_requests(monkeypatch):
    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

    install_fake_modules(monkeypatch, FakeModel)
    adapter = FasterWhisperTranscriptAdapter()

    adapter.close()
    adapter.close()

    with pytest.raises(AsrConfigurationError, match="已关闭"):
        asyncio.run(adapter.preload())
    with pytest.raises(AsrConfigurationError, match="已关闭"):
        asyncio.run(
            adapter.transcribe(b"\x00\x00", CancellationSource().token)
        )


def test_missing_dependencies_report_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(AsrConfigurationError, match=r"voicepet\[asr\]"):
        FasterWhisperTranscriptAdapter()


def test_project_model_directory_is_used_for_detection_and_loading(monkeypatch, tmp_path):
    model_directory = tmp_path / "models" / "asr" / "small"
    model_directory.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (model_directory / name).write_bytes(b"model")
    created = []

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            created.append((model_name, kwargs))

    install_fake_modules(
        monkeypatch,
        FakeModel,
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("缓存目录中的模型不应调用下载器")
        ),
    )
    adapter = FasterWhisperTranscriptAdapter(
        model_directory.name,
        model_directory=model_directory,
        device="cpu",
    )

    assert adapter.model_state() is asr_module.AsrModelState.CACHED
    asyncio.run(adapter.preload())
    assert created[0][0] == str(model_directory)
    adapter.close()


def test_project_model_download_writes_to_model_directory_without_deleting_shared_cache(
    monkeypatch, tmp_path
):
    model_directory = tmp_path / "models" / "asr" / "small"
    legacy_cache = tmp_path / "huggingface" / "models--Systran--faster-whisper-small"
    legacy_cache.mkdir(parents=True)
    (legacy_cache / "old.bin").write_bytes(b"old")
    monkeypatch.setattr(
        "huggingface_hub.constants.HF_HUB_CACHE", str(legacy_cache.parent)
    )
    calls = []

    def download(model_name, *, output_dir, local_files_only):
        calls.append((model_name, output_dir, local_files_only))
        if local_files_only:
            raise CacheMissError
        model_directory.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (model_directory / name).write_bytes(b"model")
        return str(model_directory)

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            pass

    install_fake_modules(monkeypatch, FakeModel, download)
    adapter = FasterWhisperTranscriptAdapter(
        "small",
        model_directory=model_directory,
        cache_miss_errors=(CacheMissError,),
        device="cpu",
    )

    asyncio.run(adapter.download_model())

    assert calls == [("small", str(model_directory), False)]
    assert (legacy_cache / "old.bin").read_bytes() == b"old"
    assert adapter.model_state() is asr_module.AsrModelState.CACHED
    adapter.close()


def test_managed_partial_model_can_resume_download(monkeypatch, tmp_path):
    directory = tmp_path / "small"
    directory.mkdir()
    (directory / "config.json").write_bytes(b"config")

    def download(model_name, *, output_dir, local_files_only):
        assert not local_files_only
        assert output_dir == str(directory)
        for name in ("model.bin", "tokenizer.json"):
            (directory / name).write_bytes(b"model")

    install_fake_modules(monkeypatch, object, download)
    adapter = FasterWhisperTranscriptAdapter(model_directory=directory)
    try:
        assert adapter.model_state() is asr_module.AsrModelState.MISSING
        asyncio.run(adapter.download_model())
        assert adapter.model_state() is asr_module.AsrModelState.CACHED
    finally:
        adapter.close()


def test_managed_state_checks_do_not_create_directories_or_use_global_cache(
    monkeypatch, tmp_path
):
    directory = tmp_path / "small"

    def unexpected(*args, **kwargs):
        raise AssertionError("must not use the backend")

    install_fake_modules(monkeypatch, unexpected, unexpected)
    adapter = FasterWhisperTranscriptAdapter(model_directory=directory, device="cpu")
    try:
        assert adapter.model_state() is asr_module.AsrModelState.MISSING
        assert adapter.model_state() is asr_module.AsrModelState.MISSING
        assert not directory.exists()
        with pytest.raises(AsrModelError):
            asyncio.run(adapter.preload())
    finally:
        adapter.close()


def test_managed_download_requires_complete_nonempty_files(monkeypatch, tmp_path):
    directory = tmp_path / "small"
    directory.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (directory / name).touch()
    install_fake_modules(monkeypatch, object)
    adapter = FasterWhisperTranscriptAdapter(model_directory=directory)
    try:
        with pytest.raises(AsrModelError, match="不完整"):
            asyncio.run(adapter.download_model())
        assert adapter.model_state() is asr_module.AsrModelState.MISSING
    finally:
        adapter.close()


@pytest.mark.parametrize("name", ["../outside", "org/../../outside", "C:/outside"])
def test_project_directory_rejects_model_names_escaping_root(tmp_path, name):
    with pytest.raises(AsrConfigurationError):
        asr_module.project_asr_directory(tmp_path, name)


def test_project_directory_preserves_explicit_local_model(tmp_path):
    directory = tmp_path / "local-model"
    directory.mkdir()
    assert asr_module.project_asr_directory(tmp_path, str(directory)) is None


def test_project_directory_supports_hub_repository_names(tmp_path):
    assert asr_module.project_asr_directory(tmp_path, "Systran/faster-whisper-small") == (
        tmp_path / "models" / "asr" / "Systran" / "faster-whisper-small"
    )
