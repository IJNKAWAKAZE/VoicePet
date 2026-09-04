import asyncio

import pytest

import core
from core.cancellation import CancellationSource, CancelledError
from core.network_resilience import (
    CircuitState,
    NetworkCircuitOpenError,
    NetworkResilience,
    NetworkResilienceSettings,
)


class RetryableFailure(RuntimeError):
    pass


def is_retryable(error: BaseException) -> bool:
    return isinstance(error, RetryableFailure)


def test_network_resilience_types_are_publicly_exported():
    assert core.CircuitState is CircuitState
    assert core.NetworkCircuitOpenError is NetworkCircuitOpenError
    assert core.NetworkResilience is NetworkResilience
    assert core.NetworkResilienceSettings is NetworkResilienceSettings


@pytest.mark.parametrize(
    "settings",
    [
        {"max_attempts": 0},
        {"base_delay": -0.1},
        {"max_delay": 0.0},
        {"failure_threshold": 0},
        {"recovery_timeout": 0.0},
    ],
)
def test_network_resilience_rejects_invalid_settings(settings):
    with pytest.raises(ValueError):
        NetworkResilienceSettings(**settings)


@pytest.mark.parametrize(
    "settings",
    [
        {"max_attempts": 1.5},
        {"base_delay": "0.1"},
        {"failure_threshold": 1.5},
        {"recovery_timeout": None},
    ],
)
def test_network_resilience_rejects_invalid_setting_types(settings):
    with pytest.raises(TypeError):
        NetworkResilienceSettings(**settings)


@pytest.mark.parametrize(
    "settings",
    [
        {"base_delay": float("nan")},
        {"max_delay": float("inf")},
        {"recovery_timeout": float("nan")},
    ],
)
def test_network_resilience_rejects_non_finite_settings(settings):
    with pytest.raises(ValueError):
        NetworkResilienceSettings(**settings)


def test_execute_retries_with_exponential_delays_and_success_resets_failures():
    async def scenario():
        delays = []
        calls = 0

        async def sleeper(delay):
            delays.append(delay)
            await asyncio.sleep(0)

        async def operation():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RetryableFailure("private")
            return "ok"

        resilience = NetworkResilience(
            NetworkResilienceSettings(
                max_attempts=3,
                base_delay=0.25,
                max_delay=2.0,
                failure_threshold=3,
                recovery_timeout=30.0,
            ),
            sleeper=sleeper,
        )
        result = await resilience.execute(
            operation,
            CancellationSource().token,
            retryable=is_retryable,
        )

        assert result == "ok"
        assert calls == 3
        assert delays == [0.25, 0.5]
        assert resilience.state is CircuitState.CLOSED

    asyncio.run(scenario())


def test_consecutive_failures_open_then_successful_half_open_probe_closes_circuit():
    async def scenario():
        now = [10.0]
        calls = 0

        async def fail():
            nonlocal calls
            calls += 1
            raise RetryableFailure("private")

        resilience = NetworkResilience(
            NetworkResilienceSettings(
                max_attempts=1,
                failure_threshold=2,
                recovery_timeout=5.0,
            ),
            clock=lambda: now[0],
        )
        token = CancellationSource().token

        for _ in range(2):
            with pytest.raises(RetryableFailure):
                await resilience.execute(fail, token, retryable=is_retryable)
        assert resilience.state is CircuitState.OPEN

        with pytest.raises(NetworkCircuitOpenError):
            await resilience.execute(fail, token, retryable=is_retryable)
        assert calls == 2

        now[0] += 5.0
        assert resilience.state is CircuitState.HALF_OPEN
        assert await resilience.execute(
            lambda: asyncio.sleep(0, result="recovered"),
            token,
            retryable=is_retryable,
        ) == "recovered"
        assert resilience.state is CircuitState.CLOSED

    asyncio.run(scenario())


def test_only_one_half_open_probe_can_run_at_once():
    async def scenario():
        now = [0.0]
        probe_started = asyncio.Event()
        release_probe = asyncio.Event()
        resilience = NetworkResilience(
            NetworkResilienceSettings(
                max_attempts=1,
                failure_threshold=1,
                recovery_timeout=1.0,
            ),
            clock=lambda: now[0],
        )
        token = CancellationSource().token

        async def fail():
            raise RetryableFailure("private")

        with pytest.raises(RetryableFailure):
            await resilience.execute(fail, token, retryable=is_retryable)
        now[0] = 1.0

        async def probe():
            probe_started.set()
            await release_probe.wait()
            return "ok"

        first = asyncio.create_task(
            resilience.execute(probe, token, retryable=is_retryable)
        )
        await probe_started.wait()
        with pytest.raises(NetworkCircuitOpenError):
            await resilience.execute(probe, token, retryable=is_retryable)
        release_probe.set()
        assert await first == "ok"
        assert resilience.state is CircuitState.CLOSED

    asyncio.run(scenario())


def test_late_success_from_previous_generation_cannot_close_new_circuit():
    async def scenario():
        late_started = asyncio.Event()
        release_late = asyncio.Event()
        resilience = NetworkResilience(
            NetworkResilienceSettings(
                max_attempts=1,
                failure_threshold=1,
                recovery_timeout=30.0,
            )
        )
        token = CancellationSource().token

        async def late_success():
            late_started.set()
            await release_late.wait()
            return "late"

        async def fail():
            raise RetryableFailure("private")

        late_task = asyncio.create_task(
            resilience.execute(late_success, token, retryable=is_retryable)
        )
        await late_started.wait()
        with pytest.raises(RetryableFailure):
            await resilience.execute(fail, token, retryable=is_retryable)
        assert resilience.state is CircuitState.OPEN

        release_late.set()
        assert await late_task == "late"
        assert resilience.state is CircuitState.OPEN

    asyncio.run(scenario())


def test_half_open_non_retryable_error_releases_probe_without_new_cooldown():
    async def scenario():
        now = [0.0]
        resilience = NetworkResilience(
            NetworkResilienceSettings(
                max_attempts=1,
                failure_threshold=1,
                recovery_timeout=1.0,
            ),
            clock=lambda: now[0],
        )
        token = CancellationSource().token

        async def network_failure():
            raise RetryableFailure("private")

        async def invalid_request():
            raise ValueError("invalid")

        with pytest.raises(RetryableFailure):
            await resilience.execute(
                network_failure,
                token,
                retryable=is_retryable,
            )
        now[0] = 1.0
        with pytest.raises(ValueError):
            await resilience.execute(
                invalid_request,
                token,
                retryable=is_retryable,
            )

        assert resilience.state is CircuitState.HALF_OPEN
        assert await resilience.execute(
            lambda: asyncio.sleep(0, result="recovered"),
            token,
            retryable=is_retryable,
        ) == "recovered"

    asyncio.run(scenario())


def test_half_open_cancellation_releases_probe_without_new_cooldown():
    async def scenario():
        now = [0.0]
        resilience = NetworkResilience(
            NetworkResilienceSettings(
                max_attempts=1,
                failure_threshold=1,
                recovery_timeout=1.0,
            ),
            clock=lambda: now[0],
        )

        async def network_failure():
            raise RetryableFailure("private")

        with pytest.raises(RetryableFailure):
            await resilience.execute(
                network_failure,
                CancellationSource().token,
                retryable=is_retryable,
            )
        now[0] = 1.0

        source = CancellationSource()
        started = asyncio.Event()

        async def cancelled_probe():
            started.set()
            await source.token.wait()
            source.token.throw_if_cancelled()

        task = asyncio.create_task(
            resilience.execute(
                cancelled_probe,
                source.token,
                retryable=is_retryable,
            )
        )
        await started.wait()
        source.cancel("interrupt")
        with pytest.raises(CancelledError):
            await task

        assert resilience.state is CircuitState.HALF_OPEN
        assert await resilience.execute(
            lambda: asyncio.sleep(0, result="recovered"),
            CancellationSource().token,
            retryable=is_retryable,
        ) == "recovered"

    asyncio.run(scenario())


def test_non_retryable_error_is_not_retried_or_counted():
    async def scenario():
        calls = 0
        resilience = NetworkResilience(
            NetworkResilienceSettings(failure_threshold=1)
        )

        async def operation():
            nonlocal calls
            calls += 1
            raise ValueError("invalid")

        with pytest.raises(ValueError):
            await resilience.execute(
                operation,
                CancellationSource().token,
                retryable=is_retryable,
            )
        assert calls == 1
        assert resilience.state is CircuitState.CLOSED

    asyncio.run(scenario())


def test_cancellation_interrupts_backoff_without_another_attempt():
    async def scenario():
        sleep_started = asyncio.Event()
        never = asyncio.Event()
        calls = 0

        async def sleeper(delay):
            del delay
            sleep_started.set()
            await never.wait()

        async def operation():
            nonlocal calls
            calls += 1
            raise RetryableFailure("private")

        source = CancellationSource()
        resilience = NetworkResilience(
            NetworkResilienceSettings(failure_threshold=3),
            sleeper=sleeper,
        )
        task = asyncio.create_task(
            resilience.execute(
                operation,
                source.token,
                retryable=is_retryable,
            )
        )
        await sleep_started.wait()
        source.cancel("interrupt")

        with pytest.raises(CancelledError):
            await task
        assert calls == 1

    asyncio.run(scenario())


def test_stream_retries_only_before_first_event():
    async def scenario():
        calls = 0
        delays = []

        async def sleeper(delay):
            delays.append(delay)

        def operation():
            async def events():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RetryableFailure("private")
                yield "first"
                yield "second"

            return events()

        resilience = NetworkResilience(sleeper=sleeper)
        received = [
            item
            async for item in resilience.stream(
                operation,
                CancellationSource().token,
                retryable=is_retryable,
            )
        ]

        assert received == ["first", "second"]
        assert calls == 2
        assert delays == [0.25]

    asyncio.run(scenario())


def test_stream_failure_after_first_event_is_never_replayed():
    async def scenario():
        calls = 0
        received = []

        def operation():
            async def events():
                nonlocal calls
                calls += 1
                yield "visible"
                raise RetryableFailure("private")

            return events()

        resilience = NetworkResilience()
        with pytest.raises(RetryableFailure):
            async for item in resilience.stream(
                operation,
                CancellationSource().token,
                retryable=is_retryable,
            ):
                received.append(item)

        assert received == ["visible"]
        assert calls == 1

    asyncio.run(scenario())


def test_closing_half_open_stream_releases_probe_slot():
    async def scenario():
        now = [0.0]
        inner_closed = []
        resilience = NetworkResilience(
            NetworkResilienceSettings(
                max_attempts=1,
                failure_threshold=1,
                recovery_timeout=1.0,
            ),
            clock=lambda: now[0],
        )
        token = CancellationSource().token

        async def fail():
            raise RetryableFailure("private")

        with pytest.raises(RetryableFailure):
            await resilience.execute(fail, token, retryable=is_retryable)
        now[0] = 1.0

        def stream_operation():
            async def events():
                try:
                    yield "visible"
                    await asyncio.Event().wait()
                finally:
                    inner_closed.append(True)

            return events()

        stream = resilience.stream(
            stream_operation,
            token,
            retryable=is_retryable,
        )
        assert await anext(stream) == "visible"
        await stream.aclose()

        assert inner_closed == [True]
        assert await resilience.execute(
            lambda: asyncio.sleep(0, result="recovered"),
            token,
            retryable=is_retryable,
        ) == "recovered"
        assert resilience.state is CircuitState.CLOSED

    asyncio.run(scenario())
