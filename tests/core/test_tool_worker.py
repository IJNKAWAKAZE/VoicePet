import asyncio
from uuid import uuid4

from core.audit import AuditContext, AuditStore
from core.cancellation import CancellationSource, CancelledError
from core.policy import (
    AuthorizationIssuer,
    AuthorizationVerifier,
    ConcurrencyPolicy,
    ConfirmationMode,
    PolicyEngine,
    PolicySettings,
    RiskLevel,
    ToolManifest,
    ToolProposal,
)
from core.tool_types import (
    RegisteredTool,
    ToolCatalog,
    ToolExecutionResult,
    ToolExecutionStatus,
)
from core.tool_worker import ToolWorkerService


def manifest(name="test_tool", risk=RiskLevel.R0, timeout=1.0, concurrency=None):
    return ToolManifest(
        name=name,
        description=f"执行 {name}",
        input_schema={
            "type": "object",
            "maxProperties": 1,
            "properties": {
                "value": {"type": "string", "maxLength": 32},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        base_risk=risk,
        timeout=timeout,
        concurrency_policy=concurrency or ConcurrencyPolicy.SERIAL,
    )


class FakeHandler:
    def __init__(self, *, result=None, error=None, delay=0) -> None:
        self.result = result or ToolExecutionResult(
            ToolExecutionStatus.SUCCESS,
            {"ok": True},
            "执行成功",
        )
        self.error = error
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def execute(self, arguments, token):
        self.calls.append((arguments, token))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            self.active -= 1


def decision_payload(decision):
    return {
        "allowed": decision.allowed,
        "risk": decision.risk.label,
        "confirmation": decision.confirmation.value,
        "summary": decision.summary,
        "call_fingerprint": decision.call_fingerprint,
        "policy_version": decision.policy_version,
    }


def proposal_payload(proposal):
    return {
        "call_id": proposal.call_id,
        "tool_name": proposal.tool_name,
        "arguments": dict(proposal.arguments),
    }


def service_case(
    *,
    risk=RiskLevel.R0,
    settings=None,
    handler=None,
    timeout=1.0,
    audit_store=None,
):
    handler = handler or FakeHandler()
    registered = RegisteredTool(manifest(risk=risk, timeout=timeout), handler)
    catalog = ToolCatalog([registered])
    engine = PolicyEngine(catalog.policy_registry, settings or PolicySettings())
    key = b"k" * 32
    service = ToolWorkerService(
        catalog,
        engine,
        AuthorizationVerifier(key),
        audit_store=audit_store,
    )
    proposal = ToolProposal("call-1", "test_tool", {"value": "hello"})
    decision = engine.evaluate(proposal)
    return service, handler, proposal, decision, key


def test_worker_executes_auto_decision_without_authorization():
    async def scenario():
        service, handler, proposal, decision, _ = service_case()

        result = await service.execute(
            proposal_payload(proposal),
            decision_payload(decision),
            None,
            CancellationSource().token,
        )

        assert result.status is ToolExecutionStatus.SUCCESS
        assert len(handler.calls) == 1

    asyncio.run(scenario())


def test_worker_consumes_valid_authorization_and_rejects_replay():
    async def scenario():
        service, handler, proposal, decision, key = service_case(risk=RiskLevel.R2)
        token = AuthorizationIssuer(key).issue(decision, ConfirmationMode.VOICE)
        source = CancellationSource()

        first = await service.execute(
            proposal_payload(proposal),
            decision_payload(decision),
            token,
            source.token,
        )
        replay = await service.execute(
            proposal_payload(proposal),
            decision_payload(decision),
            token,
            source.token,
        )

        assert first.status is ToolExecutionStatus.SUCCESS
        assert replay.status is ToolExecutionStatus.DENIED
        assert len(handler.calls) == 1

    asyncio.run(scenario())


def test_worker_rejects_missing_or_unexpected_authorization():
    async def scenario():
        r2_service, r2_handler, proposal, decision, _ = service_case(
            risk=RiskLevel.R2
        )
        missing = await r2_service.execute(
            proposal_payload(proposal),
            decision_payload(decision),
            None,
            CancellationSource().token,
        )

        r0_service, r0_handler, proposal, decision, key = service_case()
        unexpected = await r0_service.execute(
            proposal_payload(proposal),
            decision_payload(decision),
            AuthorizationIssuer(key),
            CancellationSource().token,
        )

        assert missing.status is ToolExecutionStatus.DENIED
        assert unexpected.status is ToolExecutionStatus.DENIED
        assert r2_handler.calls == []
        assert r0_handler.calls == []

    asyncio.run(scenario())


def test_worker_rejects_forged_parent_decision_and_policy_drift():
    async def scenario():
        service, handler, proposal, decision, _ = service_case(risk=RiskLevel.R2)
        forged = decision_payload(decision)
        forged["risk"] = "R0"
        forged["confirmation"] = "none"
        forged_result = await service.execute(
            proposal_payload(proposal),
            forged,
            None,
            CancellationSource().token,
        )

        old_engine = PolicyEngine(
            service.catalog.policy_registry,
            PolicySettings(version=1),
        )
        old_decision = old_engine.evaluate(proposal)
        service.policy_engine.update_settings(PolicySettings(version=2))
        drift = await service.execute(
            proposal_payload(proposal),
            decision_payload(old_decision),
            None,
            CancellationSource().token,
        )

        assert forged_result.status is ToolExecutionStatus.DENIED
        assert drift.status is ToolExecutionStatus.DENIED
        assert handler.calls == []

    asyncio.run(scenario())


def test_worker_returns_denied_for_disabled_and_unknown_tools():
    async def scenario():
        settings = PolicySettings(disabled_tools=frozenset({"test_tool"}))
        service, handler, proposal, decision, _ = service_case(settings=settings)
        disabled = await service.execute(
            proposal_payload(proposal),
            decision_payload(decision),
            None,
            CancellationSource().token,
        )
        unknown_proposal = ToolProposal("call-2", "unknown_tool", {})
        unknown_decision = service.policy_engine.evaluate(unknown_proposal)
        unknown = await service.execute(
            proposal_payload(unknown_proposal),
            decision_payload(unknown_decision),
            None,
            CancellationSource().token,
        )

        assert disabled.status is ToolExecutionStatus.DENIED
        assert unknown.status is ToolExecutionStatus.DENIED
        assert handler.calls == []

    asyncio.run(scenario())


def test_worker_maps_handler_error_timeout_and_cancellation():
    async def scenario():
        cases = [
            (
                FakeHandler(error=RuntimeError("private argument")),
                1.0,
                ToolExecutionStatus.FAILED,
            ),
            (FakeHandler(delay=0.05), 0.01, ToolExecutionStatus.TIMEOUT),
            (
                FakeHandler(error=CancelledError("interrupt")),
                1.0,
                ToolExecutionStatus.CANCELLED,
            ),
        ]
        for handler, timeout, expected in cases:
            service, _, proposal, decision, _ = service_case(
                handler=handler,
                timeout=timeout,
            )
            result = await service.execute(
                proposal_payload(proposal),
                decision_payload(decision),
                None,
                CancellationSource().token,
            )
            assert result.status is expected
            assert "private argument" not in result.safe_message

    asyncio.run(scenario())


class OverlapTracker:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0


class TrackingHandler(FakeHandler):
    def __init__(self, tracker, delay=0.03) -> None:
        super().__init__(delay=delay)
        self.tracker = tracker

    async def execute(self, arguments, token):
        self.tracker.active += 1
        self.tracker.max_active = max(
            self.tracker.max_active,
            self.tracker.active,
        )
        try:
            return await super().execute(arguments, token)
        finally:
            self.tracker.active -= 1


def test_serial_policy_excludes_different_tools_globally():
    async def scenario():
        tracker = OverlapTracker()
        tools = [
            RegisteredTool(
                manifest(name=name, concurrency=ConcurrencyPolicy.SERIAL),
                TrackingHandler(tracker),
            )
            for name in ("first_tool", "second_tool")
        ]
        catalog = ToolCatalog(tools)
        engine = PolicyEngine(catalog.policy_registry, PolicySettings())
        service = ToolWorkerService(
            catalog,
            engine,
            AuthorizationVerifier(b"k" * 32),
        )
        proposals = [
            ToolProposal(f"call-{index}", tool.manifest.name, {"value": "x"})
            for index, tool in enumerate(tools)
        ]

        await asyncio.gather(
            *(
                service.execute(
                    proposal_payload(proposal),
                    decision_payload(engine.evaluate(proposal)),
                    None,
                    CancellationSource().token,
                )
                for proposal in proposals
            )
        )

        assert tracker.max_active == 1

    asyncio.run(scenario())


def test_per_tool_policy_serializes_same_tool_while_parallel_policy_overlaps():
    async def maximum_overlap(policy):
        tracker = OverlapTracker()
        handler = TrackingHandler(tracker)
        registered = RegisteredTool(
            manifest(concurrency=policy),
            handler,
        )
        catalog = ToolCatalog([registered])
        engine = PolicyEngine(catalog.policy_registry, PolicySettings())
        service = ToolWorkerService(
            catalog,
            engine,
            AuthorizationVerifier(b"k" * 32),
        )
        proposals = [
            ToolProposal(f"call-{index}", "test_tool", {"value": "x"})
            for index in range(2)
        ]
        await asyncio.gather(
            *(
                service.execute(
                    proposal_payload(proposal),
                    decision_payload(engine.evaluate(proposal)),
                    None,
                    CancellationSource().token,
                )
                for proposal in proposals
            )
        )
        return tracker.max_active

    assert asyncio.run(maximum_overlap(ConcurrencyPolicy.PER_TOOL)) == 1
    assert asyncio.run(maximum_overlap(ConcurrencyPolicy.PARALLEL)) == 2


def test_worker_forwards_cooperative_cancellation_to_handler():
    class WaitingHandler:
        async def execute(self, arguments, token):
            del arguments
            await token.wait()
            token.throw_if_cancelled()
            raise AssertionError("cancelled handler must not continue")

    async def scenario():
        service, _, proposal, decision, _ = service_case(handler=WaitingHandler())
        source = CancellationSource()
        task = asyncio.create_task(
            service.execute(
                proposal_payload(proposal),
                decision_payload(decision),
                None,
                source.token,
            )
        )
        await asyncio.sleep(0)
        source.cancel("interrupt")

        result = await task

        assert result.status is ToolExecutionStatus.CANCELLED

    asyncio.run(scenario())


def test_worker_audits_success_and_handler_failure_with_local_duration(tmp_path):
    async def scenario():
        store = AuditStore(tmp_path / "audit.db")
        contexts = []
        for index, handler in enumerate(
            (FakeHandler(delay=0.01), FakeHandler(error=RuntimeError("secret")))
        ):
            service, _, proposal, decision, _ = service_case(
                handler=handler,
                audit_store=store,
            )
            context = AuditContext(
                str(uuid4()),
                str(uuid4()),
                proposal.call_id,
            )
            contexts.append(context)
            await service.execute(
                proposal_payload(proposal),
                decision_payload(decision),
                None,
                CancellationSource().token,
                audit_context=context,
            )

        records = store.list_recent(limit=2)
        assert {record.status for record in records} == {
            ToolExecutionStatus.SUCCESS,
            ToolExecutionStatus.FAILED,
        }
        assert all(record.duration_ms >= 0 for record in records)
        assert {record.tool_call_id for record in records} == {
            context.tool_call_id for context in contexts
        }
        store.close()

    asyncio.run(scenario())


def test_high_risk_execution_fails_closed_if_audit_write_fails(tmp_path):
    class FailingAuditStore:
        def write(self, **kwargs):
            del kwargs
            raise RuntimeError("database failed")

    async def scenario():
        service, handler, proposal, decision, key = service_case(
            risk=RiskLevel.R2,
            audit_store=FailingAuditStore(),
        )
        token = AuthorizationIssuer(key).issue(decision, ConfirmationMode.VOICE)
        result = await service.execute(
            proposal_payload(proposal),
            decision_payload(decision),
            token,
            CancellationSource().token,
            audit_context=AuditContext(
                str(uuid4()),
                str(uuid4()),
                proposal.call_id,
            ),
        )

        assert len(handler.calls) == 1
        assert result.status is ToolExecutionStatus.FAILED
        assert result.safe_message == "高风险工具审计写入失败"

    asyncio.run(scenario())
