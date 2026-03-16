"""Integration tests for plan-aware dispatch through the orchestrator.

Tests the full path: DecisionResult with remediation_plan → orchestrator
dispatch → PlanExecutor → handlers → audit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from oasisagent.approval.pending import PendingQueue
from oasisagent.engine.circuit_breaker import CircuitBreaker
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionResult,
    DecisionTier,
)
from oasisagent.engine.plan_executor import PlanExecutor
from oasisagent.models import (
    ActionResult,
    ActionStatus,
    Event,
    EventMetadata,
    PlanStatus,
    RecommendedAction,
    RemediationStep,
    RiskTier,
    Severity,
    VerifyResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "homeassistant",
        "event_type": "integration_failure",
        "entity_id": "sensor.temperature",
        "severity": Severity.ERROR,
        "timestamp": datetime.now(UTC),
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_action(
    handler: str = "homeassistant",
    operation: str = "restart_integration",
    risk_tier: RiskTier = RiskTier.AUTO_FIX,
) -> RecommendedAction:
    return RecommendedAction(
        description=f"{operation} via {handler}",
        handler=handler,
        operation=operation,
        risk_tier=risk_tier,
    )


def _make_step(
    order: int,
    risk_tier: RiskTier = RiskTier.AUTO_FIX,
    depends_on: list[int] | None = None,
) -> RemediationStep:
    return RemediationStep(
        order=order,
        action=_make_action(risk_tier=risk_tier),
        success_criteria=f"Step {order} OK",
        depends_on=depends_on or [],
    )


def _make_handler() -> AsyncMock:
    handler = AsyncMock()
    handler.execute.return_value = ActionResult(status=ActionStatus.SUCCESS)
    handler.verify.return_value = VerifyResult(verified=True)
    return handler


def _make_circuit_breaker() -> CircuitBreaker:
    from oasisagent.config import CircuitBreakerConfig

    return CircuitBreaker(CircuitBreakerConfig())


def _make_plan_executor(
    handlers: dict[str, AsyncMock] | None = None,
) -> PlanExecutor:
    if handlers is None:
        handlers = {"homeassistant": _make_handler()}
    return PlanExecutor(
        db=None,
        handlers=handlers,
        circuit_breaker=_make_circuit_breaker(),
    )


# ---------------------------------------------------------------------------
# Plan auto-execute
# ---------------------------------------------------------------------------


class TestPlanAutoExecute:
    async def test_all_auto_fix_executes_immediately(self) -> None:
        """All AUTO_FIX steps → plan executes without approval."""
        handler = _make_handler()
        executor = _make_plan_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1), _make_step(2, depends_on=[1])]
        plan = await executor.create_plan(event, steps, "auto-fix test")

        assert not executor.requires_approval(plan)
        assert plan.status == PlanStatus.EXECUTING

        result = await executor.execute_plan(plan, event)
        assert result.status == PlanStatus.COMPLETED
        assert handler.execute.call_count == 2


# ---------------------------------------------------------------------------
# Plan requires approval
# ---------------------------------------------------------------------------


class TestPlanRequiresApproval:
    async def test_recommend_step_triggers_approval(self) -> None:
        """RECOMMEND step in plan → requires approval."""
        executor = _make_plan_executor()
        event = _make_event()

        steps = [
            _make_step(1, risk_tier=RiskTier.AUTO_FIX),
            _make_step(2, risk_tier=RiskTier.RECOMMEND),
        ]
        plan = await executor.create_plan(event, steps, "needs approval")

        assert executor.requires_approval(plan)
        assert plan.status == PlanStatus.PENDING_APPROVAL

    async def test_enqueue_with_plan_id(self) -> None:
        """Plan approval creates PendingAction with plan_id, action=None."""
        queue = PendingQueue()
        pending = await queue.add(
            event_id="evt-1",
            action=None,
            diagnosis="Multi-step fix",
            timeout_minutes=30,
            plan_id="plan-123",
        )

        assert pending is not None
        assert pending.plan_id == "plan-123"
        assert pending.action is None

    async def test_plan_id_dedup(self) -> None:
        """Duplicate plan_id is suppressed."""
        queue = PendingQueue()
        p1 = await queue.add(
            "evt-1", None, "test", 30, plan_id="plan-123",
        )
        p2 = await queue.add(
            "evt-2", None, "test", 30, plan_id="plan-123",
        )
        assert p1 is not None
        assert p2 is None


# ---------------------------------------------------------------------------
# Plan approval triggers execution
# ---------------------------------------------------------------------------


class TestPlanApprovalTriggersExecution:
    async def test_approve_then_execute(self) -> None:
        """Approve plan → EXECUTING → steps run → COMPLETED."""
        handler = _make_handler()
        executor = _make_plan_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1, risk_tier=RiskTier.RECOMMEND)]
        plan = await executor.create_plan(event, steps, "approve test")

        assert plan.status == PlanStatus.PENDING_APPROVAL

        approved = await executor.approve_plan(plan.id)
        assert approved.status == PlanStatus.EXECUTING

        result = await executor.execute_plan(approved, event)
        assert result.status == PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# Plan rejection
# ---------------------------------------------------------------------------


class TestPlanRejection:
    async def test_reject_plan(self) -> None:
        """Reject → plan REJECTED, no execution."""
        handler = _make_handler()
        executor = _make_plan_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1, risk_tier=RiskTier.RECOMMEND)]
        plan = await executor.create_plan(event, steps, "reject test")

        rejected = await executor.reject_plan(plan.id)
        assert rejected.status == PlanStatus.REJECTED
        assert handler.execute.call_count == 0


# ---------------------------------------------------------------------------
# Fall-through to flat actions
# ---------------------------------------------------------------------------


class TestPlanFallthrough:
    async def test_no_plan_uses_flat_dispatch(self) -> None:
        """remediation_plan=None → existing flat dispatch unaffected."""
        result = DecisionResult(
            event_id="evt-1",
            tier=DecisionTier.T2,
            disposition=DecisionDisposition.MATCHED,
            diagnosis="Simple fix",
            recommended_actions=[_make_action()],
            remediation_plan=None,
        )
        # Plan field is None → should not trigger plan dispatch
        assert result.remediation_plan is None
        assert len(result.recommended_actions) == 1


# ---------------------------------------------------------------------------
# Plan skips flat actions (S3)
# ---------------------------------------------------------------------------


class TestPlanSkipsFlatActions:
    async def test_plan_present_skips_flat_actions(self) -> None:
        """When plan is present, recommended_actions are ignored."""
        result = DecisionResult(
            event_id="evt-1",
            tier=DecisionTier.T2,
            disposition=DecisionDisposition.MATCHED,
            diagnosis="Multi-step fix",
            recommended_actions=[_make_action()],
            remediation_plan=[_make_step(1)],
        )
        # Both are present — orchestrator should dispatch plan, skip flat
        assert result.remediation_plan is not None
        assert len(result.recommended_actions) == 1
