import asyncio
import threading
from types import SimpleNamespace

from core.asr import AsrRuntimeStatus
from core.cancellation import CancellationToken
from core.diagnostic_probes import (
    probe_asr,
    probe_desktop,
    probe_llm,
    probe_pet,
    probe_tts,
)
from core.diagnostics import DiagnosticStatus
from core.tts import SynthesizedAudio


def test_asr_probe_preloads_before_reporting_final_backend():
    class Asr:
        def __init__(self):
            self.status = AsrRuntimeStatus("cuda", "float16", False, None)
            self.preloaded = False

        async def preload(self):
            self.preloaded = True
            self.status = AsrRuntimeStatus(
                "cpu",
                "int8",
                True,
                "cuda unavailable",
            )

    asr = Asr()

    status, message, context = asyncio.run(probe_asr(asr))

    assert asr.preloaded is True
    assert status is DiagnosticStatus.DEGRADED
    assert "降级" in message
    assert context == {"device": "cpu", "compute_type": "int8"}


def test_llm_probe_checks_configured_model_without_generation():
    class Llm:
        def __init__(self):
            self.calls = 0

        async def probe(self):
            self.calls += 1
            return {"model": "gpt-test"}

    llm = Llm()

    status, message, context = asyncio.run(probe_llm(llm))

    assert llm.calls == 1
    assert status is DiagnosticStatus.HEALTHY
    assert "可用" in message
    assert context == {"model": "gpt-test"}


def test_llm_probe_reports_missing_configuration_as_degraded():
    status, message, context = asyncio.run(probe_llm(None))

    assert status is DiagnosticStatus.DEGRADED
    assert "凭据" in message
    assert context == {}


def test_tts_probe_synthesizes_fixed_phrase_without_playback():
    class Tts:
        def __init__(self):
            self.calls = []

        async def synthesize(self, text: str, token: CancellationToken):
            self.calls.append((text, token))
            return SynthesizedAudio(b"wav", "audio/wav", ".wav", "local")

    tts = Tts()

    status, message, context = asyncio.run(probe_tts(tts))

    assert len(tts.calls) == 1
    assert tts.calls[0][0] == "语音诊断"
    assert not tts.calls[0][1].is_cancelled
    assert status is DiagnosticStatus.HEALTHY
    assert "可用" in message
    assert context == {"backend": "local", "audio_bytes": 3}


def test_pet_probe_validates_directory_outside_event_loop(tmp_path):
    class Installer:
        def __init__(self):
            self.calls = []

        def validate_directory(self, directory):
            self.calls.append((directory, threading.get_ident()))
            return "pet-id"

    async def scenario():
        installer = Installer()
        loop_thread = threading.get_ident()
        result = await probe_pet(installer, tmp_path / "pet")
        return installer, loop_thread, result

    installer, loop_thread, result = asyncio.run(scenario())
    status, message, context = result

    assert installer.calls[0][0] == tmp_path / "pet"
    assert installer.calls[0][1] != loop_thread
    assert status is DiagnosticStatus.HEALTHY
    assert "有效" in message
    assert context == {"pet_id": "pet-id"}


def test_desktop_probe_reports_capture_without_injecting_input():
    class Desktop:
        def __init__(self):
            self.calls = []

        def list_windows(self, **kwargs):
            self.calls.append(("list_windows", kwargs))
            return [object(), object()]

        def capture_window(self, handle, **kwargs):
            self.calls.append(("capture_window", handle, kwargs))
            return SimpleNamespace(width=640, height=360, png=b"\x89PNG\r\n\x1a\n")

    desktop = Desktop()

    status, message, context = asyncio.run(probe_desktop(desktop))

    assert desktop.calls == [("list_windows", {"limit": 5}), ("capture_window", 0, {"max_width": 640})]
    assert status is DiagnosticStatus.HEALTHY
    assert "可用" in message
    assert context == {"windows": 2, "capture_width": 640, "capture_height": 360, "png_bytes": 8}


def test_desktop_probe_degrades_without_visible_windows():
    class Desktop:
        def list_windows(self, **kwargs):
            return []

        def capture_window(self, handle, **kwargs):
            return SimpleNamespace(width=100, height=50, png=b"\x89PNG")

    status, message, context = asyncio.run(probe_desktop(Desktop()))

    assert status is DiagnosticStatus.DEGRADED
    assert "窗口" in message
    assert context["windows"] == 0


def test_desktop_probe_reports_empty_capture_as_unavailable():
    class Desktop:
        def list_windows(self, **kwargs):
            return [object()]

        def capture_window(self, handle, **kwargs):
            return SimpleNamespace(width=0, height=0, png=b"")

    status, message, context = asyncio.run(probe_desktop(Desktop()))

    assert status is DiagnosticStatus.UNAVAILABLE
    assert "空图像" in message
    assert context["png_bytes"] == 0
