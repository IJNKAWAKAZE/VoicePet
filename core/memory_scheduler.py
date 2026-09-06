"""让前台优先的后台记忆整理调度器"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .cancellation import CancellationSource, CancelledError
from .event_bus import EventBus
from .events import MemoryChanged, MemoryMaintenanceChanged
from .llm import LlmProtocolError
from .memory import MemoryRecord, MemoryStore
from .memory_extraction import MemoryExtractor, MemoryInputTooLargeError
from .memory_jobs import MemoryJobStore
from .session_archive import SessionArchiveStore

REQUEST_TIMEOUT = 30
CLEANUP_INTERVAL = 600
SUMMARY_TURN_THRESHOLD = 4
SUMMARY_CHAR_THRESHOLD = 3000


class MemoryScheduler:
    """由持久化任务控制限流、重试及来源失效，模型失败留在后台"""

    def __init__(
        self, extractor: MemoryExtractor | None, memory_store: MemoryStore, archive: SessionArchiveStore,
        jobs: MemoryJobStore, event_bus: EventBus, *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        foreground_busy: Callable[[], bool] = lambda: False, enabled: bool = True,
    ) -> None:
        self._extractor = extractor
        self._memory = memory_store
        self._archive = archive
        self._jobs = jobs
        self._events = event_bus
        self._clock = clock
        self._foreground_busy = foreground_busy
        self._enabled = enabled and extractor is not None
        self._paused = False
        self._generation = 0
        self._busy = False
        self._loop_task: asyncio.Task | None = None
        self._request_task: asyncio.Task | None = None
        self._active_run_task: asyncio.Task | None = None
        self._cancellation: CancellationSource | None = None
        self._last_cleanup: datetime | None = None
        self._state = "idle" if self._enabled else "disabled"
        self._message = ""

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._jobs.recover_interrupted()
        await self._cleanup()
        self._loop_task = asyncio.create_task(self._loop(), name="memory-maintenance")

    async def stop(self) -> None:
        self.invalidate()
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
        request = self._request_task
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        active = self._active_run_task
        if active is not None and active is not asyncio.current_task() and active is not task:
            await asyncio.gather(active, return_exceptions=True)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def enqueue(self, session_id: str, turn_id: str) -> None:
        if not self._enabled or self._paused:
            return
        generation = self._archive.session_generation(session_id)
        if generation is None or not self._archive.sources_valid(session_id, (turn_id,), generation):
            return
        self._jobs.enqueue(session_id, turn_id, generation)

    def invalidate(self) -> None:
        self._generation += 1
        if self._cancellation is not None:
            self._cancellation.cancel("记忆整理已失效")
        if self._request_task is not None:
            self._request_task.cancel()
        self._jobs.invalidate()

    def configure(self, enabled: bool) -> None:
        self.invalidate()
        self._enabled = enabled and self._extractor is not None
        self._paused = False
        self._state = "idle" if self._enabled else "disabled"
        self._message = ""

    def status(self) -> dict[str, object]:
        return {"status": self._state, "message": self._message}

    async def run_due(self) -> bool:
        if self._busy:
            return False
        await self._cleanup()
        if self._extractor is None or not self._enabled or self._paused or self._foreground_busy():
            return False
        job = self._jobs.claim_due()
        if job is None:
            return False
        self._busy = True
        self._active_run_task = asyncio.current_task()
        generation = self._generation
        self._cancellation = CancellationSource()
        try:
            turns = self._archive.list_session_turns(job.session_id)
            unsummarized = set(self._jobs.list_unsummarized(job.session_id))
            summary_turns = tuple(turn for turn in turns if turn.turn_id in unsummarized)
            include_summary = len(summary_turns) >= SUMMARY_TURN_THRESHOLD or sum(
                len(turn.user_text) + len(turn.assistant_text) for turn in summary_turns
            ) >= SUMMARY_CHAR_THRESHOLD
            wanted = set(job.source_turn_ids)
            if include_summary:
                wanted.update(unsummarized)
            sources = tuple(turn for turn in turns if turn.turn_id in wanted)
            if not sources or not self._valid(job, generation, tuple(turn.turn_id for turn in sources)):
                self._jobs.invalidate(job.session_id)
                return False
            await self._publish_status("processing", "")
            self._request_task = asyncio.create_task(self._extractor.extract(
                sources, include_summary=include_summary,
                existing_facts=self._related_facts(sources), token=self._cancellation.token,
            ))
            result = await asyncio.wait_for(self._request_task, timeout=REQUEST_TIMEOUT)
            if not self._valid(job, generation, result.source_turn_ids):
                self._jobs.invalidate(job.session_id)
                return False
            changes = []
            # 从最终资格检查到提交之间不让出事件循环，存储仍在自己的事务中核验代数
            for proposal, evidence in result.facts:
                change = self._memory.save_fact(proposal, evidence, expected_generation=job.generation)
                if change is not None:
                    changes.append(change.id)
            summarized = ()
            if result.summary is not None:
                self._archive.save_summary(
                    result.source_turn_ids[-1], result.summary.topic, result.summary.unfinished_items,
                    source_turn_ids=result.source_turn_ids, decisions=result.summary.decisions,
                    expected_generation=job.generation,
                )
                summarized = result.source_turn_ids
            extracted = tuple(source for source in result.source_turn_ids if source in job.source_turn_ids)
            self._jobs.finish(job.id, extracted_ids=extracted, summarized_ids=summarized)
            if changes:
                await self._events.publish(MemoryChanged(job.session_id, tuple(changes)))
            await self._publish_status("idle", "")
            return True
        except MemoryInputTooLargeError as error:
            self._jobs.skip_source(job.id, error.turn_id)
            await self._publish_status("failed", "单条对话过长，已跳过自动整理")
        except LlmProtocolError:
            self.invalidate()
            self._paused = True
            await self._publish_status("failed", "当前服务未返回有效结构化数据，自动整理已暂停")
        except (CancelledError, asyncio.CancelledError):
            self._jobs.invalidate(job.session_id)
        except Exception:  # noqa: BLE001 后台边界隔离模型和存储失败，不输出原始异常正文
            self._jobs.fail(job.id, "后台整理失败")
            await self._publish_status("failed", "后台整理失败，可稍后重试")
        finally:
            self._request_task = None
            self._cancellation = None
            self._busy = False
            if self._state == "processing":
                await self._publish_status("idle" if self._enabled else "disabled", "")
            self._active_run_task = None
        return False

    def _valid(self, job, generation: int, turn_ids: tuple[str, ...]) -> bool:
        return self._enabled and not self._paused and generation == self._generation and self._archive.sources_valid(
            job.session_id, turn_ids, job.generation,
        )

    def _related_facts(self, turns) -> tuple[MemoryRecord, ...]:
        user_text = "\n".join(turn.user_text for turn in turns)
        return (*self._memory.recall_basic(limit=4),
                *self._memory.recall_related(((user_text, 1),), limit=6))

    async def _publish_status(self, status: str, message: str) -> None:
        if (self._state, self._message) == (status, message):
            return
        self._state, self._message = status, message
        await self._events.publish(MemoryMaintenanceChanged(status, message))

    async def _cleanup(self) -> None:
        now = self._clock()
        if self._last_cleanup is None or now - self._last_cleanup >= timedelta(seconds=CLEANUP_INTERVAL):
            self._last_cleanup = now
            try:
                self._archive.purge_expired()
                self._jobs.purge_terminal()
            except Exception:  # noqa: BLE001 清理失败只影响后台状态
                await self._publish_status("failed", "本地记忆清理暂时失败")

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception:  # noqa: BLE001 后台循环与前台状态机隔离
                await self._publish_status("failed", "后台整理暂时不可用")
            await asyncio.sleep(.25)
