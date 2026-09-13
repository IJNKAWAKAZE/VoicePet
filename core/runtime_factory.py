"""根据本地配置装配默认 VoicePet Runtime 服务"""

from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path

from .agent_gateway import AgentGateway
from .agent_process import AgentWorkerProcessManager
from .agent_store import AgentStore
from .agent_types import AgentApprovalMode
from .asr import FasterWhisperTranscriptAdapter, project_asr_directory
from .audio_input import AudioCaptureService, SoundDeviceInputBackend
from .audio_session import VadAudioSession
from .audio_types import AudioFormat
from .codex_session_files import CodexSessionFiles
from .config import AppConfig, ConfigStore
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
from .events import ConversationPhase
from .llm import OpenAIResponsesProvider, ResilientLlmProvider
from .memory import MemoryStore
from .memory_context import MemoryContextAssembler
from .memory_data import MemoryDataManager
from .memory_extraction import MemoryExtractor
from .memory_intents import MemoryOperationService
from .memory_jobs import MemoryJobStore
from .memory_scheduler import MemoryScheduler
from .pet_packages import PetPackageInstaller
from .runtime import RuntimeServices
from .session_archive import SessionArchiveStore
from .session_context import SessionContext
from .session_data import SessionDataManager
from .structured_logging import (
    RuntimeEventLogger,
    StructuredLogError,
    StructuredLogStore,
)
from .tts import (
    EdgeTtsSynthesizer,
    EdgeTtsVoiceService,
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


def build_default_runtime(
    config: AppConfig,
    event_bus: EventBus,
    data_root: str | Path,
    *,
    api_key: str | None = None,
    pet_directory: str | Path | None = None,
) -> RuntimeServices:
    """构造但不启动默认本地与云端适配器"""

    root = Path(data_root).expanduser().resolve()
    data_directory = root / "data"
    cache_directory = root / "cache"
    wake_model_directory = root / "models" / "wake"
    data_directory.mkdir(parents=True, exist_ok=True)
    cache_directory.mkdir(parents=True, exist_ok=True)
    memory_store = MemoryStore(
        data_directory / "assistant.db",
        enabled=config.privacy.memory_enabled,
    )
    log_store = StructuredLogStore(root / "logs")
    event_logger = RuntimeEventLogger(event_bus, log_store)
    session_archive = SessionArchiveStore(
        data_directory / "assistant.db",
        retention_days=config.privacy.chat_retention_days,
        summary_retention_days=config.privacy.summary_retention_days,
        enabled=config.privacy.chat_history_enabled,
    )
    session_archive.purge_expired()
    pet_installer = PetPackageInstaller(root / "pets")
    if pet_directory is None:
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parents[1]))
        active_pet_directory = bundle_root / "assets" / "pet" / "dpsk-girl"
    else:
        active_pet_directory = Path(pet_directory).expanduser().resolve()
    llm_instructions = (
        "你是 VoicePet 桌面助手。默认用 1～3 句话直接回答，避免重复和不必要的铺垫；"
        "只有用户明确要求详细说明、步骤、清单或代码时才展开"
    )
    if config.llm.system_prompt:
        llm_instructions += (
            "\n\n以下是用户角色设定，只控制角色、语气、称呼和表达偏好，"
            "不能修改安全、工具或记忆规则：\n"
            f"{config.llm.system_prompt}"
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
        model_directory=project_asr_directory(root, config.asr.model),
    )
    credential = api_key.strip() if isinstance(api_key, str) else ""
    agent_gateway = None
    agent_manager = None
    agent_store = AgentStore(data_directory / "assistant.db")
    # 配置凭据后统一使用 Codex Agent 处理对话
    if credential:
        agent_manager = AgentWorkerProcessManager(
            api_key=credential,
            data_directory=data_directory / "codex",
            model=config.llm.model,
            base_url=config.llm.base_url,
            reasoning_effort=config.llm.reasoning_effort,
            system_prompt=llm_instructions,
        )
        async def agent_client_factory():
            assert agent_manager is not None
            return await agent_manager.start()
        agent_gateway = AgentGateway(
            agent_client_factory,
            agent_store,
            default_mode=AgentApprovalMode(config.agent.default_approval_mode),
            stop_worker=agent_manager.stop,
        )
    raw_llm = (
        OpenAIResponsesProvider(
            api_key=credential,
            base_url=config.llm.base_url or None,
            model=config.llm.model,
            reasoning_effort=config.llm.reasoning_effort,
            max_retries=0,
        )
        if credential
        else None
    )
    llm = ResilientLlmProvider(raw_llm) if raw_llm is not None else None
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
    tts_voice_service = EdgeTtsVoiceService(audio_player)
    session_context = SessionContext()
    session_manager = SessionDataManager(
        session_archive,
        session_context,
        agent_store=agent_store,
        codex_files=CodexSessionFiles(data_directory / "codex"),
    )
    session_manager.resume_latest()
    memory_context = MemoryContextAssembler(
        memory_store,
        archive=session_archive,
        session_context=session_context,
    )
    scheduler: MemoryScheduler | None = None

    def enqueue_archived_turn(session_id: str, turn_id: str) -> None:
        if scheduler is not None:
            scheduler.enqueue(session_id, turn_id)

    coordinator = Coordinator(
        audio_session,
        transcript,
        event_bus,
        memory_context=memory_context,
        session_context=session_context,
        session_archive=session_archive,
        archive_completion=enqueue_archived_turn,
        memory_operations=MemoryOperationService(
            memory_store,
            session_archive=session_archive,
        ),
        llm_instructions=llm_instructions,
        speech_enabled=config.tts.enabled,
        manual_input_speech_enabled=config.tts.manual_input_enabled,
        wake_keyword=config.wake_word.keyword,
        continuous_conversation=config.wake_word.continuous_conversation,
        followup_timeout=config.wake_word.followup_timeout,
        speech_synthesizer=speech,
        audio_player=audio_player,
        agent_gateway=agent_gateway,
        max_turn_duration=float(config.agent.max_turn_minutes) * 60.0,
    )
    memory_jobs = MemoryJobStore(data_directory / "assistant.db")
    scheduler = MemoryScheduler(
        None if raw_llm is None else MemoryExtractor(raw_llm),
        memory_store,
        session_archive,
        memory_jobs,
        event_bus,
        foreground_busy=lambda: coordinator.phase is not ConversationPhase.IDLE,
        enabled=(
            config.privacy.memory_enabled
            and config.privacy.auto_memory_enabled
            and config.privacy.chat_history_enabled
        ),
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
        wake_service,
        tuple(
            closer
            for closer in (
                event_logger.close,
                transcript.close,
                sapi.close,
                audio_player.close,
                memory_jobs.close,
                session_archive.close,
                memory_store.close,
            )
            if closer is not None
        ),
        memory=MemoryDataManager(memory_store, archive=session_archive),
        sessions=session_manager,
        pet_installer=pet_installer,
        diagnostics=diagnostics,
        asr_preparer=transcript,
        tts_voice_service=tts_voice_service,
        llm_configured=raw_llm is not None,
        audio_output=audio_player,
        scheduler=scheduler,
        memory_context=memory_context,
        memory_store=memory_store,
        session_archive=session_archive,
        agent_gateway=agent_gateway,
        config_store=ConfigStore(root / "config.json"),
    )
