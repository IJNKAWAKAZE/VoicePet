"""验证按会话并行的协调器注册表把语音和桌宠留给前台会话"""

import asyncio
from uuid import uuid4

from core.events import ConversationPhase
from core.session_runtime import SessionCoordinatorPool


class FakeCoordinator:
    """记录注册表装配参数的最小协调器"""

    def __init__(
        self,
        session_id,
        context,
        memory_context,
        *,
        speech_enabled,
        manual_input_speech_enabled,
    ):
        self.session_id = session_id
        self.context = context
        self.memory_context = memory_context
        self.speech_enabled = speech_enabled
        self.manual_input_speech_enabled = manual_input_speech_enabled
        self.phase = ConversationPhase.IDLE
        self.submitted = []
        self.cancelled = 0
        self.stopped = 0
        self.notices = []
        self.listen_sources = []
        self.approved = 0
        self.rejected = 0
        self._pending_memory = None

    async def submit_text(self, text, attachments=()):
        self.submitted.append(text)
        return text

    async def cancel_active_turn(self):
        self.cancelled += 1

    async def stop(self):
        self.stopped += 1

    async def speak_notice(self, text):
        self.notices.append(text)

    async def capture_manual_transcript(self):
        return f"语音-{self.session_id}"

    async def start_listening(self, source="click"):
        self.listen_sources.append(source)

    async def interrupt(self, source="click"):
        self.listen_sources.append(source)

    async def set_speech_enabled(self, enabled):
        self.speech_enabled = enabled

    async def set_manual_input_speech_enabled(self, enabled):
        self.manual_input_speech_enabled = enabled

    async def approve_pending(self):
        self.approved += 1

    async def reject_pending(self):
        self.rejected += 1


class FakeMemoryContext:
    def __init__(self):
        self.configured = []

    def configure(self, enabled):
        self.configured.append(enabled)


def build_pool(**settings):
    created = {}
    memories = []

    def builder(
        session_id,
        context,
        memory_context,
        *,
        speech_enabled,
        manual_input_speech_enabled,
    ):
        created[session_id] = FakeCoordinator(
            session_id,
            context,
            memory_context,
            speech_enabled=speech_enabled,
            manual_input_speech_enabled=manual_input_speech_enabled,
        )
        return created[session_id]

    def memory_builder(context):
        del context
        memories.append(FakeMemoryContext())
        return memories[-1]

    pool = SessionCoordinatorPool(
        builder, memory_context_builder=memory_builder, **settings
    )
    return pool, created, memories


def test_each_session_keeps_its_own_context_and_coordinator():
    pool, created, _memories = build_pool()
    foreground = pool.session_id
    other = str(uuid4())
    pool.coordinator_for(foreground)

    pool.add_turn("前台问题", "前台回答")
    pool.activate(other, (("旧问题", "旧回答"),))

    assert pool.session_id == other
    assert pool.build_history()[-1]["content"] == "旧回答"
    assert pool.coordinator is created[other]
    assert created[foreground] is not created[other]
    assert created[foreground].context is not created[other].context
    assert created[foreground].context.build_history()[-1]["content"] == "前台回答"


def test_foreground_follows_activation_and_new_session():
    pool, _created, _memories = build_pool()
    first = pool.session_id
    second = str(uuid4())

    assert pool.is_foreground(first) is True
    assert pool.is_foreground(second) is False

    pool.activate(second, ())
    assert pool.is_foreground(second) is True
    assert pool.is_foreground(first) is False

    pool.clear()
    assert pool.session_id not in {first, second}
    assert pool.is_foreground(second) is False


def test_discard_and_reset_release_in_process_state():
    pool, _created, _memories = build_pool()
    first = pool.session_id
    second = str(uuid4())
    pool.coordinator_for(second)

    assert set(pool.session_ids()) == {first, second}

    pool.discard(second)
    assert pool.session_ids() == (first,)

    pool.activate(second, ())
    pool.reset()
    assert pool.session_ids() == (pool.session_id,)
    assert pool.session_id not in {first, second}


def test_listening_releases_other_sessions_recordings():
    pool, created, _memories = build_pool()
    first = pool.session_id
    second = str(uuid4())
    third = str(uuid4())
    pool.coordinator_for(first)
    pool.coordinator_for(second)
    pool.activate(second, ())
    pool.activate(third, ())
    created[first].phase = ConversationPhase.LISTENING
    created[second].phase = ConversationPhase.TRANSCRIBING

    asyncio.run(pool.start_listening("click"))

    assert created[first].cancelled == 1
    assert created[second].cancelled == 1
    assert created[third].cancelled == 0
    assert created[third].listen_sources == ["click"]


def test_speech_settings_reach_every_session():
    pool, created, _memories = build_pool(
        speech_enabled=False, manual_input_speech_enabled=True
    )
    first = pool.session_id
    second = str(uuid4())
    pool.coordinator_for(first)
    pool.activate(second, ())

    assert created[second].speech_enabled is False
    assert created[second].manual_input_speech_enabled is True

    asyncio.run(pool.set_speech_enabled(True))
    asyncio.run(pool.set_manual_input_speech_enabled(False))

    assert [created[first].speech_enabled, created[second].speech_enabled] == [
        True,
        True,
    ]
    assert [
        created[first].manual_input_speech_enabled,
        created[second].manual_input_speech_enabled,
    ] == [False, False]


def test_memory_switch_reaches_existing_and_new_sessions():
    pool, _created, memories = build_pool()
    pool.coordinator_for(pool.session_id)

    pool.configure(False)
    # 新会话按当前开关装配，因此先记录一次默认值
    assert memories[0].configured == [True, False]

    pool.activate(str(uuid4()), ())
    assert memories[1].configured == [False]

    pool.configure(True)
    assert memories[0].configured == [True, False, True]
    assert memories[1].configured == [False, True]


def test_submit_and_cancel_target_the_foreground_session():
    pool, created, _memories = build_pool()
    first = pool.session_id
    second = str(uuid4())

    asyncio.run(pool.submit_text("前台问题"))
    assert created[first].submitted == ["前台问题"]

    pool.activate(second, ())
    asyncio.run(pool.cancel_active_turn())
    assert created[second].cancelled == 1

    asyncio.run(pool.cancel_active_turn(first))
    assert created[first].cancelled == 1

    asyncio.run(pool.stop())
    assert [created[first].stopped, created[second].stopped] == [1, 1]


def test_pending_approval_follows_the_session_holding_it():
    pool, created, _memories = build_pool()
    first = pool.session_id
    second = str(uuid4())
    pool.coordinator_for(first)
    pool.activate(second, ())
    created[first]._pending_memory = object()

    asyncio.run(pool.approve_pending())
    assert created[first].approved == 1
    assert created[second].approved == 0

    created[first]._pending_memory = None
    asyncio.run(pool.reject_pending())
    assert created[second].rejected == 1
