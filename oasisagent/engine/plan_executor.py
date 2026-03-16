"""Plan executor — state machine for multi-step remediation plans.

Receives a RemediationPlan, dispatches steps sequentially via handlers
in topological order, tracks step-level state, and persists transitions
to SQLite. Integrates with the circuit breaker for per-entity attempt
tracking.

Design decisions (from P2 plan):
- D4: Sequential execution only — no parallel steps in P2
- D3: handler.verify() only — success_criteria stored in audit, not evaluated
- D6: No APPROVED status — approve_plan() goes PENDING_APPROVAL → EXECUTING
- D8: Steps in EXECUTING at crash are reset to READY (idempotent retry)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from oasisagent.models import (
    ActionStatus,
    PlanStatus,
    RemediationPlan,
    RiskTier,
    StepState,
    StepStatus,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite

    from oasisagent.engine.circuit_breaker import CircuitBreaker
    from oasisagent.handlers.base import Handler
    from oasisagent.models import Event, RemediationStep

logger = logging.getLogger(__name__)

# Explicit risk ordering — StrEnum comparison is lexicographic (wrong),
# so we define the actual severity progression for max() comparisons.
_RISK_ORDER: dict[RiskTier, int] = {
    RiskTier.AUTO_FIX: 0,
    RiskTier.RECOMMEND: 1,
    RiskTier.ESCALATE: 2,
    RiskTier.BLOCK: 3,
}


class PlanExecutor:
    """Executes multi-step remediation plans with dependency ordering.

    The executor:
    1. Creates plans from T2 remediation steps
    2. Determines whether whole-plan approval is needed
    3. Runs the state machine loop: step dispatch → verify → cascade
    4. Persists every state transition to SQLite
    5. Resumes in-progress plans on restart
    """

    def __init__(
        self,
        db: aiosqlite.Connection | None,
        handlers: dict[str, Handler],
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self._db = db
        self._handlers = handlers
        self._circuit_breaker = circuit_breaker
        self._plans: dict[str, RemediationPlan] = {}
        self.on_plan_completed: Callable[[RemediationPlan], Awaitable[None]] | None = None

    async def create_plan(
        self,
        event: Event,
        steps: list[RemediationStep],
        diagnosis: str,
    ) -> RemediationPlan:
        """Create a new plan from T2 remediation steps.

        Initializes step states to PENDING and determines the effective
        risk tier (highest tier among all steps).
        """

        effective_tier = self._compute_effective_risk_tier(steps)

        plan = RemediationPlan(
            event_id=event.id,
            steps=steps,
            step_states=[StepState(order=s.order) for s in steps],
            diagnosis=diagnosis,
            effective_risk_tier=effective_tier,
            entity_id=event.entity_id,
            severity=event.severity.value,
            source=event.source,
            system=event.system,
        )

        # Auto-execute plans skip PENDING_APPROVAL
        if not self.requires_approval(plan):
            plan.status = PlanStatus.EXECUTING

        await self._persist_plan(plan)
        self._plans[plan.id] = plan

        logger.info(
            "Created plan %s for event %s: %d steps, risk=%s, status=%s",
            plan.id, event.id, len(steps), effective_tier, plan.status,
        )
        return plan

    def requires_approval(self, plan: RemediationPlan) -> bool:
        """True if any step has risk_tier != AUTO_FIX."""
        return any(
            step.action.risk_tier != RiskTier.AUTO_FIX
            for step in plan.steps
        )

    async def execute_plan(
        self,
        plan: RemediationPlan,
        event: Event,
    ) -> RemediationPlan:
        """Run the state machine loop. Returns the plan in terminal state.

        Steps in EXECUTING at resume are reset to READY and retried.
        This is safe because primary handler operations (restart_integration,
        restart_container, reload_automations) are idempotent.
        """
        plan.status = PlanStatus.EXECUTING
        plan.updated_at = datetime.now(UTC)
        await self._persist_plan(plan)

        # Reset any EXECUTING steps to READY (crash recovery, D8)
        for state in plan.step_states:
            if state.status == StepStatus.EXECUTING:
                logger.info(
                    "Plan %s step %d: resetting EXECUTING → READY (idempotent retry)",
                    plan.id, state.order,
                )
                state.status = StepStatus.READY

        # Execute steps in topological order
        sorted_steps = sorted(plan.steps, key=lambda s: s.order)
        state_by_order = {s.order: s for s in plan.step_states}

        for step in sorted_steps:
            state = state_by_order[step.order]

            # Skip already-completed steps (resume scenario)
            if state.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED):
                continue

            # Evaluate dependency readiness
            dep_result = self._evaluate_dependencies(step, state_by_order)
            if dep_result == StepStatus.BLOCKED:
                state.status = StepStatus.BLOCKED
                state.completed_at = datetime.now(UTC)
                state.error_message = "Non-conditional dependency failed"
                plan.status = PlanStatus.FAILED
                plan.updated_at = datetime.now(UTC)
                plan.completed_at = datetime.now(UTC)
                await self._persist_plan(plan)
                logger.info(
                    "Plan %s FAILED: step %d BLOCKED by failed dependency",
                    plan.id, step.order,
                )
                return plan

            if dep_result == StepStatus.SKIPPED:
                state.status = StepStatus.SKIPPED
                state.completed_at = datetime.now(UTC)
                await self._persist_plan(plan)
                logger.info(
                    "Plan %s step %d: SKIPPED (conditional dep failed)",
                    plan.id, step.order,
                )
                continue

            # Step is READY — execute
            state.status = StepStatus.EXECUTING
            state.started_at = datetime.now(UTC)
            await self._persist_plan(plan)

            handler = self._handlers.get(step.action.handler)
            if handler is None:
                state.status = StepStatus.FAILED
                state.completed_at = datetime.now(UTC)
                state.error_message = f"Handler '{step.action.handler}' not found"
                plan.status = PlanStatus.FAILED
                plan.updated_at = datetime.now(UTC)
                plan.completed_at = datetime.now(UTC)
                await self._persist_plan(plan)
                logger.error(
                    "Plan %s FAILED: handler '%s' not found for step %d",
                    plan.id, step.action.handler, step.order,
                )
                return plan

            try:
                action_result = await handler.execute(event, step.action)
                state.action_result = action_result
            except Exception as exc:
                state.status = StepStatus.FAILED
                state.completed_at = datetime.now(UTC)
                state.error_message = str(exc)
                plan.status = PlanStatus.FAILED
                plan.updated_at = datetime.now(UTC)
                plan.completed_at = datetime.now(UTC)
                await self._persist_plan(plan)
                logger.error(
                    "Plan %s FAILED: step %d execute() raised: %s",
                    plan.id, step.order, exc,
                )
                return plan

            # Record circuit breaker attempt
            cb_entity = step.action.target_entity_id or event.entity_id
            self._circuit_breaker.record_attempt(
                cb_entity,
                success=(action_result.status == ActionStatus.SUCCESS),
            )

            if action_result.status != ActionStatus.SUCCESS:
                state.status = StepStatus.FAILED
                state.completed_at = datetime.now(UTC)
                state.error_message = (
                    action_result.error_message or "Action returned non-success status"
                )
                plan.status = PlanStatus.FAILED
                plan.updated_at = datetime.now(UTC)
                plan.completed_at = datetime.now(UTC)
                await self._persist_plan(plan)
                logger.info(
                    "Plan %s FAILED: step %d returned status=%s",
                    plan.id, step.order, action_result.status,
                )
                return plan

            # Verify
            state.status = StepStatus.VERIFYING
            await self._persist_plan(plan)

            try:
                verify_result = await handler.verify(event, step.action, action_result)
                state.verify_result = verify_result
            except Exception as exc:
                state.status = StepStatus.FAILED
                state.completed_at = datetime.now(UTC)
                state.error_message = f"verify() raised: {exc}"
                plan.status = PlanStatus.FAILED
                plan.updated_at = datetime.now(UTC)
                plan.completed_at = datetime.now(UTC)
                await self._persist_plan(plan)
                logger.error(
                    "Plan %s FAILED: step %d verify() raised: %s",
                    plan.id, step.order, exc,
                )
                return plan

            if not verify_result.verified:
                state.status = StepStatus.FAILED
                state.completed_at = datetime.now(UTC)
                state.error_message = verify_result.message or "Verification failed"
                plan.status = PlanStatus.FAILED
                plan.updated_at = datetime.now(UTC)
                plan.completed_at = datetime.now(UTC)
                await self._persist_plan(plan)
                logger.info(
                    "Plan %s FAILED: step %d verification failed: %s",
                    plan.id, step.order, verify_result.message,
                )
                return plan

            # Step succeeded
            state.status = StepStatus.SUCCEEDED
            state.completed_at = datetime.now(UTC)
            await self._persist_plan(plan)
            logger.info(
                "Plan %s step %d: SUCCEEDED",
                plan.id, step.order,
            )

        # All steps completed (succeeded or skipped)
        plan.status = PlanStatus.COMPLETED
        plan.updated_at = datetime.now(UTC)
        plan.completed_at = datetime.now(UTC)
        await self._persist_plan(plan)
        logger.info("Plan %s COMPLETED", plan.id)

        if self.on_plan_completed is not None:
            try:
                await self.on_plan_completed(plan)
            except Exception:
                logger.warning(
                    "on_plan_completed callback failed for plan %s",
                    plan.id, exc_info=True,
                )

        return plan

    async def approve_plan(self, plan_id: str) -> RemediationPlan | None:
        """CAS: PENDING_APPROVAL → EXECUTING. Returns None if race/not found."""
        plan = self._plans.get(plan_id)
        if plan is None:
            plan = await self._load_plan(plan_id)

        if plan is None or plan.status != PlanStatus.PENDING_APPROVAL:
            return None

        plan.status = PlanStatus.EXECUTING
        plan.updated_at = datetime.now(UTC)
        await self._persist_plan(plan)
        self._plans[plan_id] = plan

        logger.info("Plan %s approved → EXECUTING", plan_id)
        return plan

    async def reject_plan(self, plan_id: str) -> RemediationPlan | None:
        """CAS: PENDING_APPROVAL → REJECTED. Returns None if race/not found."""
        plan = self._plans.get(plan_id)
        if plan is None:
            plan = await self._load_plan(plan_id)

        if plan is None or plan.status != PlanStatus.PENDING_APPROVAL:
            return None

        plan.status = PlanStatus.REJECTED
        plan.updated_at = datetime.now(UTC)
        plan.completed_at = datetime.now(UTC)
        await self._persist_plan(plan)
        self._plans[plan_id] = plan

        logger.info("Plan %s rejected", plan_id)
        return plan

    async def expire_plan(self, plan_id: str) -> RemediationPlan | None:
        """CAS: PENDING_APPROVAL → EXPIRED. Returns None if race/not found."""
        plan = self._plans.get(plan_id)
        if plan is None:
            plan = await self._load_plan(plan_id)

        if plan is None or plan.status != PlanStatus.PENDING_APPROVAL:
            return None

        plan.status = PlanStatus.EXPIRED
        plan.updated_at = datetime.now(UTC)
        plan.completed_at = datetime.now(UTC)
        await self._persist_plan(plan)
        self._plans[plan_id] = plan

        logger.info("Plan %s expired", plan_id)
        return plan

    async def resume_in_progress(self) -> list[RemediationPlan]:
        """Load EXECUTING plans from SQLite on restart.

        Steps in EXECUTING state are reset to READY (idempotent retry, D8).
        """
        if self._db is None:
            return []

        cursor = await self._db.execute(
            "SELECT * FROM remediation_plans WHERE status = ?",
            (PlanStatus.EXECUTING,),
        )
        rows = await cursor.fetchall()

        plans: list[RemediationPlan] = []
        for row in rows:
            plan = self._row_to_plan(row)
            if plan is not None:
                # Reset EXECUTING steps to READY (D8)
                for state in plan.step_states:
                    if state.status == StepStatus.EXECUTING:
                        state.status = StepStatus.READY
                self._plans[plan.id] = plan
                plans.append(plan)

        if plans:
            logger.info("Resumed %d in-progress plans", len(plans))
        return plans

    def get_plan(self, plan_id: str) -> RemediationPlan | None:
        """Get a plan from the in-memory cache."""
        return self._plans.get(plan_id)

    def list_pending_plans(self) -> list[RemediationPlan]:
        """List all plans awaiting approval."""
        return [
            p for p in self._plans.values()
            if p.status == PlanStatus.PENDING_APPROVAL
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate_dependencies(
        self,
        step: RemediationStep,
        state_by_order: dict[int, StepState],
    ) -> StepStatus:
        """Evaluate dependency readiness for a step.

        Returns:
            READY if all deps are met
            SKIPPED if a failed dep is conditional
            BLOCKED if a failed dep is non-conditional
        """
        if not step.depends_on:
            return StepStatus.READY

        for dep_order in step.depends_on:
            dep_state = state_by_order.get(dep_order)
            if dep_state is None:
                return StepStatus.BLOCKED

            if dep_state.status == StepStatus.SUCCEEDED:
                continue

            if dep_state.status == StepStatus.SKIPPED:
                continue

            # Dep failed (FAILED, BLOCKED, or not yet completed)
            if dep_state.status in (StepStatus.FAILED, StepStatus.BLOCKED):
                if step.conditional:
                    return StepStatus.SKIPPED
                return StepStatus.BLOCKED

            # Dep not yet reached — shouldn't happen in sequential execution
            return StepStatus.BLOCKED

        return StepStatus.READY

    @staticmethod
    def _compute_effective_risk_tier(steps: list[RemediationStep]) -> RiskTier:
        """Compute the highest risk tier among all steps."""
        return max(
            (s.action.risk_tier for s in steps),
            key=lambda t: _RISK_ORDER[t],
            default=RiskTier.AUTO_FIX,
        )

    async def _persist_plan(self, plan: RemediationPlan) -> None:
        """Write the plan to SQLite. Upsert (INSERT OR REPLACE)."""
        if self._db is None:
            return

        steps_json = json.dumps([s.model_dump(mode="json") for s in plan.steps])
        states_json = json.dumps([s.model_dump(mode="json") for s in plan.step_states])

        await self._db.execute(
            """
            INSERT OR REPLACE INTO remediation_plans (
                id, event_id, status, steps_json, step_states_json,
                diagnosis, effective_risk_tier, entity_id, severity,
                source, system, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.id,
                plan.event_id,
                plan.status.value,
                steps_json,
                states_json,
                plan.diagnosis,
                plan.effective_risk_tier.value,
                plan.entity_id,
                plan.severity,
                plan.source,
                plan.system,
                plan.created_at.isoformat(),
                plan.updated_at.isoformat(),
                plan.completed_at.isoformat() if plan.completed_at else None,
            ),
        )
        await self._db.commit()

    async def _load_plan(self, plan_id: str) -> RemediationPlan | None:
        """Load a single plan from SQLite."""
        if self._db is None:
            return None

        cursor = await self._db.execute(
            "SELECT * FROM remediation_plans WHERE id = ?",
            (plan_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        plan = self._row_to_plan(row)
        if plan is not None:
            self._plans[plan.id] = plan
        return plan

    @staticmethod
    def _row_to_plan(row: aiosqlite.Row) -> RemediationPlan | None:
        """Convert a SQLite row to a RemediationPlan."""
        from oasisagent.models import RemediationStep, RiskTier

        try:
            steps = [
                RemediationStep.model_validate(s)
                for s in json.loads(row["steps_json"])
            ]
            step_states = [
                StepState.model_validate(s)
                for s in json.loads(row["step_states_json"])
            ]
            return RemediationPlan(
                id=row["id"],
                event_id=row["event_id"],
                status=PlanStatus(row["status"]),
                steps=steps,
                step_states=step_states,
                diagnosis=row["diagnosis"],
                effective_risk_tier=RiskTier(row["effective_risk_tier"]),
                entity_id=row["entity_id"],
                severity=row["severity"],
                source=row["source"],
                system=row["system"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                completed_at=(
                    datetime.fromisoformat(row["completed_at"])
                    if row["completed_at"]
                    else None
                ),
            )
        except Exception:
            logger.warning("Failed to deserialize plan %s", row["id"])
            return None
