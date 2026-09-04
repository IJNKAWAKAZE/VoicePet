import asyncio

import pytest

from core.cancellation import CancellationSource, CancelledError


def test_cancel_is_idempotent_and_token_raises_original_reason():
    source = CancellationSource()

    assert not source.token.is_cancelled
    assert source.token.reason is None

    source.cancel("user_interrupt")
    source.cancel("duplicate")

    assert source.token.is_cancelled
    assert source.token.reason == "user_interrupt"
    with pytest.raises(CancelledError) as captured:
        source.token.throw_if_cancelled()
    assert captured.value.reason == "user_interrupt"


def test_wait_returns_original_cancellation_reason():
    async def scenario():
        source = CancellationSource()
        waiter = asyncio.create_task(source.token.wait())
        await asyncio.sleep(0)
        assert not waiter.done()

        source.cancel("shutdown")
        return await waiter

    assert asyncio.run(scenario()) == "shutdown"


def test_token_does_not_expose_mutating_cancel_operation():
    source = CancellationSource()

    assert not hasattr(source.token, "cancel")
