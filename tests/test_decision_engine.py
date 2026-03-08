"""Tests for the decision engine orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oasisagent.config import CircuitBreakerConfig, GuardrailsConfig
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionEngine,
    DecisionTier,
)
from oasisagent.engine.guardrails import GuardrailsEngine
from oasisagent.engine.known_fixes import (
    FixAction,
    FixActionType,
    FixMatch,
    KnownFix,
    KnownFixRegistry,
)
from oasisagent.models import Event, EventMetadata, RiskTier, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "homeassistant",
        "event_type": "automation_error",
        "entity_id": "automation.kitchen_lights",
        "severity": Severity.ERROR,
        "timestamp": datetime.now(UTC),
        "payload": {"error": "kelvin deprecated"},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_fix(**overrides: Any) -> KnownFix:
    defaults: dict[str, Any] = {
        "id": "test-fix",
        "match": FixMatch(system="homeassistant"),
        "diagnosis": "Test diagnosis",
        "action": FixAction(
            type=FixActionType.RECOMMEND,
            handler="homeassistant",
            operation="notify",
        ),
        "risk_tier": RiskTier.RECOMMEND,
    }
    defaults.update(overrides)
    return KnownFix(**defaults)


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


def _make_engine(
    fixes: list[KnownFix] | None = None,
    guardrails_config: GuardrailsConfig | None = None,
) -> DecisionEngine:
    registry = KnownFixRegistry()
    if fixes is not None:
        registry._fixes = fixes

    guardrails = GuardrailsEngine(guardrails_config or _make_guardrails())
    return DecisionEngine(registry=registry, guardrails=guardrails)


# ---------------------------------------------------------------------------
# T0 match → guardrails allowed
# ---------------------------------------------------------------------------


class TestT0Matched:
    """Events that match a T0 fix and pass guardrails."""

    def test_matched_event_returns_matched(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.RECOMMEND)
        engine = _make_engine(fixes=[fix])
        event = _make_event()

        result = engine.process_event(event)

        assert result.event_id == event.id
        assert result.tier == DecisionTier.T0
        assert result.disposition == DecisionDisposition.MATCHED
        assert result.matched_fix_id == "test-fix"
        assert result.diagnosis == "Test diagnosis"
        assert result.guardrail_result is not None
        assert result.guardrail_result.allowed is True

    def test_auto_fix_tier_allowed(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.AUTO_FIX)
        engine = _make_engine(fixes=[fix])

        result = engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.MATCHED
        assert result.guardrail_result.risk_tier == RiskTier.AUTO_FIX


# ---------------------------------------------------------------------------
# T0 match → guardrails blocked
# ---------------------------------------------------------------------------


class TestT0Blocked:
    """Events that match a T0 fix but are blocked by guardrails."""

    def test_blocked_domain_returns_blocked(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.AUTO_FIX)
        engine = _make_engine(fixes=[fix])
        event = _make_event(entity_id="lock.front_door")

        result = engine.process_event(event)

        assert result.disposition == DecisionDisposition.BLOCKED
        assert result.matched_fix_id == "test-fix"
        assert result.guardrail_result is not None
        assert result.guardrail_result.allowed is False

    def test_kill_switch_blocks(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.RECOMMEND)
        config = _make_guardrails(kill_switch=True)
        engine = _make_engine(fixes=[fix], guardrails_config=config)

        result = engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.BLOCKED
        assert "kill switch" in result.guardrail_result.reason.lower()

    def test_escalate_tier_blocked(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.ESCALATE)
        engine = _make_engine(fixes=[fix])

        result = engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.BLOCKED
        assert result.guardrail_result.risk_tier == RiskTier.ESCALATE

    def test_block_tier_blocked(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.BLOCK)
        engine = _make_engine(fixes=[fix])

        result = engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.BLOCKED


# ---------------------------------------------------------------------------
# T0 match → dry run
# ---------------------------------------------------------------------------


class TestT0DryRun:
    """Events matched in dry run mode."""

    def test_dry_run_returns_dry_run_disposition(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.RECOMMEND)
        config = _make_guardrails(dry_run=True)
        engine = _make_engine(fixes=[fix], guardrails_config=config)

        result = engine.process_event(_make_event())

        assert result.disposition == DecisionDisposition.DRY_RUN
        assert result.matched_fix_id == "test-fix"
        assert result.guardrail_result.dry_run is True
        assert result.guardrail_result.allowed is True


# ---------------------------------------------------------------------------
# No T0 match
# ---------------------------------------------------------------------------


class TestUnmatched:
    """Events with no T0 match produce an UNMATCHED result."""

    def test_no_match_returns_unmatched(self) -> None:
        engine = _make_engine(fixes=[])
        event = _make_event()

        result = engine.process_event(event)

        assert result.event_id == event.id
        assert result.tier == DecisionTier.T0
        assert result.disposition == DecisionDisposition.UNMATCHED
        assert result.matched_fix_id is None
        assert result.guardrail_result is None
        assert "T1/T2" in result.diagnosis

    def test_unmatched_when_system_differs(self) -> None:
        fix = _make_fix(match=FixMatch(system="docker"))
        engine = _make_engine(fixes=[fix])
        event = _make_event(system="homeassistant")

        result = engine.process_event(event)

        assert result.disposition == DecisionDisposition.UNMATCHED


# ---------------------------------------------------------------------------
# Multiple events
# ---------------------------------------------------------------------------


class TestMultipleEvents:
    """Process multiple events through the engine."""

    def test_process_batch_in_order(self) -> None:
        fix = _make_fix(
            match=FixMatch(system="homeassistant", payload_contains="kelvin"),
        )
        engine = _make_engine(fixes=[fix])

        events = [
            _make_event(payload={"error": "kelvin deprecated"}),
            _make_event(payload={"error": "timeout"}),
            _make_event(payload={"error": "kelvin in config"}),
        ]

        results = [engine.process_event(e) for e in events]

        assert results[0].disposition == DecisionDisposition.MATCHED
        assert results[1].disposition == DecisionDisposition.UNMATCHED
        assert results[2].disposition == DecisionDisposition.MATCHED

    def test_different_entities_different_outcomes(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.AUTO_FIX)
        engine = _make_engine(fixes=[fix])

        safe = _make_event(entity_id="light.kitchen")
        blocked = _make_event(entity_id="lock.front_door")

        safe_result = engine.process_event(safe)
        blocked_result = engine.process_event(blocked)

        assert safe_result.disposition == DecisionDisposition.MATCHED
        assert blocked_result.disposition == DecisionDisposition.BLOCKED


# ---------------------------------------------------------------------------
# DecisionResult model
# ---------------------------------------------------------------------------


class TestDecisionResultModel:
    """Tests for DecisionResult and related enums."""

    def test_decision_tier_values(self) -> None:
        assert DecisionTier.T0 == "t0"
        assert DecisionTier.T1 == "t1"
        assert DecisionTier.T2 == "t2"

    def test_decision_disposition_values(self) -> None:
        assert DecisionDisposition.MATCHED == "matched"
        assert DecisionDisposition.BLOCKED == "blocked"
        assert DecisionDisposition.DRY_RUN == "dry_run"
        assert DecisionDisposition.UNMATCHED == "unmatched"

    def test_result_serialization(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.RECOMMEND)
        engine = _make_engine(fixes=[fix])
        result = engine.process_event(_make_event())

        data = result.model_dump()
        assert data["tier"] == "t0"
        assert data["disposition"] == "matched"
        assert data["guardrail_result"]["risk_tier"] == "recommend"


# ---------------------------------------------------------------------------
# Integration: real fix files
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end: load real YAML fixes, run events through engine."""

    def test_kelvin_event_matched_and_allowed(self) -> None:
        registry = KnownFixRegistry()
        registry.load(Path("known_fixes"))
        guardrails = GuardrailsEngine(_make_guardrails())
        engine = DecisionEngine(registry=registry, guardrails=guardrails)

        event = _make_event(
            system="homeassistant",
            event_type="automation_error",
            entity_id="automation.kitchen",
            payload={"error": "kelvin deprecated"},
        )

        result = engine.process_event(event)

        assert result.disposition == DecisionDisposition.MATCHED
        assert result.matched_fix_id == "ha-deprecated-kelvin"
        assert result.tier == DecisionTier.T0
        assert result.guardrail_result.allowed is True

    def test_unrelated_event_unmatched(self) -> None:
        registry = KnownFixRegistry()
        registry.load(Path("known_fixes"))
        guardrails = GuardrailsEngine(_make_guardrails())
        engine = DecisionEngine(registry=registry, guardrails=guardrails)

        event = _make_event(
            system="docker",
            event_type="container_unhealthy",
            payload={"container": "nginx"},
        )

        result = engine.process_event(event)

        assert result.disposition == DecisionDisposition.UNMATCHED
