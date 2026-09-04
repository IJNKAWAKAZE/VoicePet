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
