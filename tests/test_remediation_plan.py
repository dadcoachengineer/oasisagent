"""Tests for remediation plan models, parsing, and prompt injection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from oasisagent.llm.prompts.diagnose_failure import build_diagnose_messages
from oasisagent.llm.reasoning import _parse_diagnosis, _validate_plan_steps
from oasisagent.models import (
    DependencyContext,
    DependencyNode,
    DiagnosisResult,
    Disposition,
    Event,
    RecommendedAction,
    RemediationStep,
    RiskTier,
    Severity,
    TriageResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "ha_websocket",
        "system": "homeassistant",
        "event_type": "integration_failure",
        "entity_id": "sensor.temperature",
        "severity": Severity.ERROR,
        "timestamp": datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_triage(**overrides: Any) -> TriageResult:
    defaults: dict[str, Any] = {
        "disposition": Disposition.ESCALATE_T2,
        "confidence": 0.8,
        "classification": "integration_failure",
        "summary": "ZWave integration crashed repeatedly",
        "reasoning": "Multiple restart attempts failed",
    }
    defaults.update(overrides)
    return TriageResult(**defaults)


def _make_action_dict(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "description": "Restart ZWave integration",
        "handler": "homeassistant",
        "operation": "restart_integration",
        "params": {"integration": "zwave_js"},
        "risk_tier": "auto_fix",
        "reasoning": "Safe restart",
    }
    defaults.update(overrides)
    return defaults


def _valid_diagnosis_json(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "root_cause": "ZWave USB stick needs reset",
        "confidence": 0.85,
        "recommended_actions": [_make_action_dict()],
        "risk_assessment": "Low risk",
    }
    data.update(overrides)
    return json.dumps(data)


def _make_dep_ctx() -> DependencyContext:
    return DependencyContext(
        entity_id="sensor.temperature",
        upstream=[DependencyNode(
            entity_id="host:rpi4",
            entity_type="host",
            display_name="rpi4",
            edge_type="runs_on",
            depth=1,
        )],
    )


# ---------------------------------------------------------------------------
# RemediationStep model
# ---------------------------------------------------------------------------


class TestRemediationStepModel:
    def test_valid_step(self) -> None:
        step = RemediationStep(
            order=1,
            action=RecommendedAction(
                description="Restart service",
                handler="homeassistant",
                operation="restart_integration",
                risk_tier=RiskTier.AUTO_FIX,
            ),
            success_criteria="Entity returns to 'available' state",
        )
        assert step.order == 1
        assert step.depends_on == []
        assert step.conditional is False

    def test_order_ge_1(self) -> None:
        with pytest.raises(ValidationError):
            RemediationStep(
                order=0,
                action=RecommendedAction(
                    description="Bad step",
                    handler="homeassistant",
                    operation="notify",
                    risk_tier=RiskTier.AUTO_FIX,
                ),
                success_criteria="N/A",
            )

    def test_with_depends_on(self) -> None:
        step = RemediationStep(
            order=2,
            action=RecommendedAction(
                description="Step 2",
                handler="homeassistant",
                operation="notify",
                risk_tier=RiskTier.RECOMMEND,
            ),
            success_criteria="Notification sent",
            depends_on=[1],
            conditional=True,
        )
        assert step.depends_on == [1]
        assert step.conditional is True


# ---------------------------------------------------------------------------
# DiagnosisResult with plan
# ---------------------------------------------------------------------------


class TestDiagnosisWithPlan:
    def test_diagnosis_with_plan(self) -> None:
        result = DiagnosisResult(
            root_cause="Multi-system failure",
            confidence=0.8,
            remediation_plan=[
                RemediationStep(
                    order=1,
                    action=RecommendedAction(
                        description="Fix upstream",
                        handler="portainer",
                        operation="restart_container",
                        risk_tier=RiskTier.AUTO_FIX,
                    ),
                    success_criteria="Container running",
                ),
                RemediationStep(
                    order=2,
                    action=RecommendedAction(
                        description="Fix downstream",
                        handler="homeassistant",
                        operation="restart_integration",
                        risk_tier=RiskTier.AUTO_FIX,
                    ),
                    success_criteria="Integration available",
                    depends_on=[1],
                ),
            ],
        )
        assert result.remediation_plan is not None
        assert len(result.remediation_plan) == 2

    def test_diagnosis_without_plan_backward_compat(self) -> None:
        result = DiagnosisResult(
            root_cause="Simple failure",
            confidence=0.9,
        )
        assert result.remediation_plan is None


# ---------------------------------------------------------------------------
# _validate_plan_steps
# ---------------------------------------------------------------------------


class TestValidatePlanSteps:
    def test_valid_plan(self) -> None:
        raw = [
            {
                "order": 1,
                "action": _make_action_dict(),
                "success_criteria": "Service running",
            },
            {
                "order": 2,
                "action": _make_action_dict(operation="notify"),
                "success_criteria": "Notified",
                "depends_on": [1],
            },
        ]
        result = _validate_plan_steps(raw)
        assert result is not None
        assert len(result) == 2
        assert result[1].depends_on == [1]

    def test_strips_invalid_deps(self) -> None:
        """depends_on=[99] (nonexistent) -> step stripped."""
        raw = [
            {
                "order": 1,
                "action": _make_action_dict(),
                "success_criteria": "OK",
            },
            {
                "order": 2,
                "action": _make_action_dict(),
                "success_criteria": "OK",
                "depends_on": [99],  # Step 99 doesn't exist
            },
        ]
        result = _validate_plan_steps(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0].order == 1

    def test_preserves_valid_deps(self) -> None:
        raw = [
            {
                "order": 1,
                "action": _make_action_dict(),
                "success_criteria": "OK",
            },
            {
                "order": 2,
                "action": _make_action_dict(),
                "success_criteria": "OK",
                "depends_on": [1],
            },
        ]
        result = _validate_plan_steps(raw)
        assert result is not None
        assert len(result) == 2

    def test_empty_plan_returns_none(self) -> None:
        assert _validate_plan_steps([]) is None

    def test_all_invalid_returns_none(self) -> None:
        raw = [{"not": "a valid step"}]
        assert _validate_plan_steps(raw) is None

    def test_invalid_risk_tier_defaults_to_escalate(self) -> None:
        raw = [
            {
                "order": 1,
                "action": _make_action_dict(risk_tier="yolo"),
                "success_criteria": "OK",
            },
        ]
        result = _validate_plan_steps(raw)
        assert result is not None
        assert result[0].action.risk_tier == RiskTier.ESCALATE


# ---------------------------------------------------------------------------
# _parse_diagnosis with plans
# ---------------------------------------------------------------------------


class TestParseDiagnosisWithPlan:
    def test_with_valid_plan(self) -> None:
        data = json.loads(_valid_diagnosis_json())
        data["remediation_plan"] = [
            {
                "order": 1,
                "action": _make_action_dict(),
                "success_criteria": "Integration available",
            },
        ]
        result = _parse_diagnosis(json.dumps(data))
        assert result.remediation_plan is not None
        assert len(result.remediation_plan) == 1

    def test_with_malformed_plan_falls_back(self) -> None:
        """Bad plan -> None, recommended_actions preserved."""
        data = json.loads(_valid_diagnosis_json())
        data["remediation_plan"] = "not a list"
        result = _parse_diagnosis(json.dumps(data))
        assert result.remediation_plan is None
        assert len(result.recommended_actions) == 1

    def test_without_plan_field(self) -> None:
        """JSON lacks plan key -> None."""
        result = _parse_diagnosis(_valid_diagnosis_json())
        assert result.remediation_plan is None

    def test_plan_with_invalid_steps_stripped(self) -> None:
        data = json.loads(_valid_diagnosis_json())
        data["remediation_plan"] = [
            {
                "order": 1,
                "action": _make_action_dict(),
                "success_criteria": "OK",
            },
            {"garbage": True},  # Invalid step
        ]
        result = _parse_diagnosis(json.dumps(data))
        assert result.remediation_plan is not None
        assert len(result.remediation_plan) == 1


# ---------------------------------------------------------------------------
# Prompt includes/excludes plan instructions
# ---------------------------------------------------------------------------


class TestPromptPlanInstructions:
    def test_includes_plan_instructions_with_deps(self) -> None:
        """dependency context present -> plan section in prompt."""
        messages = build_diagnose_messages(
            _make_event(), _make_triage(),
            dependency_context=_make_dep_ctx(),
        )
        user_content = messages[1]["content"]
        assert "Remediation Planning" in user_content
        assert "remediation_plan" in user_content

    def test_excludes_plan_instructions_without_deps(self) -> None:
        """No dependency context -> no plan section."""
        messages = build_diagnose_messages(
            _make_event(), _make_triage(),
        )
        user_content = messages[1]["content"]
        assert "Remediation Planning" not in user_content

    def test_excludes_plan_instructions_with_empty_deps(self) -> None:
        """Empty dependency context (no upstream/downstream/same_host) -> no plan."""
        empty_ctx = DependencyContext(entity_id="sensor.temperature")
        messages = build_diagnose_messages(
            _make_event(), _make_triage(),
            dependency_context=empty_ctx,
        )
        user_content = messages[1]["content"]
        assert "Remediation Planning" not in user_content
