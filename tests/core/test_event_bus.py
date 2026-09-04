import asyncio

import pytest

from core.event_bus import EventBus
from core.events import CorrelationId, SpeakRequested, TextDelta, TurnId


def make_delta(text: str = "hello") -> TextDelta:
    return TextDelta(TurnId.new(), CorrelationId.new(), text)


def test_subscriber_receives_exact_event_type_and_can_unsubscribe():
    async def scenario():
        bus = EventBus()
        seen = []
        unrelated = []
        handle = bus.subscribe(TextDelta, seen.append)
        bus.subscribe(SpeakRequested, unrelated.append)
        event = make_delta()

        await bus.publish(event)
        handle.close()
        handle.close()
        await bus.publish(make_delta("ignored"))
        return event, seen, unrelated

    event, seen, unrelated = asyncio.run(scenario())

    assert seen == [event]
    assert unrelated == []


def test_async_callbacks_finish_in_registration_order():
    async def scenario():
        bus = EventBus()
        order = []

        async def first(event):
            order.append(("first-start", event.text))
            await asyncio.sleep(0)
            order.append(("first-end", event.text))

        def second(event):
            order.append(("second", event.text))

        bus.subscribe(TextDelta, first)
        bus.subscribe(TextDelta, second)
        await bus.publish(make_delta())
        return order

    assert asyncio.run(scenario()) == [
        ("first-start", "hello"),
        ("first-end", "hello"),
        ("second", "hello"),
    ]


def test_callback_failures_are_grouped_after_later_subscribers_run():
    async def scenario():
        bus = EventBus()
        seen = []

        def failing(event):
            raise ValueError(event.text)

        bus.subscribe(TextDelta, failing)
        bus.subscribe(TextDelta, seen.append)
        with pytest.raises(ExceptionGroup) as captured:
            await bus.publish(make_delta())
        return captured.value, seen

    error, seen = asyncio.run(scenario())

    assert len(error.exceptions) == 1
    assert isinstance(error.exceptions[0], ValueError)
    assert [event.text for event in seen] == ["hello"]


def test_async_error_hook_handles_callback_failure():
    async def scenario():
        handled = []

        async def on_callback_error(event, callback, error):
            await asyncio.sleep(0)
            handled.append((event, callback, error))

        bus = EventBus(on_callback_error=on_callback_error)

        def failing(event):
            raise LookupError(event.text)

        bus.subscribe(TextDelta, failing)
        event = make_delta()
        await bus.publish(event)
        return handled, event, failing

    handled, event, failing = asyncio.run(scenario())

    assert len(handled) == 1
    assert handled[0][0] == event
    assert handled[0][1] is failing
    assert isinstance(handled[0][2], LookupError)
