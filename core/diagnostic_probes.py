"""为用户主动诊断提供无副作用的组件探针"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .asr import AsrRuntimeStatus
from .cancellation import CancellationSource, CancellationToken
from .diagnostics import CheckOutput, DiagnosticStatus
from .tts import SynthesizedAudio


class AsrDiagnosticTarget(Protocol):
    """主动诊断所需的 ASR 最小边界"""

    @property
    def status(self) -> AsrRuntimeStatus: ...

    async def preload(self) -> None: ...


class LlmDiagnosticTarget(Protocol):
    """主动诊断所需的 LLM 最小边界"""

    async def probe(self) -> Mapping[str, object]: ...


class TtsDiagnosticTarget(Protocol):
    """主动诊断所需的 TTS 最小边界"""

    async def synthesize(
        self,
        text: str,
        token: CancellationToken,
    ) -> SynthesizedAudio: ...


class PetDiagnosticTarget(Protocol):
    """主动诊断所需的桌宠资源校验边界"""

    def validate_directory(self, directory: str | Path) -> str: ...


async def probe_asr(target: AsrDiagnosticTarget) -> CheckOutput:
    """实际预加载模型后报告最终 ASR 后端"""

    await target.preload()
    status = target.status
    level = (
        DiagnosticStatus.DEGRADED
        if status.degraded
        else DiagnosticStatus.HEALTHY
    )
    message = "ASR 已降级但可用" if status.degraded else "ASR 模型可用"
    return level, message, {
        "device": status.device,
        "compute_type": status.compute_type,
    }


async def probe_llm(target: LlmDiagnosticTarget | None) -> CheckOutput:
    """查询已配置模型且不发起生成请求"""

    if target is None:
        return DiagnosticStatus.DEGRADED, "未配置云端 LLM 凭据", {}
    context = dict(await target.probe())
    return DiagnosticStatus.HEALTHY, "LLM 模型可用", context


async def probe_tts(target: TtsDiagnosticTarget) -> CheckOutput:
    """合成固定短语验证语音后端但不播放音频"""

    source = CancellationSource()
    audio = await target.synthesize("语音诊断", source.token)
    return DiagnosticStatus.HEALTHY, "TTS 后端可用", {
        "backend": audio.backend,
        "audio_bytes": len(audio.data),
    }


async def probe_pet(
    target: PetDiagnosticTarget,
    directory: str | Path,
) -> CheckOutput:
    """在线程中复用安装器规则验证当前桌宠资源"""

    pet_id = await asyncio.to_thread(target.validate_directory, directory)
    return DiagnosticStatus.HEALTHY, "桌宠资源有效", {"pet_id": pet_id}


class DesktopCapture(Protocol):
    """主动诊断所需的桌面截图边界"""

    png: bytes
    width: int
    height: int


class DesktopDiagnosticTarget(Protocol):
    """主动诊断所需的桌面自动化最小边界"""

    def list_windows(self, *, include_minimized: bool = False, limit: int = 60) -> list[object]: ...

    def capture_window(self, handle: int, *, max_width: int = 0) -> DesktopCapture: ...


async def probe_desktop(target: DesktopDiagnosticTarget) -> CheckOutput:
    """枚举窗口并截取整屏，验证桌面自动化原语可用且不注入任何输入"""

    windows = await asyncio.to_thread(target.list_windows, limit=5)
    capture = await asyncio.to_thread(target.capture_window, 0, max_width=640)
    context = {
        "windows": len(windows),
        "capture_width": capture.width,
        "capture_height": capture.height,
        "png_bytes": len(capture.png),
    }
    if capture.width <= 0 or capture.height <= 0 or not capture.png:
        return DiagnosticStatus.UNAVAILABLE, "桌面截图返回空图像", context
    if not windows:
        return DiagnosticStatus.DEGRADED, "桌面自动化可用但没有找到可见窗口", context
    return DiagnosticStatus.HEALTHY, "桌面自动化可用", context
