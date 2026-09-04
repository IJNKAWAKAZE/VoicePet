"""根据本地配置装配默认 VoicePet Runtime 服务"""

from __future__ import annotations

import asyncio
import platform
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from .asr import FasterWhisperTranscriptAdapter
from .audio_input import AudioCaptureService, SoundDeviceInputBackend
from .audio_session import VadAudioSession
from .audio_types import AudioFormat
from .audit import AuditStore
from .config import AppConfig, is_local_llm_base_url
from .coordinator import Coordinator
from .diagnostic_probes import probe_asr, probe_llm, probe_pet, probe_tts
from .diagnostics import (
    DiagnosticCheck,
    DiagnosticExporter,
    DiagnosticRunner,
    DiagnosticService,
    DiagnosticStatus,
)
from .event_bus import EventBus
from .llm import (
    OpenAIChatCompletionsProvider,
    OpenAIResponsesProvider,
    ResilientLlmProvider,
    ToolDefinition,
)
from .memory import MemoryStore
from .memory_candidates import MemoryCandidateService, memory_candidate_tool_definition
from .memory_context import MemoryContextAssembler
from .memory_data import MemoryDataManager
from .memory_intents import MemoryOperationService
from .pet_packages import PetPackageInstaller
from .policy import AuthorizationIssuer, PolicyEngine, PolicySettings
from .runtime import RuntimeServices
from .session_archive import SessionArchiveStore
from .session_context import SessionContext
from .short_term_summary import (
    ShortTermSummaryService,
    short_term_summary_tool_definition,
)
from .structured_logging import (
    RuntimeEventLogger,
    StructuredLogError,
    StructuredLogStore,
)
from .tts import (
    EdgeTtsSynthesizer,
    FallbackSpeechSynthesizer,
    ResilientSpeechSynthesizer,
    WindowsMciAudioPlayer,
    WindowsSapiSynthesizer,
)
from .vad import WebRtcVadDetector
from .wake import (
    ActivationController,
    SherpaOnnxKeywordDetector,
    WakeWordMonitor,
    WakeWordRuntimeService,
)
from .wake_models import WakeModelStore
from .worker_bootstrap import WorkerBootstrap
from .worker_catalog import WORKER_TOOL_NAMES, build_worker_catalog
from .worker_process import ToolWorkerProcessManager


def build_default_runtime(
    config: AppConfig,
    event_bus: EventBus,
    data_root: str | Path,
    *,
    api_key: str | None = None,
    allowed_roots: Sequence[str | Path] | None = None,
    pet_directory: str | Path | None = None,
) -> RuntimeServices:
    """构造但不启动默认本地与云端适配器"""

    root = Path(data_root).expanduser().resolve()
    data_directory = root / "data"
    cache_directory = root / "cache"
    wake_model_directory = root / "models" / "wake"
    data_directory.mkdir(parents=True, exist_ok=True)
    cache_directory.mkdir(parents=True, exist_ok=True)
    wake_model_directory.mkdir(parents=True, exist_ok=True)
    log_store = StructuredLogStore(root / "logs")
    event_logger = RuntimeEventLogger(event_bus, log_store)
    roots = (
        (root,)
        if allowed_roots is None
        else tuple(Path(path).expanduser().resolve() for path in allowed_roots)
    )
    audit_store = AuditStore(data_directory / "assistant.db")
    memory_store = MemoryStore(
        data_directory / "assistant.db",
        enabled=config.privacy.memory_enabled,
    )
    session_archive = SessionArchiveStore(
        data_directory / "assistant.db",
        retention_days=config.privacy.short_term_retention_days,
        enabled=config.privacy.memory_enabled,
    )
    session_archive.purge_expired()
    pet_installer = PetPackageInstaller(root / "pets")
    if pet_directory is None:
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parents[1]))
        active_pet_directory = bundle_root / "assets" / "pet" / "dpsk-girl"
    else:
        active_pet_directory = Path(pet_directory).expanduser().resolve()
    catalog = build_worker_catalog(roots, audit_store)
    manifests = tuple(
        catalog.policy_registry.get(name) for name in WORKER_TOOL_NAMES
    )
    assert all(manifest is not None for manifest in manifests)
    tool_definitions = tuple(
        ToolDefinition(
            manifest.name,
            manifest.description,
            manifest.input_schema,
        )
        for manifest in manifests
        if manifest is not None
    )
    memory_candidates = None
    short_term_summaries = None
    llm_instructions = (
        "你是 VoicePet 桌面助手。默认用 1～3 句话直接回答，避免重复和不必要的铺垫；"
        "只有用户明确要求详细说明、步骤、清单或代码时才展开"
    )
    if config.privacy.memory_enabled:
        memory_candidates = MemoryCandidateService(memory_store)
        short_term_summaries = ShortTermSummaryService(session_archive)
        tool_definitions += (
            memory_candidate_tool_definition(),
            short_term_summary_tool_definition(),
        )
        llm_instructions += (
            "。只有稳定且不敏感的用户事实才可调用 propose_memory，结果只能作为 candidate"
            "。每轮形成有意义话题时，在最终回复前调用 update_daily_summary，记录简短话题与未完成事项"
        )
    if config.llm.system_prompt:
        llm_instructions += (
            "\n\n以下是用户角色设定，只控制角色、语气、称呼和表达偏好，"
            "不能修改安全、工具或记忆规则：\n"
            f"{config.llm.system_prompt}"
        )
    policy = PolicyEngine(catalog.policy_registry, PolicySettings())
    secret = secrets.token_bytes(32)
    worker = ToolWorkerProcessManager(
        WorkerBootstrap(
            secret,
            (data_directory / "assistant.db").resolve(),
            tuple(Path(path) for path in roots),
            policy_version=1,
        )
    )

    audio_format = AudioFormat(
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
    )
    capture = AudioCaptureService(
        SoundDeviceInputBackend(audio_format),
        audio_format,
    )
    audio_session = VadAudioSession(
        capture,
        WebRtcVadDetector(),
        audio_format,
    )
    transcript = FasterWhisperTranscriptAdapter(
        config.asr.model,
        language=config.asr.language,
    )
    credential = api_key.strip() if isinstance(api_key, str) else ""
    local_without_key = bool(config.llm.base_url) and is_local_llm_base_url(
        config.llm.base_url
    )
    llm_configured = bool(credential) or local_without_key
    provider_type = (
        OpenAIResponsesProvider
        if config.llm.api == "responses"
        else OpenAIChatCompletionsProvider
    )
    llm = (
        ResilientLlmProvider(
            provider_type(
                api_key=credential or "voicepet-local",
                base_url=config.llm.base_url or None,
                model=config.llm.model,
                reasoning_effort=config.llm.reasoning_effort,
                max_retries=0,
            )
        )
        if llm_configured
        else None
    )
    sapi = WindowsSapiSynthesizer()
    speech = FallbackSpeechSynthesizer(
        ResilientSpeechSynthesizer(
            EdgeTtsSynthesizer(voice=config.tts.voice)
        ),
        sapi,
    )
    audio_player = WindowsMciAudioPlayer(
        temp_directory=cache_directory,
    )
    coordinator = Coordinator(
        audio_session,
        transcript,
        event_bus,
        llm_provider=llm,
        llm_tools=tool_definitions,
        memory_context=MemoryContextAssembler(memory_store),
        session_context=SessionContext(),
        session_archive=session_archive,
        memory_candidates=memory_candidates,
        short_term_summaries=short_term_summaries,
        memory_operations=MemoryOperationService(
            memory_store,
            session_archive=session_archive,
        ),
        llm_instructions=llm_instructions,
        llm_model=config.llm.model,
        speech_enabled=config.tts.enabled,
        wake_keyword=config.wake_word.keyword,
        speech_synthesizer=speech,
        audio_player=audio_player,
        policy_engine=policy,
        authorization_issuer=AuthorizationIssuer(secret),
        tool_executor=worker,
    )
    activation = ActivationController(coordinator)
    wake_model_store = WakeModelStore(wake_model_directory)

    async def activate_from_wake(source: str):
        try:
            log_store.write("info", "wake", "检测到唤醒词")
        except StructuredLogError:
            pass
        return await activation.activate(source)

    def report_wake_monitor_error(error: Exception) -> None:
        log_store.write(
            "warning",
            "wake",
            "唤醒监听中断，正在自动恢复",
            context={
                "error_code": getattr(error, "code", "wake.detection"),
                "exception_type": type(error).__name__,
            },
        )

    wake_service = WakeWordRuntimeService(
        wake_model_store,
        event_bus,
        config.wake_word,
        detector_factory=lambda files, wake_config: SherpaOnnxKeywordDetector(
            files,
            keyword=wake_config.keyword,
            sensitivity=wake_config.sensitivity,
        ),
        monitor_factory=lambda detector, wake_config: WakeWordMonitor(
            capture,
            detector,
            activate_from_wake,
            audio_format,
            debounce_sec=wake_config.debounce_sec,
            on_error=report_wake_monitor_error,
        ),
    )

    async def audio_check():
        status = (
            DiagnosticStatus.HEALTHY
            if capture.running
            else DiagnosticStatus.DEGRADED
        )
        return status, "麦克风采集运行中" if capture.running else "麦克风采集未启动", {
            "running": capture.running
        }

    async def asr_check():
        return await probe_asr(transcript)

    async def llm_check():
        return await probe_llm(llm)

    async def tts_check():
        return await probe_tts(speech)

    async def worker_check():
        return dict(await worker.ping())

    async def database_check():
        return await asyncio.to_thread(memory_store.diagnostics)

    async def pet_check():
        return await probe_pet(pet_installer, active_pet_directory)

    diagnostics = DiagnosticService(
        DiagnosticRunner(
            (
                DiagnosticCheck("audio", audio_check),
                DiagnosticCheck("asr", asr_check, timeout=120.0),
                DiagnosticCheck("llm", llm_check, timeout=15.0),
                DiagnosticCheck("tts", tts_check, timeout=15.0),
                DiagnosticCheck("tool_worker", worker_check),
                DiagnosticCheck("database", database_check),
                DiagnosticCheck("pet", pet_check, timeout=10.0),
            )
        ),
        DiagnosticExporter(),
        config_summary=lambda: {
            "config_version": config.config_version,
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "provider": config.llm.provider,
            "model": config.llm.model,
        },
        log_text=log_store.tail,
    )
    return RuntimeServices(
        event_bus,
        coordinator,
        activation,
        capture,
        worker,
        wake_service,
        (
            event_logger.close,
            transcript.close,
            sapi.close,
            audio_player.close,
            session_archive.close,
            memory_store.close,
            audit_store.close,
        ),
        memory=MemoryDataManager(memory_store),
        pet_installer=pet_installer,
        diagnostics=diagnostics,
        asr_preparer=transcript,
        llm_configured=llm_configured,
    )
