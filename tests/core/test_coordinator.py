import asyncio
from types import SimpleNamespace

import pytest

from core.agent_types import AgentEventType
from core.asr import AsrTranscriptionError
from core.audio_types import AudioDeviceError
from core.cancellation import CancellationToken
from core.coordinator import Coordinator
from core.event_bus import EventBus
from core.events import (
    AgentActivityCompleted,
    AgentActivityOutput,
    AgentActivityStarted,
    AgentApprovalRequested,
    ConversationPhase,
    ErrorSeverity,
    RecordingStarted,
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TextInputSubmitted,
    TranscriptReady,
    WakeCommandPending,
)
from core.llm import LlmAttachment
from core.session_context import SessionContext
from core.state_machine import ConversationStateMachine
from core.tts import SynthesizedAudio, TtsPlaybackError, TtsSynthesisError


@pytest.mark.parametrize("speech,manual,expected", [(True, True, True), (False, True, False), (True, False, False)])
def test_agent_replies_receive_memory_and_use_speech_switches(tmp_path, speech, manual, expected):
    from uuid import uuid4

    from core.agent_types import AgentEventType
    from core.memory import MemoryStore
    from core.memory_context import MemoryContextAssembler
    from core.session_archive import SessionArchiveStore

    async def scenario():
        store = MemoryStore(tmp_path / "memory.db")
        archive = SessionArchiveStore(tmp_path / "memory.db")
        context = SessionContext(max_turns=1)
        old = archive.archive_turn(str(uuid4()), "旅行计划", "周六出发", context.session_id)
        archive.save_summary(old.turn_id, "旅行计划", (), decisions=("周六出发",))
        store.create_confirmed(category="profile", content="我住在杭州", source_turn_id=str(uuid4()))
        requests, enqueued = [], []

        class Gateway:
            async def run_turn(self, session, text, **kwargs):
                requests.append((text, kwargs))
                yield SimpleNamespace(type=AgentEventType.TEXT_DELTA, payload={"text": "你住在**杭州**😊"})
                yield SimpleNamespace(type=AgentEventType.TURN_COMPLETED, payload={"status": "completed"})

        synth, player = RecordingSynthesizer(), RecordingPlayer()
        coordinator = Coordinator(
            ImmediateAudio(), UnexpectedTranscript(), EventBus(), agent_gateway=Gateway(),
            memory_context=MemoryContextAssembler(store, archive=archive, session_context=context),
            session_context=context, session_archive=archive,
            archive_completion=lambda *args: enqueued.append(args),
            speech_synthesizer=synth, audio_player=player,
            speech_enabled=speech, manual_input_speech_enabled=manual,
        )
        try:
            await coordinator.submit_text("我在哪个城市？")
            await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
            assert requests[0][0] == "我在哪个城市？"
            assert "杭州" in requests[0][1]["context"]
            assert "周六出发" in requests[0][1]["context"]
            assert bool(player.calls) is expected
            assert [text for text, _ in synth.calls] == (["你住在杭州"] if expected else [])
            assert coordinator.response_text == "你住在**杭州**😊"
            assert len(enqueued) == 1
            assert context.build_history()[-2]["content"] == "我在哪个城市？"
        finally:
            await coordinator.stop()
            archive.close()
            store.close()

    asyncio.run(scenario())


def test_agent_stalled_stream_obeys_budget_and_reports_error():
    async def scenario():
        closed = asyncio.Event()

        class Gateway:
            async def run_turn(self, *args, **kwargs):
                try:
                    await asyncio.Event().wait()
                    yield
                finally:
                    closed.set()

        bus, errors = EventBus(), []
        bus.subscribe(RuntimeErrorEvent, errors.append)
        coordinator = Coordinator(ImmediateAudio(), UnexpectedTranscript(), bus,
                                  agent_gateway=Gateway(), max_turn_duration=0.03)
        try:
            await coordinator.submit_text("查询")
            await wait_until(lambda: bool(errors))
            assert errors[0].error_code == "runtime.budget"
            assert closed.is_set()
        finally:
            await coordinator.stop()

    asyncio.run(scenario())


def test_cancelling_stalled_agent_releases_stream_before_next_turn():
    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()

        class Gateway:
            async def run_turn(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                    yield
                finally:
                    closed.set()

            async def cancel(self):
                pass

        coordinator = Coordinator(ImmediateAudio(), UnexpectedTranscript(), EventBus(), agent_gateway=Gateway())
        try:
            await coordinator.submit_text("查询")
            await asyncio.wait_for(started.wait(), 0.5)
            await asyncio.wait_for(coordinator.cancel_active_turn(), 0.5)
            assert closed.is_set()
            assert coordinator.phase is ConversationPhase.IDLE
        finally:
            await coordinator.stop()

    asyncio.run(scenario())


def test_history_playback_strips_markdown_and_emoji_and_accepts_long_text():
    async def scenario():
        synth = RecordingSynthesizer()
        coordinator = Coordinator(ImmediateAudio(), UnexpectedTranscript(), EventBus(),
                                  speech_synthesizer=synth, audio_player=RecordingPlayer())
        try:
            await coordinator.speak_notice("# **杭州**😊\n[西湖](https://example.com)\n" + "这是聊天记录。" * 80)
            spoken = "".join(text for text, _ in synth.calls)
            assert spoken.startswith("杭州\n西湖\n")
            assert all(symbol not in spoken for symbol in ("#", "*", "😊", "https://"))
            assert spoken.endswith("这是聊天记录。" * 80)
        finally:
            await coordinator.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["manual", "replay"])
def test_manual_actions_after_wake_never_start_followup_recording(operation):
    from core.agent_types import AgentEventType

    async def scenario():
        class Gateway:
            async def run_turn(self, *args, **kwargs):
                yield SimpleNamespace(type=AgentEventType.TEXT_DELTA, payload={"text": "手动消息的回复"})
                yield SimpleNamespace(type=AgentEventType.TURN_COMPLETED, payload={"status": "completed"})

            async def cancel(self):
                pass

        audio, bus, states = SourceAwareAudio(), EventBus(), []
        bus.subscribe(StateChanged, states.append)
        coordinator = Coordinator(
            audio, SequenceTranscript([""]), bus, agent_gateway=Gateway(),
            continuous_conversation=True, manual_input_speech_enabled=True,
            speech_synthesizer=RecordingSynthesizer(), audio_player=RecordingPlayer(),
        )
        try:
            await coordinator.start_listening("wake_word")
            await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
            states.clear()
            if operation == "manual":
                await coordinator.submit_text("手动问题")
                await wait_until(lambda: any(event.current is ConversationPhase.SPEAKING for event in states))
            else:
                await coordinator.speak_notice("聊天记录的回复")
            await asyncio.sleep(0.01)
            assert coordinator.phase is ConversationPhase.IDLE
            assert audio.include_preroll == [False]
            assert all(event.current is not ConversationPhase.LISTENING for event in states)
        finally:
            await coordinator.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("original_source", ["manual", "wake_word"])
def test_click_interrupt_uses_its_own_voice_mode_and_does_not_continue(original_source):
    from core.agent_types import AgentEventType

    async def scenario():
        class Gateway:
            async def run_turn(self, *args, **kwargs):
                yield SimpleNamespace(type=AgentEventType.TEXT_DELTA, payload={"text": "点击录音后的回复"})
                yield SimpleNamespace(type=AgentEventType.TURN_COMPLETED, payload={"status": "completed"})

            async def cancel(self):
                pass

        audio, synth = SourceAwareAudio(), RecordingSynthesizer()
        coordinator = Coordinator(
            audio, SequenceTranscript(["语音问题"]), EventBus(), agent_gateway=Gateway(),
            continuous_conversation=True, manual_input_speech_enabled=False,
            speech_synthesizer=synth, audio_player=RecordingPlayer(),
        )
        try:
            if original_source == "manual":
                await coordinator.submit_text("手动问题")
            else:
                await coordinator.start_listening(original_source)
            await coordinator.interrupt("click")
            await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
            assert audio.include_preroll == [True]
            assert "点击录音后的回复" in [text for text, _ in synth.calls]
        finally:
            await coordinator.stop()

    asyncio.run(scenario())


def test_wake_reply_still_starts_followup_recording():
    from core.agent_types import AgentEventType

    async def scenario():
        class Gateway:
            async def run_turn(self, *args, **kwargs):
                yield SimpleNamespace(type=AgentEventType.TEXT_DELTA, payload={"text": "语音问题的回复"})
                yield SimpleNamespace(type=AgentEventType.TURN_COMPLETED, payload={"status": "completed"})

            async def cancel(self):
                pass

        audio = SourceAwareAudio()
        coordinator = Coordinator(
            audio, SequenceTranscript(["语音问题", "结束对话"]), EventBus(), agent_gateway=Gateway(),
            continuous_conversation=True,
            speech_synthesizer=RecordingSynthesizer(), audio_player=RecordingPlayer(),
        )
        try:
            await coordinator.start_listening("wake_word")
            await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
            assert audio.include_preroll == [False, False]
        finally:
            await coordinator.stop()

    asyncio.run(scenario())


async def wait_until(predicate, timeout: float = 2.0):
    # 按实际时间等待后台线程完成，避免空转次数受机器负载影响
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not reached")


class ImmediateAudio:
    def __init__(self) -> None:
        self.calls: list[CancellationToken] = []

    async def record_until_silence(self, token: CancellationToken) -> bytes:
        self.calls.append(token)
        return f"audio-{len(self.calls)}".encode()


class SourceAwareAudio:
    def __init__(self) -> None:
        self.include_preroll = []

    async def record_until_silence(self, token, *, include_preroll=True):
        token.throw_if_cancelled()
        self.include_preroll.append(include_preroll)
        return b"audio"


class SequenceTranscript:
    def __init__(self, texts):
        self.texts = iter(texts)
        self.calls = 0

    async def transcribe(self, audio, token):
        del audio
        token.throw_if_cancelled()
        self.calls += 1
        return next(self.texts)


class ControlledTranscript:
    def __init__(self, results: list[str]) -> None:
        self.results = results
        self.releases = [asyncio.Event() for _ in results]
        self.calls: list[tuple[bytes, CancellationToken]] = []

    async def transcribe(
        self,
        audio: bytes,
        token: CancellationToken,
    ) -> str:
        index = len(self.calls)
        self.calls.append((audio, token))
        await self.releases[index].wait()
        return self.results[index]


class NeverEndingAudio:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.task_cancelled = asyncio.Event()

    async def record_until_silence(self, token: CancellationToken) -> bytes:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.task_cancelled.set()
            raise


class UnexpectedTranscript:
    async def transcribe(
        self,
        audio: bytes,
        token: CancellationToken,
    ) -> str:
        raise AssertionError("transcribe must not be called")


class EmptyAudio:
    async def record_until_silence(self, token: CancellationToken) -> bytes:
        return b""


class FailingAudio:
    async def record_until_silence(self, token: CancellationToken) -> bytes:
        raise AudioDeviceError("microphone disconnected")


class DelayedFirstAudioFailure:
    def __init__(self) -> None:
        self.calls = 0
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def record_until_silence(self, token: CancellationToken) -> bytes:
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
            raise AudioDeviceError("stale microphone failure")
        self.second_started.set()
        await token.wait()
        token.throw_if_cancelled()
        raise AssertionError("cancelled recording must not continue")


class FailingTranscript:
    async def transcribe(
        self,
        audio: bytes,
        token: CancellationToken,
    ) -> str:
        raise AsrTranscriptionError("model inference failed")


class BlockingTranscript:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def transcribe(self, audio, token):
        del audio, token
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class DelayedFirstTranscriptFailure:
    def __init__(self) -> None:
        self.calls = 0
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def transcribe(
        self,
        audio: bytes,
        token: CancellationToken,
    ) -> str:
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
            raise AsrTranscriptionError("stale model failure")
        self.second_started.set()
        await token.wait()
        token.throw_if_cancelled()
        raise AssertionError("cancelled transcript must not continue")


class RecordingSynthesizer:
    def __init__(self, error=None) -> None:
        self.error = error
        self.calls = []

    async def synthesize(self, text, token):
        self.calls.append((text, token))
        if self.error is not None:
            raise self.error
        return SynthesizedAudio(
            f"audio:{text}".encode(),
            "audio/wav",
            ".wav",
            "test-tts",
        )


class RecordingPlayer:
    def __init__(self, error=None) -> None:
        self.error = error
        self.calls = []

    async def play(self, audio, token):
        self.calls.append((audio, token))
        if self.error is not None:
            raise self.error


class BlockingSynthesizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def synthesize(self, text, token):
        del text, token
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class BlockingPlayer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def play(self, audio, token):
        del audio, token
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class CooperativeBlockingPlayer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def play(self, audio, token):
        del audio
        self.started.set()
        while True:
            if token.is_cancelled:
                self.cancelled.set()
                token.throw_if_cancelled()
            await asyncio.sleep(0)


class RecordingSessionArchive:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.item_calls = []
        self.discarded = []

    def archive_turn(
        self,
        turn_id,
        user_text,
        assistant_text,
        session_id=None,
        attachments=(),
        assistant_items=(),
    ):
        values = (turn_id, user_text, assistant_text)
        self.calls.append(values if session_id is None else (*values, session_id))
        self.item_calls.append(
            (turn_id, tuple(str(item.get("text", "")) for item in assistant_items))
        )
        if self.error is not None:
            raise self.error
        return type("Record", (), {"turn_id": turn_id})()

    def discard_turn(self, turn_id):
        self.discarded.append(turn_id)
        return True


class DelayedFirstSynthesizer(RecordingSynthesizer):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def synthesize(self, text, token):
        if not self.calls:
            self.calls.append((text, token))
            self.first_started.set()
            await self.release_first.wait()
            return SynthesizedAudio(
                b"stale-audio",
                "audio/wav",
                ".wav",
                "test-tts",
            )
        return await super().synthesize(text, token)


def agent_delta(text: str):
    return SimpleNamespace(type=AgentEventType.TEXT_DELTA, payload={"text": text})


def agent_completed(status: str = "completed"):
    return SimpleNamespace(
        type=AgentEventType.TURN_COMPLETED, payload={"status": status}
    )


def agent_item_delta(item_id: str, text: str):
    """带条目标识的文本增量，聊天历史按条目重建气泡"""
    return SimpleNamespace(
        type=AgentEventType.TEXT_DELTA, payload={"text": text}, item_id=item_id
    )


class ScriptedGateway:
    """按脚本回放 Agent 事件，替代已移除的内置大模型网关"""

    def __init__(self, events) -> None:
        self.events = tuple(events)
        self.calls: list[tuple[str, str, dict]] = []

    async def run_turn(self, session, text, **kwargs):
        self.calls.append((session, text, kwargs))
        for event in self.events:
            await asyncio.sleep(0)
            yield event

    async def cancel(self) -> None:
        return None


class SequencedGateway:
    def __init__(self, batches) -> None:
        self.batches = iter(batches)
        self.calls: list[tuple[str, str, dict]] = []

    async def run_turn(self, session, text, **kwargs):
        self.calls.append((session, text, kwargs))
        for event in next(self.batches):
            yield event

    async def cancel(self) -> None:
        return None


class BlockingGateway:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_turn(self, session, text, **kwargs):
        del session, text, kwargs
        self.started.set()
        try:
            await asyncio.Event().wait()
            yield
        finally:
            self.cancelled.set()

    async def cancel(self) -> None:
        return None


class StreamingGateway:
    """先回放若干条目再挂起，用于验证中断时的补写"""

    def __init__(self, events) -> None:
        self.events = tuple(events)
        self.streamed = asyncio.Event()

    async def run_turn(self, session, text, **kwargs):
        del session, text, kwargs
        for event in self.events:
            yield event
        self.streamed.set()
        await asyncio.Event().wait()

    async def cancel(self) -> None:
        return None


class FailingGateway:
    """回放若干条目后抛错，用于验证失败时的补写"""

    def __init__(self, events, error) -> None:
        self.events = tuple(events)
        self.error = error

    async def run_turn(self, session, text, **kwargs):
        del session, text, kwargs
        for event in self.events:
            yield event
        raise self.error

    async def cancel(self) -> None:
        return None


class DelayedFirstGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def run_turn(self, session, text, **kwargs):
        del session, text, kwargs
        index = self.calls
        self.calls += 1
        if index == 0:
            self.first_started.set()
            await self.release_first.wait()
            yield agent_delta("stale")
            yield agent_completed()
            return
        self.second_started.set()
        yield agent_delta("fresh")
        yield agent_completed()

    async def cancel(self) -> None:
        return None


def test_spoken_notice_bypasses_recording_transcript_and_llm():
    async def scenario():
        audio = ImmediateAudio()
        synthesizer = RecordingSynthesizer()
        player = RecordingPlayer()
        bus = EventBus()
        states = []
        speaks = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(SpeakRequested, speaks.append)
        coordinator = Coordinator(
            audio,
            UnexpectedTranscript(),
            bus,
            speech_synthesizer=synthesizer,
            audio_player=player,
        )

        turn_id = await coordinator.speak_notice("请先配置 API Key")

        assert [event.current for event in states] == [
            ConversationPhase.SYNTHESIZING,
            ConversationPhase.SPEAKING,
            ConversationPhase.IDLE,
        ]
        assert all(event.turn_id == turn_id for event in states)
        assert [event.text for event in speaks] == ["请先配置 API Key"]
        assert [text for text, _ in synthesizer.calls] == ["请先配置 API Key"]
        assert len(player.calls) == 1
        assert audio.calls == []
        await coordinator.stop()

    asyncio.run(scenario())


def test_spoken_notice_rejects_a_second_active_notice():
    async def scenario():
        synthesizer = BlockingSynthesizer()
        coordinator = Coordinator(
            ImmediateAudio(),
            UnexpectedTranscript(),
            EventBus(),
            speech_synthesizer=synthesizer,
            audio_player=RecordingPlayer(),
            shutdown_timeout=0.01,
        )

        assert hasattr(coordinator, "speak_notice")
        first = asyncio.create_task(coordinator.speak_notice("第一条提示"))
        await synthesizer.started.wait()
        with pytest.raises(RuntimeError, match="正在"):
            await coordinator.speak_notice("第二条提示")
        await coordinator.stop()
        await first

    asyncio.run(scenario())


def test_agent_tool_activity_is_published_as_separate_chat_items():
    async def scenario():
        def agent_item(kind, payload, item_id):
            return SimpleNamespace(type=kind, payload=payload, item_id=item_id)

        gateway = ScriptedGateway(
            [
                agent_item(
                    AgentEventType.COMMAND_STARTED,
                    {"message": "正在执行命令", "command": "go vet ./..."},
                    "item-1",
                ),
                agent_item(AgentEventType.COMMAND_OUTPUT_DELTA, {"text": "vet exit=0\n"}, "item-1"),
                agent_item(
                    AgentEventType.COMMAND_COMPLETED,
                    {"message": "命令执行完成", "status": "completed"},
                    "item-1",
                ),
                agent_item(
                    AgentEventType.TOOL_STARTED,
                    {"message": "正在调用", "tool": "voicepet/list_windows"},
                    "item-2",
                ),
                agent_item(
                    AgentEventType.TOOL_COMPLETED,
                    {"message": "调用未完成", "status": "failed", "tool": "voicepet/list_windows"},
                    "item-2",
                ),
                agent_item(AgentEventType.TEXT_DELTA, {"text": "已修复"}, "message-1"),
                agent_completed(),
            ]
        )
        bus = EventBus()
        started, output, completed, deltas = [], [], [], []
        bus.subscribe(AgentActivityStarted, started.append)
        bus.subscribe(AgentActivityOutput, output.append)
        bus.subscribe(AgentActivityCompleted, completed.append)
        bus.subscribe(TextDelta, deltas.append)
        coordinator = Coordinator(
            ImmediateAudio(), UnexpectedTranscript(), bus, agent_gateway=gateway
        )
        try:
            await coordinator.submit_text("修复编译错误")
            await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
        finally:
            await coordinator.stop()

        assert [(item.activity_id, item.kind, item.title) for item in started] == [
            ("item-1", "command", "go vet ./..."),
            ("item-2", "tool", "voicepet/list_windows"),
        ]
        assert [(item.activity_id, item.text) for item in output] == [("item-1", "vet exit=0\n")]
        assert [(item.activity_id, item.status) for item in completed] == [
            ("item-1", "completed"),
            ("item-2", "failed"),
        ]
        assert [item.item_id for item in deltas] == ["message-1"]

    asyncio.run(scenario())


def test_agent_reasoning_becomes_a_thinking_chat_item():
    async def scenario():
        def agent_item(kind, payload, item_id):
            return SimpleNamespace(type=kind, payload=payload, item_id=item_id)

        gateway = ScriptedGateway(
            [
                agent_item(AgentEventType.REASONING_DELTA, {"text": "先看目录"}, "item-1"),
                agent_item(AgentEventType.REASONING_DELTA, {"text": "，再读 README"}, "item-1"),
                agent_item(AgentEventType.REASONING_COMPLETED, {}, "item-1"),
                agent_item(
                    AgentEventType.COMMAND_STARTED,
                    {"message": "正在执行命令", "command": "ls"},
                    "item-2",
                ),
                agent_item(AgentEventType.REASONING_DELTA, {"text": "命令跑完了"}, "item-3"),
                agent_item(AgentEventType.TEXT_DELTA, {"text": "最终答复"}, "message-1"),
                agent_completed(),
            ]
        )
        bus = EventBus()
        started, output, completed, deltas = [], [], [], []
        bus.subscribe(AgentActivityStarted, started.append)
        bus.subscribe(AgentActivityOutput, output.append)
        bus.subscribe(AgentActivityCompleted, completed.append)
        bus.subscribe(TextDelta, deltas.append)
        coordinator = Coordinator(
            ImmediateAudio(), UnexpectedTranscript(), bus, agent_gateway=gateway
        )
        try:
            await coordinator.submit_text("看看项目结构")
            await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
        finally:
            await coordinator.stop()

        # 过程文字独立成条，命令条目保持自己的标识
        assert [(item.activity_id, item.kind, item.title) for item in started] == [
            ("item-1:thinking", "thinking", "正在思考"),
            ("item-2", "command", "ls"),
            ("item-3:thinking", "thinking", "正在思考"),
        ]
        assert [(item.activity_id, item.text) for item in output] == [
            ("item-1:thinking", "先看目录"),
            ("item-1:thinking", "，再读 README"),
            ("item-3:thinking", "命令跑完了"),
        ]
        # 缺少条目终态的推理过程在轮次结束时兜底收尾
        assert [(item.activity_id, item.status) for item in completed] == [
            ("item-1:thinking", "completed"),
            ("item-3:thinking", "completed"),
        ]
        assert [item.text for item in deltas] == ["最终答复"]

    asyncio.run(scenario())


def test_manual_text_bypasses_audio_and_asr_but_uses_full_response_pipeline():
    async def scenario():
        audio = ImmediateAudio()
        gateway = ScriptedGateway([agent_delta("手动回复"), agent_completed()])
        archive = RecordingSessionArchive()
        synthesizer = RecordingSynthesizer()
        player = RecordingPlayer()
        bus = EventBus()
        states = []
        inputs = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(TextInputSubmitted, inputs.append)
        coordinator = Coordinator(
            audio,
            UnexpectedTranscript(),
            bus,
            agent_gateway=gateway,
            session_archive=archive,
            speech_synthesizer=synthesizer,
            audio_player=player,
            manual_input_speech_enabled=True,
        )

        turn_id = await coordinator.submit_text("  手动问题  ")
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert audio.calls == []
        assert [event.current for event in states] == [
            ConversationPhase.THINKING,
            ConversationPhase.SYNTHESIZING,
            ConversationPhase.SPEAKING,
            ConversationPhase.IDLE,
        ]
        assert [(event.turn_id, event.text) for event in inputs] == [
            (turn_id, "手动问题")
        ]
        assert gateway.calls[0][1] == "手动问题"
        assert archive.calls == [(str(turn_id), "手动问题", "手动回复")]
        assert [text for text, _ in synthesizer.calls] == ["手动回复"]
        assert len(player.calls) == 1
        await coordinator.stop()

    asyncio.run(scenario())


def test_manual_text_passes_attachments_to_agent_request():
    async def scenario():
        attachment = LlmAttachment("C:/tmp/report.txt", "report.txt", "text/plain", 4, "a" * 64, "file")
        gateway = ScriptedGateway([agent_delta("收到"), agent_completed()])
        coordinator = Coordinator(
            ImmediateAudio(), UnexpectedTranscript(), EventBus(), agent_gateway=gateway
        )
        await coordinator.submit_text("请查看", (attachment,))
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
        assert gateway.calls[0][2]["attachments"] == (
            {
                "name": "report.txt",
                "path": "C:/tmp/report.txt",
                "media_type": "text/plain",
                "kind": "file",
            },
        )
        await coordinator.stop()

    asyncio.run(scenario())


def test_manual_voice_input_records_until_silence_and_returns_transcript():
    async def scenario():
        audio = SourceAwareAudio()
        coordinator = Coordinator(audio, SequenceTranscript(["  语音输入  "]), EventBus())

        text = await coordinator.capture_manual_transcript()

        assert text == "语音输入"
        assert audio.include_preroll == [False]
        await coordinator.stop()

    asyncio.run(scenario())


def test_manual_text_skips_tts_when_manual_speech_is_disabled():
    async def scenario():
        synthesizer = RecordingSynthesizer()
        player = RecordingPlayer()
        coordinator = Coordinator(
            ImmediateAudio(),
            UnexpectedTranscript(),
            EventBus(),
            agent_gateway=ScriptedGateway(
                [agent_delta("安静回复"), agent_completed()]
            ),
            speech_synthesizer=synthesizer,
            audio_player=player,
            manual_input_speech_enabled=False,
        )

        await coordinator.submit_text("手动问题")
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert coordinator.response_text == "安静回复"
        assert synthesizer.calls == []
        assert player.calls == []
        await coordinator.stop()

    asyncio.run(scenario())


def test_manual_text_rejects_invalid_or_busy_submission():
    async def scenario():
        gateway = BlockingGateway()
        coordinator = Coordinator(
            ImmediateAudio(),
            UnexpectedTranscript(),
            EventBus(),
            agent_gateway=gateway,
            shutdown_timeout=0.01,
        )

        for text in ("", "   ", "x" * 4097):
            with pytest.raises(ValueError):
                await coordinator.submit_text(text)
        await coordinator.submit_text("第一条")
        await gateway.started.wait()
        with pytest.raises(RuntimeError, match="cannot start text turn"):
            await coordinator.submit_text("第二条")
        await coordinator.stop()

    asyncio.run(scenario())


def test_voice_and_manual_text_share_one_current_session_context():
    async def scenario():
        session = SessionContext()
        archive = RecordingSessionArchive()
        gateway = ScriptedGateway([agent_delta("共同回复"), agent_completed()])
        coordinator = Coordinator(
            ImmediateAudio(),
            SequenceTranscript(["语音问题"]),
            EventBus(),
            agent_gateway=gateway,
            session_context=session,
            session_archive=archive,
        )

        await coordinator.start_listening()
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)
        await coordinator.submit_text("文字问题")
        await wait_until(
            lambda: coordinator.phase is ConversationPhase.IDLE
            and len(gateway.calls) == 2
        )

        assert [call[1] for call in gateway.calls] == ["语音问题", "文字问题"]
        assert len(archive.calls) == 2
        assert archive.calls[0][3] == session.session_id
        assert archive.calls[1][3] == session.session_id
        await coordinator.stop()

    asyncio.run(scenario())


def test_normal_turn_uses_distinct_operation_ids_and_reaches_thinking():
    async def scenario():
        audio = ImmediateAudio()
        transcript = ControlledTranscript(["  hello  "])
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        transcripts = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(TranscriptReady, transcripts.append)
        coordinator = Coordinator(audio, transcript, bus, machine)

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: machine.phase is ConversationPhase.THINKING)

        assert [event.current for event in states] == [
            ConversationPhase.LISTENING,
            ConversationPhase.TRANSCRIBING,
            ConversationPhase.THINKING,
        ]
        assert sum(
            event.current is ConversationPhase.LISTENING for event in states
        ) == 1
        assert all(event.turn_id == turn_id for event in states)
        assert transcripts[0].turn_id == turn_id
        assert transcripts[0].text == "hello"
        assert states[0].correlation_id != states[1].correlation_id
        assert transcripts[0].correlation_id == states[1].correlation_id
        assert states[2].correlation_id == states[1].correlation_id
        await coordinator.stop()

    asyncio.run(scenario())


def test_interrupt_starts_replacement_before_old_transcript_finishes():
    async def scenario():
        audio = ImmediateAudio()
        transcript = ControlledTranscript(["stale", "fresh"])
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        transcripts = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(TranscriptReady, transcripts.append)
        coordinator = Coordinator(audio, transcript, bus, machine)

        old_turn = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        old_token = transcript.calls[0][1]

        new_turn = await coordinator.interrupt()
        await wait_until(lambda: len(transcript.calls) == 2)

        assert new_turn != old_turn
        assert old_token.is_cancelled
        assert not transcript.calls[1][1].is_cancelled
        listening = [
            event for event in states
            if event.current is ConversationPhase.LISTENING
        ]
        assert [event.turn_id for event in listening] == [old_turn, new_turn]
        assert listening[0].correlation_id != listening[1].correlation_id

        transcript.releases[0].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert all(event.turn_id != old_turn for event in transcripts)
        assert machine.turn_id == new_turn

        transcript.releases[1].set()
        await wait_until(lambda: machine.phase is ConversationPhase.THINKING)
        assert [(event.turn_id, event.text) for event in transcripts] == [
            (new_turn, "fresh")
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_cancel_active_turn_cancels_token_and_returns_to_idle():
    async def scenario():
        audio = ImmediateAudio()
        transcript = ControlledTranscript(["stale"])
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        bus.subscribe(StateChanged, states.append)
        coordinator = Coordinator(audio, transcript, bus, machine)

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        token = transcript.calls[0][1]

        await coordinator.cancel_active_turn()
        await coordinator.cancel_active_turn()

        assert token.is_cancelled
        assert machine.phase is ConversationPhase.IDLE
        assert states[-1].current is ConversationPhase.IDLE
        assert sum(event.current is ConversationPhase.IDLE for event in states) == 1
        transcript.releases[0].set()
        await coordinator.stop()

    asyncio.run(scenario())


def test_blank_transcript_returns_to_idle_without_transcript_event():
    async def scenario():
        audio = ImmediateAudio()
        transcript = ControlledTranscript(["   "])
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        transcripts = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(TranscriptReady, transcripts.append)
        coordinator = Coordinator(audio, transcript, bus, machine)

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        assert transcripts == []
        assert states[-1].current is ConversationPhase.IDLE
        assert machine.turn_id is None
        await coordinator.stop()

    asyncio.run(scenario())


def test_wake_source_acknowledges_then_records_without_preroll():
    async def scenario():
        bus = EventBus()
        transcripts = []
        pending = []
        speaks = []
        bus.subscribe(TranscriptReady, transcripts.append)
        bus.subscribe(WakeCommandPending, pending.append)
        bus.subscribe(SpeakRequested, speaks.append)
        audio = SourceAwareAudio()
        synthesizer = RecordingSynthesizer()
        player = RecordingPlayer()
        coordinator = Coordinator(
            audio,
            SequenceTranscript(["打开设置"]),
            bus,
            wake_keyword="你好，小蓝",
            speech_synthesizer=synthesizer,
            audio_player=player,
        )

        turn_id = await coordinator.start_listening("wake_word")
        await wait_until(lambda: len(transcripts) == 1)

        assert transcripts[0].turn_id == turn_id
        assert transcripts[0].text == "打开设置"
        assert len(pending) == 1
        assert [event.text for event in speaks] == ["我在，请说"]
        assert [text for text, _ in synthesizer.calls] == ["我在，请说"]
        assert len(player.calls) == 1
        assert audio.include_preroll == [False]
        await coordinator.stop()

    asyncio.run(scenario())


def test_wake_waits_for_acknowledgement_even_when_reply_speech_disabled():
    async def scenario():
        bus = EventBus()
        started = []
        bus.subscribe(RecordingStarted, started.append)
        release = asyncio.Event()
        playing = asyncio.Event()

        class Player:
            async def play(self, audio, token):
                playing.set()
                await release.wait()

        audio = SourceAwareAudio()
        synthesizer = RecordingSynthesizer()
        coordinator = Coordinator(audio, SequenceTranscript(["打开设置"]), bus,
                                  speech_enabled=False, speech_synthesizer=synthesizer,
                                  audio_player=Player())
        try:
            await coordinator.start_listening("wake_word")
            await wait_until(playing.is_set)
            assert [text for text, _ in synthesizer.calls] == ["我在，请说"]
            assert audio.include_preroll == []
            assert started == []
            release.set()
            await wait_until(lambda: bool(audio.include_preroll))
            assert audio.include_preroll == [False]
            assert len(started) == 1
        finally:
            release.set()
            await coordinator.stop()

    asyncio.run(scenario())


def test_click_source_preserves_matching_keyword_in_transcript():
    async def scenario():
        bus = EventBus()
        transcripts = []
        bus.subscribe(TranscriptReady, transcripts.append)
        coordinator = Coordinator(
            SourceAwareAudio(),
            SequenceTranscript(["你好小蓝，打开设置"]),
            bus,
            wake_keyword="你好，小蓝",
        )

        await coordinator.start_listening("click")
        await wait_until(lambda: len(transcripts) == 1)

        assert transcripts[0].text == "你好小蓝，打开设置"
        await coordinator.stop()

    asyncio.run(scenario())


def test_empty_wake_command_returns_to_idle_without_starting_another_recording():
    async def scenario():
        bus = EventBus()
        pending = []
        bus.subscribe(WakeCommandPending, pending.append)
        audio = SourceAwareAudio()
        transcript = SequenceTranscript([""])
        coordinator = Coordinator(
            audio,
            transcript,
            bus,
            wake_keyword="你好，小蓝",
        )

        first_turn = await coordinator.start_listening("wake_word")
        await wait_until(lambda: transcript.calls == 1)
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert len(pending) == 1
        assert pending[0].turn_id == first_turn
        assert audio.include_preroll == [False]
        await coordinator.stop()

    asyncio.run(scenario())


def test_stop_forces_uncooperative_task_and_rejects_future_work():
    async def scenario():
        audio = NeverEndingAudio()
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        bus.subscribe(StateChanged, states.append)
        coordinator = Coordinator(
            audio,
            UnexpectedTranscript(),
            bus,
            machine,
            shutdown_timeout=0.01,
        )

        await coordinator.start_listening()
        await audio.started.wait()
        event_count = len(states)
        await coordinator.stop()
        await coordinator.stop()

        assert audio.task_cancelled.is_set()
        assert machine.phase is ConversationPhase.IDLE
        assert machine.turn_id is None
        assert len(states) == event_count
        with pytest.raises(RuntimeError, match="coordinator is stopped"):
            await coordinator.start_listening()
        with pytest.raises(RuntimeError, match="coordinator is stopped"):
            await coordinator.interrupt()

    asyncio.run(scenario())


def test_state_subscriber_failure_does_not_leave_listening_without_task():
    async def scenario():
        audio = ImmediateAudio()
        bus = EventBus()
        machine = ConversationStateMachine()

        def failing_subscriber(event):
            raise ValueError(event.current.value)

        bus.subscribe(StateChanged, failing_subscriber)
        coordinator = Coordinator(
            audio,
            UnexpectedTranscript(),
            bus,
            machine,
        )

        with pytest.raises(ExceptionGroup):
            await coordinator.start_listening()
        await wait_until(lambda: len(audio.calls) == 1)
        await coordinator.stop()

    asyncio.run(scenario())


def test_empty_audio_returns_to_idle_without_calling_transcript_adapter():
    async def scenario():
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        transcripts = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(TranscriptReady, transcripts.append)
        coordinator = Coordinator(
            EmptyAudio(),
            UnexpectedTranscript(),
            bus,
            machine,
        )

        await coordinator.start_listening()
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        assert [event.current for event in states] == [
            ConversationPhase.LISTENING,
            ConversationPhase.IDLE,
        ]
        assert transcripts == []
        await coordinator.stop()

    asyncio.run(scenario())


def test_current_audio_error_is_published_and_returns_to_idle():
    async def scenario():
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        errors = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(RuntimeErrorEvent, errors.append)
        coordinator = Coordinator(
            FailingAudio(),
            UnexpectedTranscript(),
            bus,
            machine,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(errors) == 1)
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        assert len(errors) == 1
        assert errors[0].turn_id == turn_id
        assert errors[0].correlation_id == states[0].correlation_id
        assert errors[0].code == "audio.device"
        assert errors[0].component == "audio"
        assert errors[0].severity is ErrorSeverity.WARNING
        assert errors[0].retryable is True
        assert errors[0].user_action_required is True
        assert errors[0].safe_message == (
            "麦克风设备不可用，请检查系统权限和设备连接"
        )
        assert errors[0].diagnostic_context == {
            "exception_type": "AudioDeviceError"
        }
        assert "microphone disconnected" not in repr(errors[0])
        assert [event.current for event in states[-2:]] == [
            ConversationPhase.RECOVERING,
            ConversationPhase.IDLE,
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_stale_audio_error_after_interrupt_is_discarded():
    async def scenario():
        audio = DelayedFirstAudioFailure()
        bus = EventBus()
        machine = ConversationStateMachine()
        errors = []
        bus.subscribe(RuntimeErrorEvent, errors.append)
        coordinator = Coordinator(
            audio,
            UnexpectedTranscript(),
            bus,
            machine,
        )

        old_turn = await coordinator.start_listening()
        await audio.first_started.wait()
        new_turn = await coordinator.interrupt()
        await audio.second_started.wait()
        audio.release_first.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert old_turn != new_turn
        assert machine.turn_id == new_turn
        assert machine.phase is ConversationPhase.LISTENING
        assert errors == []
        await coordinator.stop()

    asyncio.run(scenario())


def test_current_asr_error_uses_transcription_id_and_returns_to_idle():
    async def scenario():
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        errors = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(RuntimeErrorEvent, errors.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            FailingTranscript(),
            bus,
            machine,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(errors) == 1)
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        transcribing = next(
            event
            for event in states
            if event.current is ConversationPhase.TRANSCRIBING
        )
        assert len(errors) == 1
        assert errors[0].turn_id == turn_id
        assert errors[0].correlation_id == transcribing.correlation_id
        assert errors[0].code == "asr.transcription"
        assert errors[0].component == "asr"
        assert errors[0].severity is ErrorSeverity.WARNING
        assert errors[0].retryable is True
        assert errors[0].user_action_required is False
        assert errors[0].safe_message == "语音识别暂时失败，请重试"
        assert errors[0].diagnostic_context == {
            "exception_type": "AsrTranscriptionError"
        }
        assert "model inference failed" not in repr(errors[0])
        assert [event.current for event in states[-2:]] == [
            ConversationPhase.RECOVERING,
            ConversationPhase.IDLE,
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_stale_asr_error_after_interrupt_is_discarded():
    async def scenario():
        transcript = DelayedFirstTranscriptFailure()
        bus = EventBus()
        machine = ConversationStateMachine()
        errors = []
        bus.subscribe(RuntimeErrorEvent, errors.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
        )

        old_turn = await coordinator.start_listening()
        await transcript.first_started.wait()
        new_turn = await coordinator.interrupt()
        await transcript.second_started.wait()
        transcript.release_first.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert old_turn != new_turn
        assert machine.turn_id == new_turn
        assert machine.phase is ConversationPhase.TRANSCRIBING
        assert errors == []
        await coordinator.stop()

    asyncio.run(scenario())


def test_agent_uses_distinct_operation_id_and_publishes_ordered_deltas():
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        gateway = ScriptedGateway(
            [
                agent_delta("你"),
                agent_delta("好"),
                agent_completed(),
            ]
        )
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        transcripts = []
        deltas = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(TranscriptReady, transcripts.append)
        bus.subscribe(TextDelta, deltas.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=gateway,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: coordinator.response_text == "你好")

        assert [event.text for event in deltas] == ["你", "好"]
        assert all(event.turn_id == turn_id for event in deltas)
        assert len({event.correlation_id for event in deltas}) == 1
        assert deltas[0].correlation_id != transcripts[0].correlation_id
        assert gateway.calls[0][1] == "hello"
        assert machine.phase is ConversationPhase.IDLE
        assert machine.turn_id is None
        await coordinator.stop()

    asyncio.run(scenario())


def test_final_agent_reply_is_archived_once_without_blocking_tts():
    async def scenario():
        transcript = ControlledTranscript(["第一问"])
        archive = RecordingSessionArchive()
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            EventBus(),
            agent_gateway=ScriptedGateway(
                [agent_delta("第一答"), agent_completed()]
            ),
            session_archive=archive,
            speech_synthesizer=RecordingSynthesizer(),
            audio_player=RecordingPlayer(),
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert archive.calls == [(str(turn_id), "第一问", "第一答")]
        assert archive.discarded == []
        await coordinator.stop()

    asyncio.run(scenario())


def test_session_archive_failure_does_not_suppress_final_reply():
    async def scenario():
        transcript = ControlledTranscript(["问题"])
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            EventBus(),
            agent_gateway=ScriptedGateway([agent_delta("回复"), agent_completed()]),
            session_archive=RecordingSessionArchive(RuntimeError("disk")),
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: coordinator.response_text == "回复")
        await asyncio.sleep(0.01)

        assert coordinator.phase is ConversationPhase.IDLE
        await coordinator.stop()

    asyncio.run(scenario())


def test_completed_agent_turn_archives_reply_segments_in_order():
    async def scenario():
        transcript = ControlledTranscript(["长任务"])
        archive = RecordingSessionArchive()
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            EventBus(),
            agent_gateway=ScriptedGateway(
                [
                    agent_item_delta("item-1", "先看环境"),
                    agent_item_delta("item-1", "，再改文档"),
                    agent_item_delta("item-2", "最后提交"),
                    agent_completed(),
                ]
            ),
            session_archive=archive,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert archive.calls == [
            (str(turn_id), "长任务", "先看环境，再改文档最后提交")
        ]
        assert archive.item_calls == [
            (str(turn_id), ("先看环境，再改文档", "最后提交"))
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_cancelled_agent_turn_archives_what_was_already_shown():
    async def scenario():
        transcript = ControlledTranscript(["长任务"])
        gateway = StreamingGateway(
            [
                agent_item_delta("item-1", "先看环境"),
                agent_item_delta("item-2", "再改文档"),
            ]
        )
        archive = RecordingSessionArchive()
        session = SessionContext()
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            EventBus(),
            agent_gateway=gateway,
            session_context=session,
            session_archive=archive,
            shutdown_timeout=0.01,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await gateway.streamed.wait()
        await coordinator.cancel_active_turn()

        assert archive.calls == [
            (str(turn_id), "长任务", "先看环境再改文档", session.session_id)
        ]
        assert archive.item_calls == [(str(turn_id), ("先看环境", "再改文档"))]
        await coordinator.stop()

    asyncio.run(scenario())


def test_cancelled_turn_without_reply_is_not_archived():
    async def scenario():
        transcript = ControlledTranscript(["长任务"])
        gateway = StreamingGateway(())
        archive = RecordingSessionArchive()
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            EventBus(),
            agent_gateway=gateway,
            session_archive=archive,
            shutdown_timeout=0.01,
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await gateway.streamed.wait()
        await coordinator.cancel_active_turn()

        assert archive.calls == []
        assert archive.item_calls == []
        await coordinator.stop()

    asyncio.run(scenario())


def test_interrupted_turn_reaches_the_real_session_archive(tmp_path):
    from core.session_archive import SessionArchiveStore

    async def scenario():
        transcript = ControlledTranscript(["长任务"])
        gateway = StreamingGateway(
            [
                agent_item_delta("item-1", "先看环境"),
                agent_item_delta("item-2", "再改文档"),
            ]
        )
        archive = SessionArchiveStore(tmp_path / "sessions.db")
        session = SessionContext()
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            EventBus(),
            agent_gateway=gateway,
            session_context=session,
            session_archive=archive,
            shutdown_timeout=0.01,
        )
        try:
            await coordinator.start_listening()
            await wait_until(lambda: len(transcript.calls) == 1)
            transcript.releases[0].set()
            await gateway.streamed.wait()
            await coordinator.cancel_active_turn()

            record = archive.list_session_turns(session.session_id)[0]
            assert record.user_text == "长任务"
            assert record.assistant_text == "先看环境再改文档"
            assert [item["text"] for item in record.assistant_items] == [
                "先看环境",
                "再改文档",
            ]
        finally:
            await coordinator.stop()
            archive.close()

    asyncio.run(scenario())


def test_failed_agent_turn_archives_what_was_already_shown():
    async def scenario():
        transcript = ControlledTranscript(["长任务"])
        gateway = FailingGateway(
            [agent_item_delta("item-1", "先看环境"), agent_item_delta("item-2", "再改文档")],
            RuntimeError("agent crashed"),
        )
        archive = RecordingSessionArchive()
        bus = EventBus()
        errors = []
        ready = asyncio.Event()
        bus.subscribe(RuntimeErrorEvent, lambda event: (errors.append(event), ready.set()))
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            agent_gateway=gateway,
            session_archive=archive,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await ready.wait()

        assert archive.calls == [(str(turn_id), "长任务", "先看环境再改文档")]
        assert archive.item_calls == [(str(turn_id), ("先看环境", "再改文档"))]
        await coordinator.stop()

    asyncio.run(scenario())


def test_agent_approval_request_is_forwarded_to_the_ui():
    async def scenario():
        transcript = ControlledTranscript(["open calculator"])
        gateway = ScriptedGateway(
            [
                SimpleNamespace(
                    type=AgentEventType.APPROVAL_REQUEST,
                    payload={
                        "request_id": "approval-1",
                        "message": "是否打开计算器？",
                        "options": [{"label": "允许", "value": "accept"}],
                    },
                ),
                agent_delta("已打开"),
                agent_completed(),
            ]
        )
        bus = EventBus()
        machine = ConversationStateMachine()
        approvals = []
        bus.subscribe(AgentApprovalRequested, approvals.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=gateway,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: len(approvals) == 1)

        assert approvals[0].turn_id == turn_id
        assert approvals[0].approval_id == "approval-1"
        assert approvals[0].summary == "是否打开计算器？"
        assert approvals[0].options == ({"label": "允许", "value": "accept"},)
        await coordinator.stop()

    asyncio.run(scenario())


def test_agent_error_is_published_and_returns_to_idle():
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        gateway = ScriptedGateway(
            [SimpleNamespace(type=AgentEventType.ERROR, payload={"message": "failed"})]
        )
        bus = EventBus()
        machine = ConversationStateMachine()
        errors = []
        states = []
        bus.subscribe(RuntimeErrorEvent, errors.append)
        bus.subscribe(StateChanged, states.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=gateway,
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: len(errors) == 1)
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        assert len(errors) == 1
        assert errors[0].error_code == "runtime.error"
        assert errors[0].component == "runtime"
        assert errors[0].severity is ErrorSeverity.ERROR
        assert errors[0].retryable is False
        thinking = next(
            event
            for event in states
            if event.current is ConversationPhase.THINKING
        )
        assert errors[0].correlation_id != thinking.correlation_id
        assert states[-1].correlation_id == errors[0].correlation_id
        assert [event.current for event in states[-2:]] == [
            ConversationPhase.RECOVERING,
            ConversationPhase.IDLE,
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_interrupt_discards_stale_agent_stream_events():
    async def scenario():
        transcript = ControlledTranscript(["old", "new"])
        gateway = DelayedFirstGateway()
        bus = EventBus()
        machine = ConversationStateMachine()
        deltas = []
        bus.subscribe(TextDelta, deltas.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=gateway,
        )

        old_turn = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await gateway.first_started.wait()

        new_turn = await coordinator.interrupt()
        await wait_until(lambda: len(transcript.calls) == 2)
        transcript.releases[1].set()
        await gateway.second_started.wait()
        gateway.release_first.set()
        await wait_until(lambda: coordinator.response_text == "fresh")

        assert old_turn != new_turn
        assert [(event.turn_id, event.text) for event in deltas] == [
            (new_turn, "fresh")
        ]
        assert machine.turn_id is None
        assert machine.phase is ConversationPhase.IDLE
        await coordinator.stop()

    asyncio.run(scenario())


def test_interrupt_never_archives_stale_agent_completion():
    async def scenario():
        transcript = ControlledTranscript(["旧问题", "新问题"])
        gateway = DelayedFirstGateway()
        session = SessionContext()
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            EventBus(),
            agent_gateway=gateway,
            session_context=session,
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await gateway.first_started.wait()

        await coordinator.interrupt()
        await wait_until(lambda: len(transcript.calls) == 2)
        transcript.releases[1].set()
        await wait_until(lambda: coordinator.response_text == "fresh")
        gateway.release_first.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert session.build_history() == (
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "fresh"},
        )
        await coordinator.stop()

    asyncio.run(scenario())


def test_tts_synthesizes_sentences_and_completes_turn_in_order():
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        gateway = ScriptedGateway(
            [
                agent_delta("第一句。第二"),
                agent_delta("句！尾巴"),
                agent_completed(),
            ]
        )
        synthesizer = RecordingSynthesizer()
        player = RecordingPlayer()
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        deltas = []
        speaks = []
        bus.subscribe(StateChanged, states.append)
        bus.subscribe(TextDelta, deltas.append)
        bus.subscribe(SpeakRequested, speaks.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=gateway,
            speech_synthesizer=synthesizer,
            audio_player=player,
        )

        turn_id = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        assert [call[0] for call in synthesizer.calls] == [
            "第一句。第二句！尾巴"
        ]
        assert [call[0].data for call in player.calls] == [
            "audio:第一句。第二句！尾巴".encode()
        ]
        assert [event.text for event in speaks] == [
            "第一句。第二句！尾巴"
        ]
        assert all(event.turn_id == turn_id for event in speaks)
        synthesis = next(
            event
            for event in states
            if event.current is ConversationPhase.SYNTHESIZING
        )
        speaking = next(
            event
            for event in states
            if event.current is ConversationPhase.SPEAKING
        )
        assert synthesis.correlation_id == speaking.correlation_id
        assert synthesis.correlation_id == speaks[0].correlation_id
        assert synthesis.correlation_id != deltas[0].correlation_id
        assert [event.current for event in states[-3:]] == [
            ConversationPhase.SYNTHESIZING,
            ConversationPhase.SPEAKING,
            ConversationPhase.IDLE,
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_disabled_speech_skips_tts_and_completes_reply():
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        synthesizer = RecordingSynthesizer()
        player = RecordingPlayer()
        bus = EventBus()
        states = []
        bus.subscribe(StateChanged, states.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            agent_gateway=ScriptedGateway(
                [agent_delta("静音回复"), agent_completed()]
            ),
            speech_synthesizer=synthesizer,
            audio_player=player,
            speech_enabled=False,
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: coordinator.phase is ConversationPhase.IDLE)

        assert coordinator.response_text == "静音回复"
        assert synthesizer.calls == []
        assert player.calls == []
        assert states[-1].current is ConversationPhase.IDLE
        await coordinator.stop()

    asyncio.run(scenario())


def test_disabling_speech_cancels_playback_and_allows_later_reenable():
    async def scenario():
        transcript = ControlledTranscript(["第一轮", "第二轮"])
        player = CooperativeBlockingPlayer()
        bus = EventBus()
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            agent_gateway=ScriptedGateway(
                [agent_delta("保留文字"), agent_completed()]
            ),
            speech_synthesizer=RecordingSynthesizer(),
            audio_player=player,
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await player.started.wait()
        await coordinator.set_speech_enabled(False)
        await player.cancelled.wait()

        assert coordinator.phase is ConversationPhase.IDLE
        assert coordinator.response_text == "保留文字"

        await coordinator.set_speech_enabled(True)
        assert coordinator.speech_enabled is True
        await coordinator.stop()

    asyncio.run(scenario())


def test_tts_converts_completed_markdown_without_changing_text_events():
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        gateway = ScriptedGateway(
            [
                agent_delta("**重要"),
                agent_delta("**：[说明](https://example.com)。版本是 3.12.14。"),
                agent_completed(),
            ]
        )
        synthesizer = RecordingSynthesizer()
        bus = EventBus()
        machine = ConversationStateMachine()
        deltas = []
        speaks = []
        bus.subscribe(TextDelta, deltas.append)
        bus.subscribe(SpeakRequested, speaks.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=gateway,
            speech_synthesizer=synthesizer,
            audio_player=RecordingPlayer(),
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        assert "".join(event.text for event in deltas) == (
            "**重要**：[说明](https://example.com)。版本是 3.12.14。"
        )
        assert coordinator.response_text == (
            "**重要**：[说明](https://example.com)。版本是 3.12.14。"
        )
        assert [call[0] for call in synthesizer.calls] == [
            "重要：说明。版本是 3.12.14。"
        ]
        assert [event.text for event in speaks] == [
            "重要：说明。版本是 3.12.14。"
        ]
        await coordinator.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("synth_error", "play_error", "expected_code"),
    [
        (TtsSynthesisError("failed"), None, "tts.synthesis"),
        (None, TtsPlaybackError("failed"), "tts.playback"),
    ],
)
def test_tts_errors_are_published_and_return_completed_reply_to_idle(
    synth_error,
    play_error,
    expected_code,
):
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        bus = EventBus()
        machine = ConversationStateMachine()
        states = []
        errors = []
        error_ready = asyncio.Event()
        bus.subscribe(StateChanged, states.append)

        def record_error(event):
            errors.append(event)
            error_ready.set()

        bus.subscribe(RuntimeErrorEvent, record_error)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=ScriptedGateway(
                [agent_delta("回复"), agent_completed()]
            ),
            speech_synthesizer=RecordingSynthesizer(synth_error),
            audio_player=RecordingPlayer(play_error),
        )

        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await asyncio.wait_for(error_ready.wait(), timeout=1)
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)

        assert len(errors) == 1
        assert errors[0].code == expected_code
        assert errors[0].component == "tts"
        assert errors[0].severity is ErrorSeverity.ERROR
        assert errors[0].retryable is False
        assert errors[0].user_action_required is False
        assert errors[0].diagnostic_context == {
            "exception_type": type(synth_error or play_error).__name__
        }
        assert "failed" not in repr(errors[0])
        tts_state = next(
            event
            for event in states
            if event.current
            in {ConversationPhase.SYNTHESIZING, ConversationPhase.SPEAKING}
            and event.correlation_id == errors[0].correlation_id
        )
        assert tts_state.turn_id == errors[0].turn_id
        assert states[-1].current is ConversationPhase.IDLE
        await coordinator.stop()

    asyncio.run(scenario())


def test_interrupt_discards_stale_synthesis_before_playback():
    async def scenario():
        transcript = ControlledTranscript(["old", "new"])
        synthesizer = DelayedFirstSynthesizer()
        player = RecordingPlayer()
        bus = EventBus()
        machine = ConversationStateMachine()
        speaks = []
        bus.subscribe(SpeakRequested, speaks.append)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            machine,
            agent_gateway=ScriptedGateway(
                [agent_delta("回复。"), agent_completed()]
            ),
            speech_synthesizer=synthesizer,
            audio_player=player,
        )

        old_turn = await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await synthesizer.first_started.wait()

        new_turn = await coordinator.interrupt()
        await wait_until(lambda: len(transcript.calls) == 2)
        transcript.releases[1].set()
        await wait_until(lambda: machine.phase is ConversationPhase.IDLE)
        synthesizer.release_first.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert old_turn != new_turn
        assert len(player.calls) == 1
        assert [(event.turn_id, event.text) for event in speaks] == [
            (new_turn, "回复。")
        ]
        await coordinator.stop()

    asyncio.run(scenario())


def test_turn_duration_expires_blocked_recording():
    async def scenario():
        audio = NeverEndingAudio()
        bus = EventBus()
        errors = []
        ready = asyncio.Event()

        def record_error(event):
            errors.append(event)
            ready.set()

        bus.subscribe(RuntimeErrorEvent, record_error)
        coordinator = Coordinator(
            audio,
            UnexpectedTranscript(),
            bus,
            max_turn_duration=0.02,
        )
        await coordinator.start_listening()
        await audio.started.wait()
        try:
            await asyncio.wait_for(ready.wait(), timeout=0.5)
        finally:
            await coordinator.stop()

        assert errors[-1].error_code == "runtime.budget"
        assert audio.task_cancelled.is_set()

    asyncio.run(scenario())


def test_turn_duration_expires_blocked_transcription():
    async def scenario():
        transcript = BlockingTranscript()
        bus = EventBus()
        errors = []
        ready = asyncio.Event()

        def record_error(event):
            errors.append(event)
            ready.set()

        bus.subscribe(RuntimeErrorEvent, record_error)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            max_turn_duration=0.02,
        )
        await coordinator.start_listening()
        await transcript.started.wait()
        try:
            await asyncio.wait_for(ready.wait(), timeout=0.5)
        finally:
            await coordinator.stop()

        assert errors[-1].error_code == "runtime.budget"
        assert transcript.cancelled.is_set()

    asyncio.run(scenario())


def test_turn_duration_expires_blocked_agent_stream():
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        gateway = BlockingGateway()
        bus = EventBus()
        errors = []
        ready = asyncio.Event()

        def record_error(event):
            errors.append(event)
            ready.set()

        bus.subscribe(RuntimeErrorEvent, record_error)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            agent_gateway=gateway,
            max_turn_duration=0.02,
        )
        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await gateway.started.wait()
        try:
            await asyncio.wait_for(ready.wait(), timeout=0.5)
        finally:
            await coordinator.stop()

        assert errors[-1].error_code == "runtime.budget"
        assert gateway.cancelled.is_set()

    asyncio.run(scenario())


def test_turn_duration_expires_blocked_synthesis():
    async def scenario():
        transcript = ControlledTranscript(["hello"])
        synthesizer = BlockingSynthesizer()
        bus = EventBus()
        errors = []
        ready = asyncio.Event()

        def record_error(event):
            errors.append(event)
            ready.set()

        bus.subscribe(RuntimeErrorEvent, record_error)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            agent_gateway=ScriptedGateway(
                [agent_delta("回复"), agent_completed()]
            ),
            speech_synthesizer=synthesizer,
            audio_player=RecordingPlayer(),
            max_turn_duration=0.02,
        )
        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        await synthesizer.started.wait()
        try:
            await asyncio.wait_for(ready.wait(), timeout=0.5)
        finally:
            await coordinator.stop()

        assert errors[-1].error_code == "runtime.budget"
        assert synthesizer.cancelled.is_set()

    asyncio.run(scenario())


def test_turn_duration_expires_blocked_playback():
    async def scenario():
        elapsed = [0.0]

        class DeadlineSynthesizer(RecordingSynthesizer):
            async def synthesize(self, text, token):
                audio = await super().synthesize(text, token)
                # 在播放开始前才消耗预算，避免慢机器在转写阶段提前超时。
                elapsed[0] = 0.98
                return audio

        transcript = ControlledTranscript(["hello"])
        player = BlockingPlayer()
        bus = EventBus()
        errors = []
        ready = asyncio.Event()

        def record_error(event):
            errors.append(event)
            ready.set()

        bus.subscribe(RuntimeErrorEvent, record_error)
        coordinator = Coordinator(
            ImmediateAudio(),
            transcript,
            bus,
            agent_gateway=ScriptedGateway(
                [agent_delta("回复"), agent_completed()]
            ),
            speech_synthesizer=DeadlineSynthesizer(),
            audio_player=player,
            max_turn_duration=1.0,
            clock=lambda: elapsed[0],
        )
        await coordinator.start_listening()
        await wait_until(lambda: len(transcript.calls) == 1)
        transcript.releases[0].set()
        try:
            await asyncio.wait_for(player.started.wait(), timeout=0.5)
            await asyncio.wait_for(ready.wait(), timeout=0.5)
        finally:
            await coordinator.stop()

        assert errors[-1].error_code == "runtime.budget"
        assert player.cancelled.is_set()

    asyncio.run(scenario())
