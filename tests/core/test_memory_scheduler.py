import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.event_bus import EventBus
from core.events import (
    RuntimeErrorEvent,
    SpeakRequested,
    StateChanged,
    TextDelta,
    TranscriptReady,
)
from core.llm import LlmCompleted, LlmNetworkError, LlmTextDelta, LlmToolCall
from core.memory import MemoryStore
from core.memory_extraction import MemoryExtractor
from core.session_archive import SessionArchiveStore

NOW = datetime(2026, 9, 6, tzinfo=UTC)


class Provider:
    def __init__(self):
        self.requests = []
        self.failures = 0
        self.plain_text = False
        self.started = None
        self.release = None
        self.fact_overrides = {}

    async def stream(self, request, token):
        self.requests.append(request)
        if self.started:
            self.started.set()
        if self.release:
            await self.release.wait()
        if self.failures:
            self.failures -= 1
            raise LlmNetworkError("synthetic network failure")
        if self.plain_text:
            yield LlmTextDelta("not structured")
        else:
            payload = json.loads(request.input_text)
            source = payload["turns"][-1]
            facts = [{"source_turn_id": source["source_turn_id"], "category": "preference",
                      "content": "用户喜欢猫", "fact_key": "user.pet", "value": "猫",
                      "quote": "我喜欢猫", "confidence": .9, "stability": "stable",
                      "sensitivity": "normal", "explicit_update": False, "keywords": []}]
            facts[0].update(self.fact_overrides)
            summary = ({"topic": "宠物偏好", "decisions": ["喜欢猫"], "unfinished_items": []}
                       if payload["include_summary"] else None)
            yield LlmToolCall("analysis", "submit_memory_analysis", {"facts": facts, "summary": summary})
        yield LlmCompleted("test", 1, 1)


class Setup:
    def __init__(self, tmp_path):
        from core.memory_jobs import MemoryJobStore
        from core.memory_scheduler import MemoryScheduler

        self.current = [NOW]
        self.busy = False
        self.path = tmp_path / "scheduler.db"
        clock = lambda: self.current[0]
        self.archive = SessionArchiveStore(self.path, clock=clock)
        self.memory = MemoryStore(self.path, clock=clock)
        self.jobs = MemoryJobStore(self.path, clock=clock)
        self.provider = Provider()
        self.events = EventBus()
        self.scheduler = MemoryScheduler(MemoryExtractor(self.provider), self.memory, self.archive,
                                         self.jobs, self.events, clock=clock,
                                         foreground_busy=lambda: self.busy)
        self.session = str(uuid4())

    def enqueue(self, *, session_id=None, text="我喜欢猫"):
        turn_id = str(uuid4())
        self.archive.archive_turn(turn_id, text, "知道了", session_id or self.session)
        self.scheduler.enqueue(session_id or self.session, turn_id)
        return turn_id

    def advance(self, seconds):
        self.current[0] += timedelta(seconds=seconds)

    def close(self):
        self.jobs.close()
        self.memory.close()
        self.archive.close()


def test_scheduler_debounces_respects_foreground_and_emits_only_memory_events(tmp_path):
    async def scenario():
        from core.events import MemoryChanged

        setup = Setup(tmp_path)
        try:
            changes, errors = [], []
            setup.events.subscribe(MemoryChanged, changes.append)
            setup.events.subscribe(RuntimeErrorEvent, errors.append)
            for event_type in (SpeakRequested, StateChanged, TextDelta, TranscriptReady):
                setup.events.subscribe(event_type, errors.append)
            setup.enqueue()
            setup.advance(4)
            assert not await setup.scheduler.run_due()
            setup.advance(1)
            setup.busy = True
            assert not await setup.scheduler.run_due()
            setup.busy = False
            assert await setup.scheduler.run_due()
            assert len(setup.memory.list_all()) == 1
            assert changes[0].session_id == setup.session
            assert errors == []
            assert setup.scheduler.status()["status"] == "idle"
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_scheduler_retries_only_once_with_sixty_second_spacing(tmp_path):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            setup.provider.failures = 3
            setup.enqueue()
            setup.advance(5)
            await setup.scheduler.run_due()
            assert len(setup.provider.requests) == 1
            setup.advance(59)
            assert not await setup.scheduler.run_due()
            setup.advance(1)
            await setup.scheduler.run_due()
            setup.advance(100)
            assert not await setup.scheduler.run_due()
            assert len(setup.provider.requests) == 2
            assert setup.memory.list_all() == ()
            assert setup.scheduler.status()["status"] == "failed"
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_scheduler_preserves_origin_session_and_throttles_next_request(tmp_path):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            setup.enqueue()
            setup.advance(5)
            assert await setup.scheduler.run_due()
            other = str(uuid4())
            setup.enqueue(session_id=other)
            setup.advance(29)
            assert not await setup.scheduler.run_due()
            setup.advance(1)
            assert await setup.scheduler.run_due()
            assert len(setup.provider.requests) == 2
            assert setup.memory.list_all()[0].session_id == setup.session
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["delete", "disable", "clear", "invalidate"])
def test_scheduler_does_not_commit_after_invalidation_or_deleted_sources(tmp_path, action):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            setup.provider.started, setup.provider.release = asyncio.Event(), asyncio.Event()
            setup.enqueue()
            setup.advance(5)
            task = asyncio.create_task(setup.scheduler.run_due())
            await setup.provider.started.wait()
            assert not await setup.scheduler.run_due()
            if action == "delete":
                setup.archive.discard_session(setup.session)
            elif action == "clear":
                setup.archive.clear_all()
            elif action == "invalidate":
                setup.scheduler.invalidate()
            else:
                setup.scheduler.configure(False)
            setup.provider.release.set()
            await task
            assert setup.memory.list_all() == ()
            assert setup.archive.list_summaries() == ()
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_scheduler_protocol_failure_pauses_extra_requests_without_frontend_error(tmp_path):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            setup.provider.plain_text = True
            setup.enqueue()
            setup.advance(5)
            await setup.scheduler.run_due()
            setup.enqueue()
            setup.advance(100)
            assert not await setup.scheduler.run_due()
            assert len(setup.provider.requests) == 1
            assert setup.scheduler.status()["message"]
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_four_unsummarized_turns_generate_summary_and_mark_real_coverage(tmp_path):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            ids = [setup.enqueue() for _ in range(4)]
            setup.advance(5)
            assert await setup.scheduler.run_due()
            summaries = setup.archive.list_summaries(setup.session)
            assert summaries[0].source_turn_ids == tuple(ids)
            assert summaries[0].decisions == ("喜欢猫",)
            assert setup.jobs.list_unsummarized(setup.session) == ()
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_stop_waits_for_entire_active_pipeline_before_stores_can_close(tmp_path):
    async def scenario():
        setup = Setup(tmp_path)
        setup.provider.started, setup.provider.release = asyncio.Event(), asyncio.Event()
        setup.enqueue()
        setup.advance(5)
        running = asyncio.create_task(setup.scheduler.run_due())
        await setup.provider.started.wait()
        await setup.scheduler.stop()
        try:
            assert running.done()
            assert setup.scheduler.status()["status"] != "processing"
        finally:
            setup.provider.release.set()
            await running
            setup.close()

    asyncio.run(scenario())


def test_scheduler_timeout_is_local_and_uses_thirty_second_production_limit(tmp_path, monkeypatch):
    async def scenario():
        import core.memory_scheduler as module

        assert module.REQUEST_TIMEOUT == 30
        monkeypatch.setattr(module, "REQUEST_TIMEOUT", .01)
        setup = Setup(tmp_path)
        try:
            setup.provider.release = asyncio.Event()
            setup.enqueue()
            setup.advance(5)
            assert not await setup.scheduler.run_due()
            assert setup.scheduler.status()["status"] == "failed"
            assert setup.memory.list_all() == ()
            assert setup.jobs.list_jobs()[0].attempts == 1
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_summary_includes_earlier_extracted_turns_without_remarking_wrong_job(tmp_path):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            ids = []
            for _ in range(4):
                ids.append(setup.enqueue())
                setup.advance(30)
                assert await setup.scheduler.run_due()
            assert setup.archive.list_summaries()[0].source_turn_ids == tuple(ids)
            assert setup.jobs.list_unsummarized(setup.session) == ()
            assert setup.scheduler.status()["status"] == "idle"
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("long_user", [False, True])
def test_oversized_old_turn_does_not_block_later_short_chat(tmp_path, long_user):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            long_id = str(uuid4())
            setup.archive.archive_turn(long_id, "我喜欢猫" + "x" * 4000 if long_user else "我喜欢猫",
                                       "x" * (9000 if long_user else 13000), setup.session)
            setup.scheduler.enqueue(setup.session, long_id)
            setup.enqueue()
            setup.advance(5)
            assert not await setup.scheduler.run_due()
            setup.advance(30)
            assert await setup.scheduler.run_due()
            assert len(setup.provider.requests) == 1
            assert len(setup.memory.list_all()) == 1
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("overrides,expected", [
    ({"stability": "temporary"}, None),
    ({"sensitivity": "personal"}, None),
    ({"sensitivity": "sensitive"}, None),
    ({"confidence": .6}, "candidate"),
])
def test_legal_nonautomatic_facts_finish_job_without_frontend_events(tmp_path, overrides, expected):
    async def scenario():
        setup = Setup(tmp_path)
        try:
            frontend = []
            for event_type in (RuntimeErrorEvent, SpeakRequested, StateChanged, TextDelta, TranscriptReady):
                setup.events.subscribe(event_type, frontend.append)
            setup.provider.fact_overrides = overrides
            setup.enqueue()
            setup.advance(5)
            assert await setup.scheduler.run_due()
            records = setup.memory.list_all()
            assert [item.status.value for item in records] == ([] if expected is None else [expected])
            assert setup.scheduler.status()["status"] == "idle"
            assert setup.jobs.list_jobs()[0].status == "completed"
            assert frontend == []
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_production_start_recovers_interrupted_job_and_runs_loop(tmp_path):
    async def scenario():
        from core.events import MemoryChanged

        setup = Setup(tmp_path)
        try:
            setup.enqueue()
            setup.advance(5)
            assert setup.jobs.claim_due().attempts == 1
            changed = asyncio.Event()
            setup.events.subscribe(MemoryChanged, lambda event: changed.set())
            await setup.scheduler.start()
            setup.advance(60)
            await asyncio.wait_for(changed.wait(), timeout=2)
            assert len(setup.memory.list_all()) == 1
            assert setup.jobs.list_jobs()[0].attempts == 2
            assert len(setup.provider.requests) == 1
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_startup_and_periodic_cleanup_remove_expired_bodies_and_terminal_jobs(tmp_path):
    async def scenario():
        import sqlite3

        setup = Setup(tmp_path)
        try:
            setup.enqueue()
            setup.advance(5)
            assert await setup.scheduler.run_due()
            setup.advance(8 * 86400)
            await setup.scheduler.start()
            with sqlite3.connect(setup.path) as connection:
                assert connection.execute("SELECT COUNT(*) FROM session_turns").fetchone()[0] == 0
            assert setup.jobs.list_jobs() == ()
            setup.enqueue()
            setup.advance(5)
            assert await setup.scheduler.run_due()
            setup.advance(594)
            await setup.scheduler.run_due()
            assert len(setup.jobs.list_jobs()) == 1
            setup.advance(1)
            await setup.scheduler.run_due()
            assert setup.jobs.list_jobs() == ()
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())


def test_disabled_scheduler_without_extractor_still_runs_expiration_cleanup(tmp_path):
    async def scenario():
        current = [NOW]
        clock = lambda: current[0]
        path = tmp_path / "cleanup-only.db"
        archive = SessionArchiveStore(path, retention_days=1, clock=clock)
        memory = MemoryStore(path, clock=clock)
        from core.memory_jobs import MemoryJobStore
        from core.memory_scheduler import MemoryScheduler

        jobs = MemoryJobStore(path, clock=clock)
        events = EventBus()
        turn_id = str(uuid4())
        session_id = archive.archive_turn(turn_id, "过期内容", "回复").session_id
        scheduler = MemoryScheduler(
            None,
            memory,
            archive,
            jobs,
            events,
            clock=clock,
            enabled=False,
        )
        try:
            await scheduler.start()
            current[0] += timedelta(days=2, seconds=600)
            assert await scheduler.run_due() is False
            assert archive.list_turns() == ()
            assert scheduler.status()["status"] == "disabled"
            assert session_id
        finally:
            await scheduler.stop()
            jobs.close()
            archive.close()
            memory.close()

    asyncio.run(scenario())


def test_undo_while_background_result_is_pending_prevents_same_source_recreation(tmp_path):
    async def scenario():
        from core.events import MemoryChanged
        from core.memory_facts import FactEvidence, FactProposal

        setup = Setup(tmp_path)
        try:
            source = setup.enqueue()
            change = setup.memory.save_fact(
                FactProposal("preference", "用户喜欢猫", "user.pet", "猫", "我喜欢猫"),
                FactEvidence(source, setup.session, "我喜欢猫"),
            )
            setup.provider.started, setup.provider.release = asyncio.Event(), asyncio.Event()
            events = []
            setup.events.subscribe(MemoryChanged, events.append)
            setup.advance(5)
            task = asyncio.create_task(setup.scheduler.run_due())
            await asyncio.wait_for(setup.provider.started.wait(), timeout=2)
            assert setup.memory.undo(change.id)
            setup.provider.release.set()
            assert await task
            assert setup.memory.list_all() == ()
            assert setup.memory.list_changes()[0].undone
            assert events == []
        finally:
            await setup.scheduler.stop()
            setup.close()

    asyncio.run(scenario())
