from dataclasses import FrozenInstanceError

import pytest

import core
from core.turn_budget import (
    TurnBudget,
    TurnBudgetExceededError,
    TurnBudgetLimits,
)


def test_turn_budget_types_are_publicly_exported():
    assert core.TurnBudget is TurnBudget
    assert core.TurnBudgetExceededError is TurnBudgetExceededError
    assert core.TurnBudgetLimits is TurnBudgetLimits


@pytest.mark.parametrize(
    "settings",
    [
        {"max_duration": 0},
        {"max_duration": float("nan")},
        {"max_duration": float("inf")},
        {"max_tokens": 0},
        {"max_tool_calls": 0},
    ],
)
def test_turn_budget_limits_reject_invalid_values(settings):
    with pytest.raises(ValueError):
        TurnBudgetLimits(**settings)


@pytest.mark.parametrize(
    "settings",
    [
        {"max_duration": "120"},
        {"max_tokens": 1.5},
        {"max_tool_calls": True},
    ],
)
def test_turn_budget_limits_reject_invalid_types(settings):
    with pytest.raises(TypeError):
        TurnBudgetLimits(**settings)


def test_turn_budget_limits_are_immutable():
    limits = TurnBudgetLimits()

    with pytest.raises(FrozenInstanceError):
        limits.max_tokens = 1


def test_turn_budget_tracks_monotonic_remaining_duration_and_expiry():
    now = [10.0]
    budget = TurnBudget(
        TurnBudgetLimits(max_duration=120.0),
        clock=lambda: now[0],
    )

    assert budget.remaining_duration() == 120.0
    assert budget.elapsed_ms() == 0
    now[0] = 40.25
    assert budget.remaining_duration() == 89.75
    assert budget.elapsed_ms() == 30_250
    now[0] = 130.0
    with pytest.raises(TurnBudgetExceededError) as captured:
        budget.ensure_available()
    assert captured.value.kind == "duration"
    assert captured.value.code == "runtime.budget"
    assert captured.value.retryable is False


def test_turn_budget_clamps_output_tokens_to_remaining_total():
    budget = TurnBudget(TurnBudgetLimits(max_tokens=100))

    assert budget.output_token_limit(60) == 60
    assert budget.record_usage(50, 30) == 80
    assert budget.output_token_limit(60) == 20
    assert budget.record_usage(10, 10) == 100
    with pytest.raises(TurnBudgetExceededError) as captured:
        budget.output_token_limit(60)
    assert captured.value.kind == "tokens"


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1), (1.5, 0), (0, True)],
)
def test_turn_budget_rejects_invalid_usage(input_tokens, output_tokens):
    budget = TurnBudget()

    with pytest.raises((TypeError, ValueError)):
        budget.record_usage(input_tokens, output_tokens)
    assert budget.total_tokens == 0


def test_turn_budget_reserves_tool_calls_without_exceeding_limit():
    budget = TurnBudget(TurnBudgetLimits(max_tool_calls=2))

    assert budget.reserve_tool_call() == 1
    assert budget.reserve_tool_call() == 2
    with pytest.raises(TurnBudgetExceededError) as captured:
        budget.reserve_tool_call()
    assert captured.value.kind == "tool_calls"
    assert budget.tool_calls_used == 2


def test_turn_budgets_keep_counters_independent():
    first = TurnBudget()
    second = TurnBudget()

    first.record_usage(10, 5)
    first.reserve_tool_call()

    assert first.total_tokens == 15
    assert first.tool_calls_used == 1
    assert second.total_tokens == 0
    assert second.tool_calls_used == 0


def test_turn_budget_clamps_backward_clock_without_negative_elapsed_time():
    now = [10.0]
    budget = TurnBudget(clock=lambda: now[0])
    now[0] = 5.0

    assert budget.elapsed_ms() == 0
    assert budget.remaining_duration() == 120.0
