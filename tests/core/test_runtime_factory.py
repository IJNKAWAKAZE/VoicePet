import asyncio
import json
from dataclasses import replace
from pathlib import Path

import core
import core.runtime_factory as runtime_factory_module
from core.audio_input import AudioCaptureService
from core.config import AppConfig
from core.coordinator import Coordinator
from core.event_bus import EventBus
from core.events import ConversationPhase, CorrelationId, StateChanged, TurnId
from core.llm import ResilientLlmProvider
from core.runtime import RuntimeHost
from core.runtime_factory import build_default_runtime
from core.session_archive import SessionArchiveStore
from core.session_context import SessionContext
from core.tts import FallbackSpeechSynthesizer, ResilientSpeechSynthesizer
from core.turn_budget import TurnBudgetLimits
from core.wake import WakeRuntimeStatus, WakeWordRuntimeService
from core.wake_models import WAKE_MODEL_NAME
from core.worker_process import ToolWorkerProcessManager


def test_default_runtime_factory_is_publicly_exported():
    assert core.build_default_runtime is build_default_runtime


def test_default_runtime_factory_wires_local_fallback_and_private_worker(tmp_path):
    event_bus = EventBus()

    services = build_default_runtime(
        AppConfig(),
        event_bus,
        tmp_path / "VoicePet",
        api_key=None,
    )
    try:
        assert services.event_bus is event_bus
        assert isinstance(services.capture, AudioCaptureService)
        assert services.capture.audio_format.sample_rate == 16_000
        assert isinstance(services.coordinator, Coordinator)
        assert isinstance(services.worker, ToolWorkerProcessManager)
        assert services.memory is not None
        assert services.diagnostics is not None
        assert services.memory.list_records() == ()
        assert isinstance(services.wake_service, WakeWordRuntimeService)
        assert services.wake_service.status is WakeRuntimeStatus.MISSING
        assert services.coordinator._llm_provider is None
        assert services.llm_configured is False
        assert services.asr_preparer is services.coordinator._transcript_adapter
        assert isinstance(services.coordinator._session_context, SessionContext)
        assert isinstance(
            services.coordinator._session_archive,
            SessionArchiveStore,
        )
        assert services.coordinator._session_archive._retention_days == 7

        turn_id = TurnId.new()
        correlation_id = CorrelationId.new()
        asyncio.run(
            event_bus.publish(
                StateChanged(
                    turn_id,
                    correlation_id,
                    ConversationPhase.IDLE,
                    ConversationPhase.LISTENING,
                )
            )
        )
        log_tail = services.diagnostics._log_text()
        log_record = json.loads(log_tail.splitlines()[-1])
        assert log_record["turn_id"] == str(turn_id)
        assert log_record["correlation_id"] == str(correlation_id)
        assert (tmp_path / "VoicePet" / "logs" / "voicepet.jsonl").is_file()
        checks = services.diagnostics._runner._checks
        assert [check.component for check in checks] == [
            "audio",
            "asr",
            "llm",
            "tts",
            "tool_worker",
            "database",
            "pet",
        ]
        assert {check.component: check.timeout for check in checks} == {
            "audio": 5.0,
            "asr": 120.0,
            "llm": 15.0,
            "tts": 15.0,
            "tool_worker": 5.0,
            "database": 5.0,
            "pet": 10.0,
        }

        bootstrap = services.worker._bootstrap
        assert len(bootstrap.secret) == 32
        assert bootstrap.audit_database == (
            tmp_path / "VoicePet" / "data" / "assistant.db"
        ).resolve()
        assert bootstrap.allowed_roots == ((tmp_path / "VoicePet").resolve(),)
        assert (tmp_path / "VoicePet" / "cache").is_dir()
        command_text = " ".join(services.worker.command)
        assert bootstrap.secret.hex() not in command_text
        assert {
            definition.name for definition in services.coordinator._llm_tools
        } == {
            "system_info",
            "move_file",
            "undo_move_file",
        }
        assert services.coordinator._memory_candidates is None
        assert services.coordinator._short_term_summaries is None
        assert "update_daily_summary" not in services.coordinator._llm_instructions
        assert "candidate" not in services.coordinator._llm_instructions
        assert "1～3 句" in services.coordinator._llm_instructions
        assert "明确要求" in services.coordinator._llm_instructions
        assert "详细" in services.coordinator._llm_instructions
        assert all(Path(root).is_absolute() for root in bootstrap.allowed_roots)
    finally:
        RuntimeHost(services).close()


def test_default_runtime_factory_wires_model_and_turn_budget_defaults(tmp_path):
    config = AppConfig()
    config = replace(
        config,
        llm=replace(config.llm, model="gpt-runtime-budget-test"),
    )
    services = build_default_runtime(
        config,
        EventBus(),
        tmp_path / "VoicePet",
    )
    try:
        assert services.coordinator._llm_model == "gpt-runtime-budget-test"
        assert services.coordinator._budget_limits == TurnBudgetLimits()
    finally:
        RuntimeHost(services).close()


def test_default_runtime_factory_wires_speech_enabled_setting(tmp_path):
    config = AppConfig()
    config = replace(config, tts=replace(config.tts, enabled=False))

    services = build_default_runtime(
        config,
        EventBus(),
        tmp_path / "VoicePet",
    )
    try:
        assert services.coordinator.speech_enabled is False
    finally:
        RuntimeHost(services).close()


def test_default_runtime_factory_appends_persona_after_builtin_rules(tmp_path):
    persona = "你是一只沉稳的蓝色猫娘，称呼用户为主人"
    config = replace(
        AppConfig(),
        llm=replace(AppConfig().llm, system_prompt=persona),
    )

    services = build_default_runtime(
        config,
        EventBus(),
        tmp_path / "VoicePet",
    )
    try:
        instructions = services.coordinator._llm_instructions
        assert "1～3 句" in instructions
        assert "不能修改安全、工具或记忆规则" in instructions
        assert instructions.index(persona) > instructions.index("1～3 句")
    finally:
        RuntimeHost(services).close()


def test_default_runtime_factory_hides_candidate_tool_when_memory_is_disabled(
    tmp_path,
):
    config = AppConfig()
    config = replace(
        config,
        privacy=replace(config.privacy, memory_enabled=False),
    )
    services = build_default_runtime(
        config,
        EventBus(),
        tmp_path / "VoicePet",
    )
    try:
        assert "propose_memory" not in {
            definition.name for definition in services.coordinator._llm_tools
        }
        assert services.coordinator._memory_candidates is None
        assert services.coordinator._short_term_summaries is None
        assert "update_daily_summary" not in services.coordinator._llm_instructions
    finally:
        RuntimeHost(services).close()


def test_default_runtime_factory_wraps_network_services_without_sdk_retries(
    tmp_path,
    monkeypatch,
):
    seen = {}

    class LlmProvider:
        def __init__(self, **settings):
            seen["llm_settings"] = settings

        async def probe(self):
            return {"model": "fake"}

        def stream(self, request, token):
            del request, token

            async def events():
                if False:
                    yield None

            return events()

    class EdgeSynthesizer:
        def __init__(self, **settings):
            seen["tts_settings"] = settings

        async def synthesize(self, text, token):
            del text, token
            raise AssertionError("测试不应调用在线语音")

    monkeypatch.setattr(
        runtime_factory_module,
        "OpenAIResponsesProvider",
        LlmProvider,
    )
    monkeypatch.setattr(
        runtime_factory_module,
        "EdgeTtsSynthesizer",
        EdgeSynthesizer,
    )

    services = build_default_runtime(
        AppConfig(),
        EventBus(),
        tmp_path / "VoicePet",
        api_key="sk-private",
    )
    try:
        llm = services.coordinator._llm_provider
        speech = services.coordinator._speech_synthesizer

        assert isinstance(llm, ResilientLlmProvider)
        assert services.llm_configured is True
        assert services.asr_preparer is services.coordinator._transcript_adapter
        assert isinstance(llm._provider, LlmProvider)
        assert seen["llm_settings"]["max_retries"] == 0
        assert seen["llm_settings"]["base_url"] is None
        assert isinstance(speech, FallbackSpeechSynthesizer)
        assert isinstance(speech._primary, ResilientSpeechSynthesizer)
        assert isinstance(speech._primary._synthesizer, EdgeSynthesizer)
        assert seen["tts_settings"] == {
            "voice": AppConfig().tts.voice,
        }
        assert llm._resilience is not speech._primary._resilience
    finally:
        RuntimeHost(services).close()


def test_runtime_factory_selects_responses_provider_with_custom_base_url(
    tmp_path,
    monkeypatch,
):
    created = []

    class ResponsesProvider:
        def __init__(self, **settings):
            created.append(("responses", settings))

        async def probe(self):
            return {"model": "fake"}

        def stream(self, request, token):
            del request, token

            async def events():
                if False:
                    yield None

            return events()

    class ChatProvider(ResponsesProvider):
        def __init__(self, **settings):
            created.append(("chat", settings))

    monkeypatch.setattr(
        runtime_factory_module,
        "OpenAIResponsesProvider",
        ResponsesProvider,
    )
    monkeypatch.setattr(
        runtime_factory_module,
        "OpenAIChatCompletionsProvider",
        ChatProvider,
        raising=False,
    )
    config = replace(
        AppConfig(),
        llm=replace(
            AppConfig().llm,
            base_url="https://gateway.example/v1",
        ),
    )

    services = build_default_runtime(
        config,
        EventBus(),
        tmp_path / "VoicePet",
        api_key=" key ",
    )
    try:
        assert services.llm_configured is True
        assert created == [
            (
                "responses",
                {
                    "api_key": "key",
                    "base_url": "https://gateway.example/v1",
                    "model": config.llm.model,
                    "reasoning_effort": config.llm.reasoning_effort,
                    "max_retries": 0,
                },
            )
        ]
    finally:
        RuntimeHost(services).close()


def test_runtime_factory_selects_chat_provider_for_keyless_loopback(
    tmp_path,
    monkeypatch,
):
    created = []

    class Provider:
        def __init__(self, **settings):
            created.append(settings)

        async def probe(self):
            return {"model": "fake"}

        def stream(self, request, token):
            del request, token

            async def events():
                if False:
                    yield None

            return events()

    monkeypatch.setattr(
        runtime_factory_module,
        "OpenAIChatCompletionsProvider",
        Provider,
        raising=False,
    )
    config = replace(
        AppConfig(),
        llm=replace(
            AppConfig().llm,
            api="chat_completions",
            base_url="http://localhost:11434/v1",
        ),
    )

    services = build_default_runtime(
        config,
        EventBus(),
        tmp_path / "VoicePet",
        api_key=None,
    )
    try:
        assert services.llm_configured is True
        assert created == [
            {
                "api_key": "voicepet-local",
                "base_url": "http://localhost:11434/v1",
                "model": config.llm.model,
                "reasoning_effort": config.llm.reasoning_effort,
                "max_retries": 0,
            }
        ]
    finally:
        RuntimeHost(services).close()


def test_runtime_factory_requires_key_for_public_custom_endpoint(tmp_path):
    config = replace(
        AppConfig(),
        llm=replace(
            AppConfig().llm,
            base_url="https://gateway.example/v1",
        ),
    )

    services = build_default_runtime(
        config,
        EventBus(),
        tmp_path / "VoicePet",
        api_key=None,
    )
    try:
        assert services.llm_configured is False
        assert services.coordinator._llm_provider is None
    finally:
        RuntimeHost(services).close()


def test_runtime_factory_closes_owned_mci_player(tmp_path, monkeypatch):
    players = []

    class Player:
        def __init__(self, **settings):
            self.settings = settings
            self.closed = 0
            players.append(self)

        async def play(self, audio, token):
            del audio, token

        def close(self):
            self.closed += 1

    monkeypatch.setattr(runtime_factory_module, "WindowsMciAudioPlayer", Player)

    services = build_default_runtime(
        AppConfig(),
        EventBus(),
        tmp_path / "VoicePet",
    )
    RuntimeHost(services).close()

    assert len(players) == 1
    assert services.audio_output is players[0]
    assert services.coordinator._audio_player is players[0]
    assert services.tts_voice_service._audio_player is players[0]
    assert players[0].closed == 1
    assert players[0].settings == {
        "temp_directory": tmp_path / "VoicePet" / "cache"
    }


def test_default_runtime_factory_registers_active_component_probes(
    tmp_path,
    monkeypatch,
):
    seen = []

    async def fake_asr(target):
        seen.append(("asr", target))
        return {"ok": True}

    async def fake_llm(target):
        seen.append(("llm", target))
        return {"ok": True}

    async def fake_tts(target):
        seen.append(("tts", target))
        return {"ok": True}

    async def fake_pet(target, directory):
        seen.append(("pet", target, Path(directory)))
        return {"ok": True}

    monkeypatch.setattr(runtime_factory_module, "probe_asr", fake_asr)
    monkeypatch.setattr(runtime_factory_module, "probe_llm", fake_llm)
    monkeypatch.setattr(runtime_factory_module, "probe_tts", fake_tts)
    monkeypatch.setattr(runtime_factory_module, "probe_pet", fake_pet)
    pet_directory = Path("assets/pet/dpsk-girl").resolve()
    services = build_default_runtime(
        AppConfig(),
        EventBus(),
        tmp_path / "VoicePet",
        pet_directory=pet_directory,
    )
    try:
        checks = {
            check.component: check
            for check in services.diagnostics._runner._checks
        }
        for name in ("asr", "llm", "tts", "pet"):
            asyncio.run(checks[name].check())

        assert [item[0] for item in seen] == ["asr", "llm", "tts", "pet"]
        assert seen[1][1] is None
        assert seen[-1][2] == pet_directory
    finally:
        RuntimeHost(services).close()


def test_default_runtime_factory_enables_managed_wake_service_for_ready_model(
    tmp_path,
    monkeypatch,
):
    model = tmp_path / "VoicePet" / "models" / "wake" / WAKE_MODEL_NAME
    model.mkdir(parents=True)
    for name in (
        "tokens.txt",
        "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
    ):
        (model / name).write_bytes(b"model")
    seen = {}

    class Detector:
        def __init__(self, files, **settings):
            seen["files"] = files
            seen["detector_settings"] = settings

        def set_speaking(self, speaking):
            seen["speaking"] = speaking

        def close(self):
            seen["detector_closed"] = True

    class Monitor:
        def __init__(self, capture, detector, on_wake, audio_format, **settings):
            seen["monitor"] = (capture, detector, on_wake, audio_format, settings)

        async def start(self):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr("core.runtime_factory.SherpaOnnxKeywordDetector", Detector)
    monkeypatch.setattr("core.runtime_factory.WakeWordMonitor", Monitor)

    services = build_default_runtime(
        AppConfig(),
        EventBus(),
        tmp_path / "VoicePet",
    )
    try:
        asyncio.run(services.wake_service.start())

        assert services.wake_service.status is WakeRuntimeStatus.LISTENING
        assert seen["files"].tokens == (model / "tokens.txt").resolve()
        assert seen["detector_settings"]["keyword"] == "你好，小蓝"
        assert seen["monitor"][4]["debounce_sec"] == 1.5
        assert callable(seen["monitor"][4]["on_error"])
        asyncio.run(services.wake_service.stop())
    finally:
        RuntimeHost(services).close()
