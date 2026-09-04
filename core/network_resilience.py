"""为无副作用网络操作提供可取消的指数退避与熔断"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from numbers import Real
from typing import TypeVar

from .cancellation import CancellationToken, CancelledError

T = TypeVar("T")


class CircuitState(str, Enum):
    """网络熔断器对调用方公开的稳定状态"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class NetworkCircuitOpenError(RuntimeError):
    """网络熔断期间快速拒绝底层调用"""


@dataclass(frozen=True, slots=True)
class NetworkResilienceSettings:
    """单个网络服务的退避与熔断参数"""

    max_attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 2.0
    failure_threshold: int = 3
    recovery_timeout: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts,
            int,
        ):
            raise TypeError("网络请求最大尝试次数类型无效")
        if isinstance(self.failure_threshold, bool) or not isinstance(
            self.failure_threshold,
            int,
        ):
            raise TypeError("网络熔断失败阈值类型无效")
        numeric_settings = (
            self.base_delay,
            self.max_delay,
            self.recovery_timeout,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, Real)
            for value in numeric_settings
        ):
            raise TypeError("网络退避与恢复时间类型无效")
        if not all(math.isfinite(float(value)) for value in numeric_settings):
            raise ValueError("网络退避与恢复时间必须是有限数值")
        if self.max_attempts <= 0:
            raise ValueError("网络请求最大尝试次数必须大于零")
        if self.base_delay < 0:
            raise ValueError("网络请求基础退避时间不能小于零")
        if self.max_delay <= 0 or self.base_delay > self.max_delay:
            raise ValueError("网络请求最大退避时间必须覆盖基础退避时间")
        if self.failure_threshold <= 0:
            raise ValueError("网络熔断失败阈值必须大于零")
        if self.recovery_timeout <= 0:
            raise ValueError("网络熔断恢复时间必须大于零")


@dataclass(frozen=True, slots=True)
class _OperationPermit:
    generation: int
    half_open: bool


class NetworkResilience:
    """在调用边界维护重试计数和熔断状态"""

    def __init__(
        self,
        settings: NetworkResilienceSettings | None = None,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings or NetworkResilienceSettings()
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or asyncio.sleep
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._generation = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """返回包含冷却时间判断的当前熔断状态"""

        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self._settings.recovery_timeout:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        token: CancellationToken,
        *,
        retryable: Callable[[BaseException], bool],
    ) -> T:
        """执行完整返回结果的网络操作"""

        token.throw_if_cancelled()
        permit = await self._begin_operation()
        try:
            for attempt in range(self._settings.max_attempts):
                token.throw_if_cancelled()
                try:
                    result = await operation()
                    token.throw_if_cancelled()
                except (CancelledError, asyncio.CancelledError):
                    await self._abandon_operation(permit)
                    raise
                except Exception as error:
                    if not retryable(error):
                        await self._abandon_operation(permit)
                        raise
                    opened = await self._record_failure(permit)
                    if opened or attempt + 1 >= self._settings.max_attempts:
                        raise
                    await self._wait_before_retry(attempt, token)
                    continue
                await self._record_success(permit)
                return result
        except (CancelledError, asyncio.CancelledError):
            await self._abandon_operation(permit)
            raise
        raise RuntimeError("网络请求重试循环异常结束")

    async def stream(
        self,
        operation: Callable[[], AsyncIterator[T]],
        token: CancellationToken,
        *,
        retryable: Callable[[BaseException], bool],
    ) -> AsyncIterator[T]:
        """仅在尚未产出事件时重试流式网络操作"""

        token.throw_if_cancelled()
        permit = await self._begin_operation()
        settled = False
        try:
            for attempt in range(self._settings.max_attempts):
                emitted = False
                token.throw_if_cancelled()
                iterator = operation()
                try:
                    async for item in iterator:
                        token.throw_if_cancelled()
                        emitted = True
                        yield item
                        token.throw_if_cancelled()
                except (CancelledError, asyncio.CancelledError):
                    await self._abandon_operation(permit)
                    settled = True
                    raise
                except Exception as error:
                    if not retryable(error):
                        await self._abandon_operation(permit)
                        settled = True
                        raise
                    opened = await self._record_failure(permit)
                    if permit.half_open:
                        settled = True
                    if (
                        emitted
                        or opened
                        or attempt + 1 >= self._settings.max_attempts
                    ):
                        raise
                    await self._wait_before_retry(attempt, token)
                    continue
                finally:
                    await self._close_iterator(iterator)
                await self._record_success(permit)
                settled = True
                return
        except (CancelledError, asyncio.CancelledError):
            await self._abandon_operation(permit)
            settled = True
            raise
        finally:
            if permit.half_open and not settled:
                await self._abandon_operation(permit)

    @staticmethod
    async def _close_iterator(iterator: AsyncIterator[object]) -> None:
        """在重试或外层关闭时立即释放底层流资源"""

        close = getattr(iterator, "aclose", None)
        if close is None:
            close = getattr(iterator, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def _begin_operation(self) -> _OperationPermit:
        async with self._lock:
            if self._opened_at is None:
                return _OperationPermit(self._generation, False)
            elapsed = self._clock() - self._opened_at
            if elapsed < self._settings.recovery_timeout:
                raise NetworkCircuitOpenError("网络服务熔断中")
            if self._probe_in_flight:
                raise NetworkCircuitOpenError("网络服务正在进行恢复探测")
            self._probe_in_flight = True
            return _OperationPermit(self._generation, True)

    async def _record_success(self, permit: _OperationPermit) -> None:
        async with self._lock:
            if permit.generation != self._generation:
                return
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    async def _record_failure(self, permit: _OperationPermit) -> bool:
        async with self._lock:
            if permit.generation != self._generation:
                return self._opened_at is not None
            self._consecutive_failures += 1
            if (
                permit.half_open
                or self._consecutive_failures
                >= self._settings.failure_threshold
            ):
                self._generation += 1
                self._opened_at = self._clock()
                self._probe_in_flight = False
                return True
            return False

    async def _abandon_operation(self, permit: _OperationPermit) -> None:
        if not permit.half_open:
            return
        async with self._lock:
            if permit.generation == self._generation:
                self._probe_in_flight = False

    async def _wait_before_retry(
        self,
        attempt: int,
        token: CancellationToken,
    ) -> None:
        delay = min(
            self._settings.base_delay * (2**attempt),
            self._settings.max_delay,
        )
        token.throw_if_cancelled()
        sleep_task = asyncio.create_task(self._sleeper(delay))
        cancel_task = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                (sleep_task, cancel_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                token.throw_if_cancelled()
            await sleep_task
            token.throw_if_cancelled()
        finally:
            for task in (sleep_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                sleep_task,
                cancel_task,
                return_exceptions=True,
            )
