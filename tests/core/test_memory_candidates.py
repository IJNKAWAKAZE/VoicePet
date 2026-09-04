from uuid import uuid4

import pytest

import core
from core.memory import MemoryConfigurationError, MemoryStatus, MemoryStore
from core.memory_candidates import (
    MEMORY_CANDIDATE_TOOL_NAME,
    MemoryCandidateService,
    memory_candidate_tool_definition,
)


def test_memory_candidate_types_are_publicly_exported():
    assert core.MemoryCandidateService is MemoryCandidateService
    assert core.memory_candidate_tool_definition is memory_candidate_tool_definition


def test_candidate_tool_definition_has_strict_bounded_schema():
    definition = memory_candidate_tool_definition()

    assert definition.name == MEMORY_CANDIDATE_TOOL_NAME
    assert definition.input_schema == {
        "type": "object",
        "properties": {
            "category": {"type": "string", "maxLength": 64},
            "content": {"type": "string", "maxLength": 4096},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ("category", "content", "confidence"),
        "additionalProperties": False,
    }


def test_candidate_service_persists_only_candidate_status(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    service = MemoryCandidateService(store)

    record = service.create(
        {
            "category": "preference",
            "content": "用户偏好低音量",
            "confidence": 0.8,
        },
        str(uuid4()),
    )

    assert record.status is MemoryStatus.CANDIDATE
    assert record.category == "preference"
    assert record.content == "用户偏好低音量"
    assert record.confidence == 0.8
    store.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"category": "preference", "content": "fact"},
        {
            "category": "preference",
            "content": "fact",
            "confidence": 0.8,
            "extra": True,
        },
        {"category": "", "content": "fact", "confidence": 0.8},
        {"category": "preference", "content": " ", "confidence": 0.8},
        {"category": "preference", "content": "fact", "confidence": True},
    ],
)
def test_candidate_service_rejects_invalid_model_arguments(tmp_path, arguments):
    store = MemoryStore(tmp_path / "assistant.db")

    with pytest.raises(MemoryConfigurationError, match="候选"):
        MemoryCandidateService(store).create(arguments, str(uuid4()))

    assert store.list_all() == ()
    store.close()


def test_candidate_service_reuses_secret_policy_and_memory_enabled_gate(tmp_path):
    store = MemoryStore(tmp_path / "assistant.db")
    service = MemoryCandidateService(store)
    source_turn_id = str(uuid4())

    with pytest.raises(MemoryConfigurationError, match="敏感"):
        service.create(
            {
                "category": "account",
                "content": "password=secret123",
                "confidence": 0.9,
            },
            source_turn_id,
        )
    store.set_enabled(False)
    with pytest.raises(MemoryConfigurationError, match="关闭"):
        service.create(
            {
                "category": "preference",
                "content": "用户偏好安静",
                "confidence": 0.9,
            },
            source_turn_id,
        )

    assert store.list_all() == ()
    store.close()
