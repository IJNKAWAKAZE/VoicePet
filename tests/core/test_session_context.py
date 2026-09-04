import core
from core.session_context import SessionContext


def test_session_context_is_publicly_exported():
    assert core.SessionContext is SessionContext


def test_session_context_keeps_recent_complete_turns_with_character_budget():
    context = SessionContext(max_turns=2, max_chars=80)
    context.add_turn("第一问", "第一答")
    context.add_turn("第二问", "第二答")
    context.add_turn("第三问", "第三答")

    history = context.build_history()

    assert history == (
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
        {"role": "user", "content": "第三问"},
        {"role": "assistant", "content": "第三答"},
    )


def test_session_context_keeps_only_whole_turns_within_character_budget():
    context = SessionContext(max_turns=4, max_chars=8)
    context.add_turn("第一问", "第一答")
    context.add_turn("第二问", "第二答")

    assert context.build_history() == (
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
    )


def test_session_context_ignores_blank_turn_and_clears_without_persistence():
    context = SessionContext()
    context.add_turn(" ", "回答")
    context.add_turn("问题", "\n")

    assert context.build_history() == ()

    context.add_turn("你好", "你好呀")
    context.clear()

    assert context.build_history() == ()
