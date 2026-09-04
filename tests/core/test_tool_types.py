from dataclasses import FrozenInstanceError

import pytest

from core.policy import (
    ConcurrencyPolicy,
    PolicyConfigurationError,
    RiskLevel,
    ToolManifest,
)
from core.tool_types import (
    RegisteredTool,
    ToolCatalog,
    ToolConfigurationError,
    ToolExecutionResult,
    ToolExecutionStatus,
)


def manifest(name="system_info"):
    return ToolManifest(
        name=name,
        description="读取系统信息",
        input_schema={
            "type": "object",
            "maxProperties": 1,
            "properties": {
                "detail": {"type": "string", "maxLength": 16},
            },
            "required": ["detail"],
            "additionalProperties": False,
        },
        base_risk=RiskLevel.R0,
        timeout=5,
        concurrency_policy=ConcurrencyPolicy.PARALLEL,
    )


class FakeHandler:
    async def execute(self, arguments, token):
        raise AssertionError("not called")


def test_execution_status_values_are_stable():
    assert [status.value for status in ToolExecutionStatus] == [
        "success",
        "denied",
        "cancelled",
        "timeout",
        "failed",
        "partial",
    ]


def test_execution_result_freezes_nested_payload_and_undo_data():
    result = ToolExecutionResult(
        ToolExecutionStatus.SUCCESS,
        {"items": [{"ok": True}]},
        "操作已完成",
        {"source": "a", "destination": "b"},
    )

    with pytest.raises(FrozenInstanceError):
        result.status = ToolExecutionStatus.FAILED
    with pytest.raises(TypeError):
        result.payload["items"][0]["ok"] = False
    with pytest.raises(TypeError):
        result.undo_data["source"] = "changed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"payload": []},
        {"payload": {1: "bad-key"}},
        {"safe_message": " "},
        {"undo_data": {"value": object()}},
    ],
)
def test_execution_result_rejects_invalid_contract_values(kwargs):
    values = {
        "status": ToolExecutionStatus.SUCCESS,
        "payload": {},
        "safe_message": "操作已完成",
        "undo_data": None,
    }
    values.update(kwargs)
    with pytest.raises(ToolConfigurationError):
        ToolExecutionResult(**values)


def test_catalog_uses_exact_names_and_projects_policy_registry():
    handler = FakeHandler()
    registered = RegisteredTool(manifest(), handler)
    catalog = ToolCatalog([registered])

    assert catalog.get("system_info") is registered
    assert catalog.get("System_Info") is None
    assert catalog.policy_registry.get("system_info") is registered.manifest


def test_catalog_rejects_duplicate_names():
    with pytest.raises((ToolConfigurationError, PolicyConfigurationError), match="重复"):
        ToolCatalog(
            [
                RegisteredTool(manifest(), FakeHandler()),
                RegisteredTool(manifest(), FakeHandler()),
            ]
        )
