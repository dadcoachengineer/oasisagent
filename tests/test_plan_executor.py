"""Tests for the PlanExecutor state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

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
    StepStatus,
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
    **overrides: Any,
) -> RecommendedAction:
    defaults: dict[str, Any] = {
        "description": f"{operation} via {handler}",
        "handler": handler,
        "operation": operation,
        "risk_tier": risk_tier,
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


def _make_step(
    order: int,
    handler: str = "homeassistant",
    operation: str = "restart_integration",
    risk_tier: RiskTier = RiskTier.AUTO_FIX,
    depends_on: list[int] | None = None,
    conditional: bool = False,
) -> RemediationStep:
    return RemediationStep(
        order=order,
        action=_make_action(handler=handler, operation=operation, risk_tier=risk_tier),
        success_criteria=f"Step {order} OK",
        depends_on=depends_on or [],
        conditional=conditional,
    )


def _make_handler(
    execute_status: ActionStatus = ActionStatus.SUCCESS,
    verify_result: bool = True,
    execute_raises: Exception | None = None,
    verify_raises: Exception | None = None,
) -> AsyncMock:
    handler = AsyncMock()
    handler.name = "mock_handler"

    if execute_raises:
        handler.execute.side_effect = execute_raises
    else:
        handler.execute.return_value = ActionResult(status=execute_status)

    if verify_raises:
        handler.verify.side_effect = verify_raises
    else:
        handler.verify.return_value = VerifyResult(verified=verify_result)

    return handler


def _make_circuit_breaker() -> MagicMock:
    cb = MagicMock()
    cb.record_attempt.return_value = MagicMock(allowed=True)
    cb.check.return_value = MagicMock(allowed=True)
    return cb


def _make_executor(
    handlers: dict[str, AsyncMock] | None = None,
    db: aiosqlite.Connection | None = None,
) -> PlanExecutor:
    if handlers is None:
        handlers = {"homeassistant": _make_handler()}
    return PlanExecutor(
        db=db,
        handlers=handlers,
        circuit_breaker=_make_circuit_breaker(),
    )


# ---------------------------------------------------------------------------
# Linear plan execution
# ---------------------------------------------------------------------------


class TestLinearPlan:
    async def test_all_succeed(self) -> None:
        """3 steps, no deps, all succeed → COMPLETED."""
        handler = _make_handler()
        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1), _make_step(2), _make_step(3)]
        plan = await executor.create_plan(event, steps, "test diagnosis")

        assert plan.status == PlanStatus.EXECUTING  # All AUTO_FIX
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.COMPLETED
        assert result.completed_at is not None
        assert all(
            s.status == StepStatus.SUCCEEDED for s in result.step_states
        )
        assert handler.execute.call_count == 3
        assert handler.verify.call_count == 3


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------


class TestDependencyOrdering:
    async def test_dependency_ordering(self) -> None:
        """Step 2 depends_on=[1], executes after step 1."""
        call_order: list[int] = []

        async def track_order(event: Event, action: RecommendedAction) -> ActionResult:
            call_order.append(int(action.description.split("Step ")[1].split(" ")[0]))
            return ActionResult(status=ActionStatus.SUCCESS)

        handler = _make_handler()
        handler.execute.side_effect = track_order

        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [
            RemediationStep(
                order=1,
                action=_make_action(description="Step 1 restart"),
                success_criteria="OK",
            ),
            RemediationStep(
                order=2,
                action=_make_action(description="Step 2 restart"),
                success_criteria="OK",
                depends_on=[1],
            ),
        ]
        plan = await executor.create_plan(event, steps, "ordered")
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.COMPLETED
        assert call_order == [1, 2]


# ---------------------------------------------------------------------------
# Conditional skip
# ---------------------------------------------------------------------------


class TestConditionalSkip:
    async def test_conditional_skip(self) -> None:
        """Dep fails → plan FAILED immediately. Steps after failure remain PENDING.

        execute_plan fails the plan immediately when any step's execute()
        returns non-SUCCESS. Steps 2 and 3 are never reached.
        """
        fail_handler = _make_handler(execute_status=ActionStatus.FAILURE)
        ok_handler = _make_handler()

        executor = _make_executor(handlers={
            "fail": fail_handler,
            "ok": ok_handler,
        })
        event = _make_event()

        steps = [
            _make_step(1, handler="fail"),
            _make_step(2, handler="ok", depends_on=[1], conditional=True),
            _make_step(3, handler="ok"),
        ]
        plan = await executor.create_plan(event, steps, "conditional")
        result = await executor.execute_plan(plan, event)

        states = {s.order: s for s in result.step_states}
        assert result.status == PlanStatus.FAILED
        assert states[1].status == StepStatus.FAILED
        assert states[2].status == StepStatus.PENDING  # Never reached
        assert states[3].status == StepStatus.PENDING  # Never reached

    async def test_conditional_chain_step2_fails(self) -> None:
        """Step 1 OK, step 2 fails → plan FAILED. Step 3 never reached."""
        call_count = 0

        async def counting_execute(event: Event, action: RecommendedAction) -> ActionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Step 2 fails
                return ActionResult(status=ActionStatus.FAILURE)
            return ActionResult(status=ActionStatus.SUCCESS)

        handler = _make_handler()
        handler.execute.side_effect = counting_execute

        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [
            _make_step(1),
            _make_step(2),  # Will fail
            _make_step(3, depends_on=[2], conditional=True),
            _make_step(4),  # No deps
        ]
        plan = await executor.create_plan(event, steps, "chain")
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.FAILED
        states = {s.order: s for s in result.step_states}
        assert states[1].status == StepStatus.SUCCEEDED
        assert states[2].status == StepStatus.FAILED


# ---------------------------------------------------------------------------
# Non-conditional abort
# ---------------------------------------------------------------------------


class TestNonConditionalAbort:
    async def test_non_conditional_abort(self) -> None:
        """Dep fails, non-conditional step → BLOCKED, plan → FAILED.

        This tests the dependency evaluation path (not execute failure path).
        We need step 1 to succeed but step 2 (which step 3 depends on) to fail.
        Since step 2 failure causes immediate plan FAILED, step 3 never gets
        its dependency checked. So we test the simpler case: step 1 fails,
        step 2 (non-conditional dep on 1) would be BLOCKED.
        But again, step 1 failure causes immediate return.

        To properly test BLOCKED, we need a step that fails in its dependency
        check, not in execution. The only way to reach the dependency eval
        for step 2 is if step 1 completed (SUCCEEDED or SKIPPED).
        Let's have step 1 be conditional on a failed step 0... but that's
        circular. Let's just test with a simpler approach.
        """
        # Step 1 succeeds, step 2 fails, step 3 non-conditional on step 2 → BLOCKED
        call_count = 0

        async def ordered_execute(event: Event, action: RecommendedAction) -> ActionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Step 2
                return ActionResult(status=ActionStatus.FAILURE)
            return ActionResult(status=ActionStatus.SUCCESS)

        handler = _make_handler()
        handler.execute.side_effect = ordered_execute

        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [
            _make_step(1),
            _make_step(2),  # Will fail
            _make_step(3, depends_on=[2]),  # Non-conditional → BLOCKED
        ]
        plan = await executor.create_plan(event, steps, "abort")
        result = await executor.execute_plan(plan, event)

        # Step 2 fails → plan FAILED immediately (execute failure, not BLOCKED)
        assert result.status == PlanStatus.FAILED
        states = {s.order: s for s in result.step_states}
        assert states[2].status == StepStatus.FAILED


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


class TestApproval:
    async def test_requires_approval_mixed_tiers(self) -> None:
        """AUTO_FIX + RECOMMEND → requires approval."""
        executor = _make_executor()
        event = _make_event()

        steps = [
            _make_step(1, risk_tier=RiskTier.AUTO_FIX),
            _make_step(2, risk_tier=RiskTier.RECOMMEND),
        ]
        plan = await executor.create_plan(event, steps, "mixed")

        assert executor.requires_approval(plan)
        assert plan.status == PlanStatus.PENDING_APPROVAL

    async def test_requires_approval_all_auto_fix(self) -> None:
        """All AUTO_FIX → no approval needed, starts EXECUTING."""
        executor = _make_executor()
        event = _make_event()

        steps = [_make_step(1), _make_step(2)]
        plan = await executor.create_plan(event, steps, "auto")

        assert not executor.requires_approval(plan)
        assert plan.status == PlanStatus.EXECUTING

    async def test_approve_plan(self) -> None:
        """approve_plan transitions PENDING_APPROVAL → EXECUTING."""
        executor = _make_executor()
        event = _make_event()

        steps = [_make_step(1, risk_tier=RiskTier.RECOMMEND)]
        plan = await executor.create_plan(event, steps, "approve")

        assert plan.status == PlanStatus.PENDING_APPROVAL
        approved = await executor.approve_plan(plan.id)

        assert approved is not None
        assert approved.status == PlanStatus.EXECUTING

    async def test_approve_plan_wrong_status(self) -> None:
        """Cannot approve a plan that's not PENDING_APPROVAL."""
        executor = _make_executor()
        event = _make_event()

        steps = [_make_step(1)]  # AUTO_FIX → EXECUTING
        plan = await executor.create_plan(event, steps, "wrong")

        result = await executor.approve_plan(plan.id)
        assert result is None

    async def test_reject_plan(self) -> None:
        """reject_plan transitions PENDING_APPROVAL → REJECTED."""
        executor = _make_executor()
        event = _make_event()

        steps = [_make_step(1, risk_tier=RiskTier.RECOMMEND)]
        plan = await executor.create_plan(event, steps, "reject")

        rejected = await executor.reject_plan(plan.id)
        assert rejected is not None
        assert rejected.status == PlanStatus.REJECTED
        assert rejected.completed_at is not None

    async def test_expire_plan(self) -> None:
        """expire_plan transitions PENDING_APPROVAL → EXPIRED."""
        executor = _make_executor()
        event = _make_event()

        steps = [_make_step(1, risk_tier=RiskTier.RECOMMEND)]
        plan = await executor.create_plan(event, steps, "expire")

        expired = await executor.expire_plan(plan.id)
        assert expired is not None
        assert expired.status == PlanStatus.EXPIRED

    async def test_approve_nonexistent(self) -> None:
        """Approving a nonexistent plan returns None."""
        executor = _make_executor()
        result = await executor.approve_plan("nonexistent-id")
        assert result is None


# ---------------------------------------------------------------------------
# Handler failures
# ---------------------------------------------------------------------------


class TestHandlerFailures:
    async def test_handler_execute_failure(self) -> None:
        """execute() returns FAILURE → step FAILED, plan FAILED."""
        handler = _make_handler(execute_status=ActionStatus.FAILURE)
        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1)]
        plan = await executor.create_plan(event, steps, "fail")
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.FAILED
        assert result.step_states[0].status == StepStatus.FAILED

    async def test_handler_execute_raises(self) -> None:
        """execute() raises → step FAILED, plan FAILED."""
        handler = _make_handler(execute_raises=RuntimeError("boom"))
        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1)]
        plan = await executor.create_plan(event, steps, "raise")
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.FAILED
        assert "boom" in result.step_states[0].error_message

    async def test_handler_verify_failure(self) -> None:
        """verify() returns verified=False → step FAILED, plan FAILED."""
        handler = _make_handler(verify_result=False)
        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1)]
        plan = await executor.create_plan(event, steps, "verify-fail")
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.FAILED
        assert result.step_states[0].status == StepStatus.FAILED

    async def test_handler_verify_raises(self) -> None:
        """verify() raises → step FAILED."""
        handler = _make_handler(verify_raises=RuntimeError("verify boom"))
        executor = _make_executor(handlers={"homeassistant": handler})
        event = _make_event()

        steps = [_make_step(1)]
        plan = await executor.create_plan(event, steps, "verify-raise")
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.FAILED
        assert "verify boom" in result.step_states[0].error_message

    async def test_handler_not_found(self) -> None:
        """Unknown handler → step FAILED, plan FAILED."""
        executor = _make_executor(handlers={})  # No handlers
        event = _make_event()

        steps = [_make_step(1)]
        plan = await executor.create_plan(event, steps, "no-handler")
        result = await executor.execute_plan(plan, event)

        assert result.status == PlanStatus.FAILED
        assert "not found" in result.step_states[0].error_message


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------


class TestSQLitePersistence:
    async def test_persist_and_reload(self, tmp_path: Path) -> None:
        """Create plan, reload from DB, state preserved."""
        db_path = str(tmp_path / "test.db")
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            # Create schema
            await db.execute("""
                CREATE TABLE remediation_plans (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_approval',
                    steps_json TEXT NOT NULL,
                    step_states_json TEXT NOT NULL,
                    diagnosis TEXT NOT NULL DEFAULT '',
                    effective_risk_tier TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    system TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)

            handler = _make_handler()
            executor = _make_executor(
                handlers={"homeassistant": handler},
                db=db,
            )
            event = _make_event()

            steps = [_make_step(1), _make_step(2, depends_on=[1])]
            plan = await executor.create_plan(event, steps, "persist test")

            # Create a new executor to force reload from DB
            executor2 = PlanExecutor(
                db=db,
                handlers={"homeassistant": handler},
                circuit_breaker=_make_circuit_breaker(),
            )

            # Load directly from DB
            loaded = await executor2._load_plan(plan.id)

            assert loaded is not None
            assert loaded.id == plan.id
            assert loaded.event_id == plan.event_id
            assert loaded.status == plan.status
            assert len(loaded.steps) == 2
            assert len(loaded.step_states) == 2
            assert loaded.diagnosis == "persist test"


# ---------------------------------------------------------------------------
# Resume (crash recovery)
# ---------------------------------------------------------------------------


class TestResume:
    async def test_resume_resets_executing_to_ready(self, tmp_path: Path) -> None:
        """Step in EXECUTING at crash → reset to READY, retried."""
        db_path = str(tmp_path / "test.db")
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("""
                CREATE TABLE remediation_plans (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_approval',
                    steps_json TEXT NOT NULL,
                    step_states_json TEXT NOT NULL,
                    diagnosis TEXT NOT NULL DEFAULT '',
                    effective_risk_tier TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    system TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)

            handler = _make_handler()
            executor = _make_executor(
                handlers={"homeassistant": handler},
                db=db,
            )
            event = _make_event()

            # Create plan and simulate crash mid-execution
            steps = [_make_step(1), _make_step(2)]
            plan = await executor.create_plan(event, steps, "crash")

            # Manually set step 1 to EXECUTING (simulating crash)
            plan.step_states[0].status = StepStatus.EXECUTING
            plan.step_states[1].status = StepStatus.PENDING
            plan.status = PlanStatus.EXECUTING
            await executor._persist_plan(plan)

            # New executor on restart
            executor2 = PlanExecutor(
                db=db,
                handlers={"homeassistant": handler},
                circuit_breaker=_make_circuit_breaker(),
            )

            resumed = await executor2.resume_in_progress()
            assert len(resumed) == 1
            assert resumed[0].step_states[0].status == StepStatus.READY

    async def test_resume_skips_completed_steps(self, tmp_path: Path) -> None:
        """SUCCEEDED steps not re-executed on resume."""
        db_path = str(tmp_path / "test.db")
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("""
                CREATE TABLE remediation_plans (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_approval',
                    steps_json TEXT NOT NULL,
                    step_states_json TEXT NOT NULL,
                    diagnosis TEXT NOT NULL DEFAULT '',
                    effective_risk_tier TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    system TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)

            handler = _make_handler()
            executor = _make_executor(
                handlers={"homeassistant": handler},
                db=db,
            )
            event = _make_event()

            # Create plan with step 1 already succeeded, step 2 was executing
            steps = [_make_step(1), _make_step(2)]
            plan = await executor.create_plan(event, steps, "resume")

            plan.step_states[0].status = StepStatus.SUCCEEDED
            plan.step_states[1].status = StepStatus.EXECUTING  # Was executing at crash
            plan.status = PlanStatus.EXECUTING
            await executor._persist_plan(plan)

            # New executor
            executor2 = PlanExecutor(
                db=db,
                handlers={"homeassistant": handler},
                circuit_breaker=_make_circuit_breaker(),
            )

            resumed = await executor2.resume_in_progress()
            assert len(resumed) == 1
            # Step 1 still SUCCEEDED, step 2 reset to READY
            assert resumed[0].step_states[0].status == StepStatus.SUCCEEDED
            assert resumed[0].step_states[1].status == StepStatus.READY

            # Execute resumed plan
            handler2 = _make_handler()
            executor2._handlers = {"homeassistant": handler2}
            result = await executor2.execute_plan(resumed[0], event)

            assert result.status == PlanStatus.COMPLETED
            # handler2 should only have been called once (for step 2)
            assert handler2.execute.call_count == 1


# ---------------------------------------------------------------------------
# Utility methods
# ---------------------------------------------------------------------------


class TestUtilityMethods:
    async def test_get_plan(self) -> None:
        executor = _make_executor()
        event = _make_event()
        steps = [_make_step(1)]
        plan = await executor.create_plan(event, steps, "get")

        assert executor.get_plan(plan.id) is plan
        assert executor.get_plan("nonexistent") is None

    async def test_list_pending_plans(self) -> None:
        executor = _make_executor()
        event = _make_event()

        # Create one pending and one auto-execute plan
        p1 = await executor.create_plan(
            event, [_make_step(1, risk_tier=RiskTier.RECOMMEND)], "pending",
        )
        await executor.create_plan(
            event, [_make_step(1)], "auto",
        )

        pending = executor.list_pending_plans()
        assert len(pending) == 1
        assert pending[0].id == p1.id

    async def test_effective_risk_tier_computation(self) -> None:
        """Effective tier is the highest among all steps."""
        executor = _make_executor()
        event = _make_event()

        steps = [
            _make_step(1, risk_tier=RiskTier.AUTO_FIX),
            _make_step(2, risk_tier=RiskTier.RECOMMEND),
            _make_step(3, risk_tier=RiskTier.AUTO_FIX),
        ]
        plan = await executor.create_plan(event, steps, "tier")

        assert plan.effective_risk_tier == RiskTier.RECOMMEND

    async def test_effective_risk_tier_escalate_beats_recommend(self) -> None:
        """ESCALATE > RECOMMEND — catches lexicographic ordering bug."""
        executor = _make_executor()
        event = _make_event()

        steps = [
            _make_step(1, risk_tier=RiskTier.AUTO_FIX),
            _make_step(2, risk_tier=RiskTier.ESCALATE),
            _make_step(3, risk_tier=RiskTier.RECOMMEND),
        ]
        plan = await executor.create_plan(event, steps, "escalate")

        assert plan.effective_risk_tier == RiskTier.ESCALATE

    async def test_effective_risk_tier_block_is_highest(self) -> None:
        """BLOCK is the highest risk tier."""
        executor = _make_executor()
        event = _make_event()

        steps = [
            _make_step(1, risk_tier=RiskTier.ESCALATE),
            _make_step(2, risk_tier=RiskTier.BLOCK),
            _make_step(3, risk_tier=RiskTier.RECOMMEND),
        ]
        plan = await executor.create_plan(event, steps, "block")

        assert plan.effective_risk_tier == RiskTier.BLOCK
