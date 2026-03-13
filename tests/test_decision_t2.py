"""Tests for T2 reasoning paths in the decision engine."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from oasisagent.config import CircuitBreakerConfig, GuardrailsConfig
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionEngine,
    DecisionTier,
)
from oasisagent.engine.guardrails import GuardrailsEngine
from oasisagent.engine.known_fixes import KnownFixRegistry
from oasisagent.models import (
    DiagnosisResult,
    Disposition,
    Event,
    EventMetadata,
    RecommendedAction,
    RiskTier,
    Severity,
    TriageResult,
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
        "payload": {"error": "unknown failure"},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_guardrails(**overrides: Any) -> GuardrailsConfig:
    defaults: dict[str, Any] = {
        "blocked_domains": ["lock.*", "alarm_control_panel.*"],
        "blocked_entities": [],
        "kill_switch": False,
        "dry_run": False,
        "circuit_breaker": CircuitBreakerConfig(),
    }
    defaults.update(overrides)
    return GuardrailsConfig(**defaults)


def _make_triage_service(
    disposition: Disposition = Disposition.ESCALATE_T2,
    confidence: float = 0.8,
) -> AsyncMock:
    mock = AsyncMock()
    mock.classify.return_value = TriageResult(
        disposition=disposition,
        confidence=confidence,
        classification="integration_failure",
        summary="ZWave integration crashed",
        reasoning="Multiple restarts failed",
    )
    return mock


def _make_reasoning_service(
    diagnosis: DiagnosisResult | None = None,
) -> AsyncMock:
    mock = AsyncMock()
    if diagnosis is None:
        diagnosis = DiagnosisResult(
            root_cause="ZWave USB stick needs reset",
            confidence=0.85,
            recommended_actions=[
                RecommendedAction(
                    description="Restart ZWave integration",
                    handler="homeassistant",
                    operation="restart_integration",
                    params={"integration": "zwave_js"},
                    risk_tier=RiskTier.AUTO_FIX,
                    reasoning="Safe restart of non-critical integration",
                ),
            ],
            risk_assessment="Low risk — only affects ZWave devices",
        )
    mock.diagnose.return_value = diagnosis
    return mock


def _make_engine(
    guardrails_config: GuardrailsConfig | None = None,
    triage_service: AsyncMock | None = None,
    reasoning_service: AsyncMock | None = None,
) -> DecisionEngine:
    registry = KnownFixRegistry()
    guardrails = GuardrailsEngine(guardrails_config or _make_guardrails())
    return DecisionEngine(
        registry=registry,
        guardrails=guardrails,
        triage_service=triage_service,
        reasoning_service=reasoning_service,
    )


# ---------------------------------------------------------------------------
# T1 → T2 escalation
# ---------------------------------------------------------------------------


class TestT1EscalateToT2:
    """T1 escalates to T2 when reasoning service is available."""

    async def test_escalate_t2_calls_reasoning(self) -> None:
        triage = _make_triage_service(Disposition.ESCALATE_T2)
        reasoning = _make_reasoning_service()
        engine = _make_engine(triage_service=triage, reasoning_service=reasoning)

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T2
        assert result.disposition == DecisionDisposition.MATCHED
        reasoning.diagnose.assert_awaited_once()

    async def test_escalate_t2_no_reasoning_service(self) -> None:
        triage = _make_triage_service(Disposition.ESCALATE_T2)
        engine = _make_engine(triage_service=triage, reasoning_service=None)

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T1
        assert result.disposition == DecisionDisposition.ESCALATED
        assert result.details.get("escalate_to") == "human"


# ---------------------------------------------------------------------------
# T2 with guardrails
# ---------------------------------------------------------------------------


class TestT2Guardrails:
    """T2 actions are independently guardrail-checked."""

    async def test_single_action_approved(self) -> None:
        reasoning = _make_reasoning_service()
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.MATCHED
        assert len(result.recommended_actions) == 1
        assert result.recommended_actions[0].operation == "restart_integration"

    async def test_action_blocked_by_domain(self) -> None:
        """Action on a blocked domain entity gets blocked."""
        diagnosis = DiagnosisResult(
            root_cause="Lock firmware issue",
            confidence=0.9,
            recommended_actions=[
                RecommendedAction(
                    description="Restart lock integration",
                    handler="homeassistant",
                    operation="restart_integration",
                    risk_tier=RiskTier.AUTO_FIX,
                    reasoning="Need to restart",
                ),
            ],
            risk_assessment="Affects security domain",
        )
        reasoning = _make_reasoning_service(diagnosis)
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )
        # Entity in blocked domain
        event = _make_event(entity_id="lock.front_door")

        result = await engine.process_event(event)

        assert result.disposition == DecisionDisposition.BLOCKED

    async def test_three_actions_one_blocked(self) -> None:
        """3 actions, 1 blocked by escalate tier → 2 approved."""
        diagnosis = DiagnosisResult(
            root_cause="Multiple integrations affected",
            confidence=0.8,
            recommended_actions=[
                RecommendedAction(
                    description="Restart ZWave",
                    handler="homeassistant",
                    operation="restart_integration",
                    params={"integration": "zwave_js"},
                    risk_tier=RiskTier.AUTO_FIX,
                    reasoning="Safe restart",
                ),
                RecommendedAction(
                    description="Notify operator",
                    handler="homeassistant",
                    operation="notify",
                    risk_tier=RiskTier.RECOMMEND,
                    reasoning="Operator should verify",
                ),
                RecommendedAction(
                    description="Escalate to human",
                    handler="homeassistant",
                    operation="escalate",
                    risk_tier=RiskTier.ESCALATE,
                    reasoning="Unclear root cause for this action",
                ),
            ],
            risk_assessment="Mixed risk levels",
        )
        reasoning = _make_reasoning_service(diagnosis)
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.MATCHED
        assert len(result.recommended_actions) == 2
        assert result.details["total_actions"] == 3
        assert result.details["approved_actions"] == 2
        assert result.details["blocked_count"] == 1

    async def test_all_actions_blocked_escalates(self) -> None:
        """When all T2 actions are blocked, escalate to human."""
        diagnosis = DiagnosisResult(
            root_cause="Needs firmware update",
            confidence=0.9,
            recommended_actions=[
                RecommendedAction(
                    description="High-risk action",
                    handler="homeassistant",
                    operation="firmware_update",
                    risk_tier=RiskTier.ESCALATE,
                    reasoning="Requires human judgment",
                ),
            ],
            risk_assessment="High risk",
        )
        reasoning = _make_reasoning_service(diagnosis)
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.BLOCKED
        assert result.tier == DecisionTier.T2
        assert result.details["blocked_count"] == 1

    async def test_no_actions_escalates_to_human(self) -> None:
        """T2 returns no actions → escalate to human."""
        diagnosis = DiagnosisResult(
            root_cause="Cannot determine fix",
            confidence=0.3,
            recommended_actions=[],
            risk_assessment="Unknown",
        )
        reasoning = _make_reasoning_service(diagnosis)
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.ESCALATED
        assert result.tier == DecisionTier.T2
        assert result.details.get("escalate_to") == "human"

    async def test_kill_switch_blocks_all_t2_actions(self) -> None:
        reasoning = _make_reasoning_service()
        engine = _make_engine(
            guardrails_config=_make_guardrails(kill_switch=True),
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.BLOCKED

    async def test_target_entity_id_checked_instead_of_event(self) -> None:
        """Guardrails check action.target_entity_id when set, not event.entity_id."""
        diagnosis = DiagnosisResult(
            root_cause="Lock firmware issue",
            confidence=0.9,
            recommended_actions=[
                RecommendedAction(
                    description="Restart lock integration",
                    handler="homeassistant",
                    operation="restart_integration",
                    risk_tier=RiskTier.AUTO_FIX,
                    reasoning="Need to restart",
                    target_entity_id="lock.front_door",
                ),
            ],
            risk_assessment="Affects security domain",
        )
        reasoning = _make_reasoning_service(diagnosis)
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )
        # Event entity is NOT in a blocked domain, but target is
        event = _make_event(entity_id="sensor.temperature")

        result = await engine.process_event(event)

        assert result.disposition == DecisionDisposition.BLOCKED

    async def test_no_target_entity_falls_back_to_event(self) -> None:
        """Without target_entity_id, guardrails use event.entity_id."""
        diagnosis = DiagnosisResult(
            root_cause="Sensor issue",
            confidence=0.9,
            recommended_actions=[
                RecommendedAction(
                    description="Restart sensor integration",
                    handler="homeassistant",
                    operation="restart_integration",
                    risk_tier=RiskTier.AUTO_FIX,
                    reasoning="Safe restart",
                ),
            ],
            risk_assessment="Low risk",
        )
        reasoning = _make_reasoning_service(diagnosis)
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )
        event = _make_event(entity_id="sensor.temperature")

        result = await engine.process_event(event)

        assert result.disposition == DecisionDisposition.MATCHED
        assert len(result.recommended_actions) == 1

    async def test_dry_run_blocks_t2_actions(self) -> None:
        """Dry run mode blocks all T2 actions (they count as not approved)."""
        reasoning = _make_reasoning_service()
        engine = _make_engine(
            guardrails_config=_make_guardrails(dry_run=True),
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        # Dry run + blocked = all actions filtered
        assert result.disposition == DecisionDisposition.BLOCKED


# ---------------------------------------------------------------------------
# T2 result details
# ---------------------------------------------------------------------------


class TestT2ResultDetails:
    """T2 DecisionResult carries full context."""

    async def test_result_has_diagnosis(self) -> None:
        reasoning = _make_reasoning_service()
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert result.diagnosis == "ZWave USB stick needs reset"

    async def test_result_has_confidence(self) -> None:
        reasoning = _make_reasoning_service()
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert result.details["confidence"] == 0.85

    async def test_result_has_risk_assessment(self) -> None:
        reasoning = _make_reasoning_service()
        engine = _make_engine(
            triage_service=_make_triage_service(),
            reasoning_service=reasoning,
        )

        result = await engine.process_event(_make_event())

        assert "risk_assessment" in result.details
