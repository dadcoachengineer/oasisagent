"""Tests for PlanStatus, StepStatus, StepState, and RemediationPlan models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from oasisagent.models import (
    ActionResult,
    ActionStatus,
    PlanStatus,
    RecommendedAction,
    RemediationPlan,
    RemediationStep,
    RiskTier,
    StepState,
    StepStatus,
    VerifyResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(**overrides: Any) -> RecommendedAction:
    defaults = {
        "description": "Restart ZWave integration",
        "handler": "homeassistant",
        "operation": "restart_integration",
        "risk_tier": RiskTier.AUTO_FIX,
    }
    defaults.update(overrides)
    return RecommendedAction(**defaults)


def _make_step(order: int = 1, **overrides: Any) -> RemediationStep:
    defaults = {
        "order": order,
        "action": _make_action(),
        "success_criteria": "Integration available",
    }
    defaults.update(overrides)
    return RemediationStep(**defaults)


def _make_plan(**overrides: Any) -> RemediationPlan:
    defaults = {
        "event_id": "evt-123",
        "steps": [_make_step(1), _make_step(2, depends_on=[1])],
        "effective_risk_tier": RiskTier.AUTO_FIX,
    }
    defaults.update(overrides)
    return RemediationPlan(**defaults)


# ---------------------------------------------------------------------------
# PlanStatus enum
# ---------------------------------------------------------------------------


class TestPlanStatus:
    def test_all_values(self) -> None:
        assert PlanStatus.PENDING_APPROVAL == "pending_approval"
        assert PlanStatus.REJECTED == "rejected"
        assert PlanStatus.EXPIRED == "expired"
        assert PlanStatus.EXECUTING == "executing"
        assert PlanStatus.COMPLETED == "completed"
        assert PlanStatus.FAILED == "failed"

    def test_is_str_enum(self) -> None:
        assert isinstance(PlanStatus.EXECUTING, str)


# ---------------------------------------------------------------------------
# StepStatus enum
# ---------------------------------------------------------------------------


class TestStepStatus:
    def test_all_values(self) -> None:
        assert StepStatus.PENDING == "pending"
        assert StepStatus.READY == "ready"
        assert StepStatus.EXECUTING == "executing"
        assert StepStatus.VERIFYING == "verifying"
        assert StepStatus.SUCCEEDED == "succeeded"
        assert StepStatus.FAILED == "failed"
        assert StepStatus.SKIPPED == "skipped"
        assert StepStatus.BLOCKED == "blocked"


# ---------------------------------------------------------------------------
# StepState
# ---------------------------------------------------------------------------


class TestStepState:
    def test_defaults(self) -> None:
        state = StepState(order=1)
        assert state.status == StepStatus.PENDING
        assert state.action_result is None
        assert state.verify_result is None
        assert state.started_at is None
        assert state.completed_at is None
        assert state.error_message is None

    def test_with_action_result(self) -> None:
        ar = ActionResult(status=ActionStatus.SUCCESS)
        state = StepState(order=1, status=StepStatus.SUCCEEDED, action_result=ar)
        assert state.action_result.status == ActionStatus.SUCCESS

    def test_with_verify_result(self) -> None:
        vr = VerifyResult(verified=True, message="OK")
        state = StepState(order=1, verify_result=vr)
        assert state.verify_result.verified is True

    def test_with_timestamps(self) -> None:
        now = datetime.now(UTC)
        state = StepState(order=1, started_at=now, completed_at=now)
        assert state.started_at == now

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            StepState(order=1, extra_field="bad")


# ---------------------------------------------------------------------------
# RemediationPlan
# ---------------------------------------------------------------------------


class TestRemediationPlan:
    def test_defaults(self) -> None:
        plan = _make_plan()
        assert plan.status == PlanStatus.PENDING_APPROVAL
        assert len(plan.steps) == 2
        assert plan.step_states == []
        assert plan.diagnosis == ""
        assert plan.completed_at is None
        assert plan.entity_id == ""
        assert plan.severity == ""
        assert plan.source == ""
        assert plan.system == ""

    def test_auto_generated_id(self) -> None:
        plan1 = _make_plan()
        plan2 = _make_plan()
        assert plan1.id != plan2.id

    def test_timestamps_auto_set(self) -> None:
        plan = _make_plan()
        assert plan.created_at is not None
        assert plan.updated_at is not None

    def test_with_step_states(self) -> None:
        states = [
            StepState(order=1, status=StepStatus.SUCCEEDED),
            StepState(order=2, status=StepStatus.PENDING),
        ]
        plan = _make_plan(step_states=states)
        assert len(plan.step_states) == 2
        assert plan.step_states[0].status == StepStatus.SUCCEEDED

    def test_with_denormalized_context(self) -> None:
        plan = _make_plan(
            entity_id="sensor.temperature",
            severity="error",
            source="ha_websocket",
            system="homeassistant",
        )
        assert plan.entity_id == "sensor.temperature"
        assert plan.severity == "error"

    def test_effective_risk_tier_required(self) -> None:
        with pytest.raises(ValidationError):
            RemediationPlan(
                event_id="evt-123",
                steps=[_make_step()],
                # missing effective_risk_tier
            )

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            _make_plan(bogus="field")

    def test_serialization_roundtrip(self) -> None:
        plan = _make_plan(
            diagnosis="ZWave failure",
            entity_id="sensor.temperature",
        )
        json_str = plan.model_dump_json()
        restored = RemediationPlan.model_validate_json(json_str)
        assert restored.id == plan.id
        assert restored.event_id == plan.event_id
        assert len(restored.steps) == 2
        assert restored.diagnosis == "ZWave failure"
