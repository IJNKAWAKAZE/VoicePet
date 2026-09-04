from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

import core
from core.session_archive import SessionArchiveStore
from core.short_term_summary import (
    SHORT_TERM_SUMMARY_TOOL_NAME,
    ShortTermSummaryDraft,
    ShortTermSummaryService,
    short_term_summary_tool_definition,
)


def test_short_term_summary_types_are_publicly_exported():
    assert core.ShortTermSummaryService is ShortTermSummaryService
    assert core.ShortTermSummaryDraft is ShortTermSummaryDraft


def test_summary_tool_definition_is_strict_and_bounded():
    definition = short_term_summary_tool_definition()

    assert definition.name == SHORT_TERM_SUMMARY_TOOL_NAME
    assert definition.input_schema["additionalProperties"] is False
    assert definition.input_schema["required"] == (
        "topic",
        "unfinished_items",
    )
    assert definition.input_schema["properties"]["unfinished_items"][
        "maxItems"
    ] == 10


def test_summary_service_validates_draft_then_saves_for_archived_turn(tmp_path):
    archive = SessionArchiveStore(
        tmp_path / "assistant.db",
        clock=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )
    service = ShortTermSummaryService(archive)
    turn_id = str(uuid4())
    archive.archive_turn(turn_id, "规划发布", "先跑测试")

    draft = service.prepare(
        {"topic": "发布准备", "unfinished_items": ["完成测试", "打包"]}
    )
    record = service.save(turn_id, draft)

    assert draft == ShortTermSummaryDraft("发布准备", ("完成测试", "打包"))
    with pytest.raises(FrozenInstanceError):
        draft.topic = "changed"
    assert record.source_turn_id == turn_id
    assert archive.list_summaries() == (record,)
    archive.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"topic": "话题"},
        {"topic": "", "unfinished_items": []},
        {"topic": "话题", "unfinished_items": "事项"},
        {"topic": "话题", "unfinished_items": ["x"] * 11},
        {"topic": "话题", "unfinished_items": [""]},
        {"topic": "话题", "unfinished_items": [], "extra": True},
    ],
)
def test_summary_service_rejects_invalid_model_arguments(tmp_path, arguments):
    archive = SessionArchiveStore(tmp_path / "assistant.db")

    with pytest.raises((TypeError, ValueError), match="摘要"):
        ShortTermSummaryService(archive).prepare(arguments)

    assert archive.list_summaries() == ()
    archive.close()


def test_summary_service_rejects_sensitive_topic_or_unfinished_item(tmp_path):
    archive = SessionArchiveStore(tmp_path / "assistant.db")
    service = ShortTermSummaryService(archive)

    with pytest.raises(ValueError, match="敏感"):
        service.prepare(
            {
                "topic": "账户设置",
                "unfinished_items": ["保存 password=secret123"],
            }
        )

    archive.close()
