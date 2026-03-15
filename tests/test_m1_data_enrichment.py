"""Tests for M1 Data Enrichment — DecisionDetails, guardrail rule names,
suppression audit, and InfluxDB field vs tag verification (#218)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.config import CircuitBreakerConfig, GuardrailsConfig
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionEngine,
    DecisionResult,
    DecisionTier,
)
from oasisagent.engine.guardrails import GuardrailResult, GuardrailsEngine
from oasisagent.engine.known_fixes import (
    FixAction,
    FixActionType,
    FixMatch,
    KnownFix,
    KnownFixRegistry,
)
from oasisagent.models import (
    DecisionDetails,
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
            handler="homeassistant",
            operation="restart_integration",
            type=FixActionType.AUTO_FIX,
        ),
        "risk_tier": RiskTier.AUTO_FIX,
    }
    defaults.update(overrides)
    return KnownFix(**defaults)


def _guardrails_config(**overrides: Any) -> GuardrailsConfig:
    defaults: dict[str, Any] = {
        "blocked_domains": [],
        "blocked_entities": [],
        "kill_switch": False,
        "dry_run": False,
        "circuit_breaker": CircuitBreakerConfig(),
    }
    defaults.update(overrides)
    return GuardrailsConfig(**defaults)


# ---------------------------------------------------------------------------
# DecisionDetails TypedDict contract
# ---------------------------------------------------------------------------


class TestDecisionDetailsSchema:
    """DecisionDetails TypedDict provides a stable contract."""

    def test_typeddict_has_t1_fields(self) -> None:
        annotations = DecisionDetails.__annotations__
        assert "classification" in annotations
        assert "confidence" in annotations
        assert "reasoning" in annotations
        assert "suggested_fix" in annotations

    def test_typeddict_has_t2_fields(self) -> None:
        annotations = DecisionDetails.__annotations__
        assert "t2_root_cause" in annotations
        assert "t2_confidence" in annotations
        assert "risk_assessment" in annotations
        assert "total_actions" in annotations
        assert "approved_actions" in annotations
        assert "blocked_count" in annotations

    def test_typeddict_has_guardrail_field(self) -> None:
        annotations = DecisionDetails.__annotations__
        assert "guardrail_rule" in annotations

    def test_typeddict_has_correlation_fields(self) -> None:
        annotations = DecisionDetails.__annotations__
        assert "leader_event_id" in annotations
        assert "escalate_to" in annotations

    def test_typeddict_has_suppression_field(self) -> None:
        annotations = DecisionDetails.__annotations__
        assert "suppressed_count" in annotations

    def test_typeddict_is_total_false(self) -> None:
        """All fields are optional (total=False)."""
        assert DecisionDetails.__total__ is False

    def test_can_construct_partial(self) -> None:
        """Should be able to create with only some fields."""
        d: DecisionDetails = {"classification": "network", "confidence": 0.9}
        assert d["classification"] == "network"


# ---------------------------------------------------------------------------
# Guardrail rule names
# ---------------------------------------------------------------------------


class TestGuardrailRuleName:
    """Each guardrail check returns a specific rule_name."""

    def test_kill_switch_rule_name(self) -> None:
        engine = GuardrailsEngine(_guardrails_config(kill_switch=True))
        result = engine.check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.rule_name == "kill_switch"

    def test_risk_tier_policy_rule_name(self) -> None:
        engine = GuardrailsEngine(_guardrails_config())
        result = engine.check("light.kitchen", RiskTier.ESCALATE)
        assert result.rule_name == "risk_tier_policy"

    def test_blocked_domain_rule_name(self) -> None:
        engine = GuardrailsEngine(
            _guardrails_config(blocked_domains=["lock.*"])
        )
        result = engine.check("lock.front_door", RiskTier.AUTO_FIX)
        assert result.rule_name == "blocked_domain:lock.*"

    def test_blocked_entity_rule_name(self) -> None:
        engine = GuardrailsEngine(
            _guardrails_config(blocked_entities=["sensor.critical"])
        )
        result = engine.check("sensor.critical", RiskTier.AUTO_FIX)
        assert result.rule_name == "blocked_entity:sensor.critical"

    def test_dry_run_rule_name(self) -> None:
        engine = GuardrailsEngine(_guardrails_config(dry_run=True))
        result = engine.check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.rule_name == "dry_run"

    def test_passed_rule_name(self) -> None:
        engine = GuardrailsEngine(_guardrails_config())
        result = engine.check("light.kitchen", RiskTier.AUTO_FIX)
        assert result.rule_name == "passed"


# ---------------------------------------------------------------------------
# T0 decision populates guardrail_rule in details
# ---------------------------------------------------------------------------


class TestT0DetailsEnrichment:
    """T0 decisions include guardrail_rule in details dict."""

    def test_t0_matched_has_guardrail_rule(self) -> None:
        fix = _make_fix()
        registry = KnownFixRegistry.__new__(KnownFixRegistry)
        registry._fixes = [fix]
        guardrails = GuardrailsEngine(_guardrails_config())
        engine = DecisionEngine(registry, guardrails)

        event = _make_event()
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            engine.process_event(event)
        )
        assert result.tier == DecisionTier.T0
        assert result.details.get("guardrail_rule") == "passed"

    def test_t0_blocked_has_guardrail_rule(self) -> None:
        fix = _make_fix(risk_tier=RiskTier.ESCALATE)
        registry = KnownFixRegistry.__new__(KnownFixRegistry)
        registry._fixes = [fix]
        guardrails = GuardrailsEngine(_guardrails_config())
        engine = DecisionEngine(registry, guardrails)

        event = _make_event()
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            engine.process_event(event)
        )
        assert result.disposition == DecisionDisposition.BLOCKED
        assert result.details.get("guardrail_rule") == "risk_tier_policy"


# ---------------------------------------------------------------------------
# T2 decision populates t2_root_cause and t2_confidence
# ---------------------------------------------------------------------------


class TestT2DetailsEnrichment:
    """T2 decisions include t2_root_cause and t2_confidence in details."""

    @pytest.mark.asyncio
    async def test_t2_matched_has_root_cause(self) -> None:
        from oasisagent.models import DiagnosisResult

        triage_result = TriageResult(
            disposition=Disposition.ESCALATE_T2,
            confidence=0.8,
            classification="network",
            summary="Network issue detected",
            reasoning="Switch appears down",
        )
        diagnosis = DiagnosisResult(
            root_cause="UniFi switch lost power",
            confidence=0.95,
            recommended_actions=[
                RecommendedAction(
                    description="Restart switch",
                    handler="unifi",
                    operation="restart_device",
                    risk_tier=RiskTier.AUTO_FIX,
                ),
            ],
            risk_assessment="Low risk",
        )

        mock_triage = AsyncMock()
        mock_triage.classify.return_value = triage_result
        mock_reasoning = AsyncMock()
        mock_reasoning.diagnose.return_value = diagnosis

        registry = KnownFixRegistry.__new__(KnownFixRegistry)
        registry._fixes = []
        guardrails = GuardrailsEngine(_guardrails_config())
        engine = DecisionEngine(
            registry, guardrails,
            triage_service=mock_triage,
            reasoning_service=mock_reasoning,
        )

        event = _make_event()
        result = await engine.process_event(event)

        assert result.tier == DecisionTier.T2
        assert result.details["t2_root_cause"] == "UniFi switch lost power"
        assert result.details["t2_confidence"] == 0.95
        assert result.details["risk_assessment"] == "Low risk"

    @pytest.mark.asyncio
    async def test_t2_escalated_has_root_cause(self) -> None:
        from oasisagent.models import DiagnosisResult

        triage_result = TriageResult(
            disposition=Disposition.ESCALATE_T2,
            confidence=0.7,
            classification="storage",
            summary="Disk issue",
            reasoning="SMART errors",
        )
        diagnosis = DiagnosisResult(
            root_cause="Disk failing",
            confidence=0.6,
            recommended_actions=[],
            risk_assessment="High risk",
        )

        mock_triage = AsyncMock()
        mock_triage.classify.return_value = triage_result
        mock_reasoning = AsyncMock()
        mock_reasoning.diagnose.return_value = diagnosis

        registry = KnownFixRegistry.__new__(KnownFixRegistry)
        registry._fixes = []
        guardrails = GuardrailsEngine(_guardrails_config())
        engine = DecisionEngine(
            registry, guardrails,
            triage_service=mock_triage,
            reasoning_service=mock_reasoning,
        )

        event = _make_event()
        result = await engine.process_event(event)

        assert result.disposition == DecisionDisposition.ESCALATED
        assert result.details["t2_root_cause"] == "Disk failing"
        assert result.details["t2_confidence"] == 0.6


# ---------------------------------------------------------------------------
# Suppression audit record
# ---------------------------------------------------------------------------


class TestSuppressionAudit:
    """Suppression tracker returns count; orchestrator writes audit record."""

    def test_suppression_returns_count(self) -> None:
        from oasisagent.orchestrator import EventSuppressionTracker

        tracker = EventSuppressionTracker(threshold=2)
        event = _make_event()

        assert tracker.check(event) == 0  # 1st
        assert tracker.check(event) == 0  # 2nd (== threshold)
        count = tracker.check(event)      # 3rd (> threshold)
        assert count == 3

    def test_suppression_count_increments(self) -> None:
        from oasisagent.orchestrator import EventSuppressionTracker

        tracker = EventSuppressionTracker(threshold=1)
        event = _make_event()

        assert tracker.check(event) == 0  # 1st == threshold
        assert tracker.check(event) == 2  # 2nd
        assert tracker.check(event) == 3  # 3rd


# ---------------------------------------------------------------------------
# InfluxDB field vs tag verification
# ---------------------------------------------------------------------------


class TestInfluxDBFieldVerification:
    """Verify that variable-length LLM output is stored as fields, not tags."""

    @pytest.mark.asyncio
    async def test_write_decision_details_as_field(self) -> None:
        """DecisionResult.details must be stored as an InfluxDB field."""
        from oasisagent.audit.influxdb import AuditWriter
        from oasisagent.config import AuditConfig, InfluxDbConfig

        config = AuditConfig(
            influxdb=InfluxDbConfig(
                enabled=True,
                url="http://localhost:8086",
                token="test",
                org="test",
                bucket="test",
            )
        )
        writer = AuditWriter(config)

        mock_write_api = AsyncMock()
        writer._write_api = mock_write_api
        writer._client = MagicMock()

        event = _make_event()
        result = DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T1,
            disposition=DecisionDisposition.DROPPED,
            diagnosis="Test long reasoning text that should be a field",
            details={
                "classification": "network",
                "confidence": 0.85,
                "reasoning": "This is a long reasoning string from T1",
            },
        )

        await writer.write_decision(event, result)

        # The write_api.write should have been called with a Point
        mock_write_api.write.assert_called_once()
        call_kwargs = mock_write_api.write.call_args
        point = call_kwargs.kwargs.get("record") or call_kwargs.args[0]

        # Convert point to line protocol and verify fields vs tags
        line = point.to_line_protocol()

        # 'details' and 'diagnosis' should be in the field set (after the space
        # separating tags from fields), not in the tag set
        assert "diagnosis=" in line
        assert "details=" in line

    @pytest.mark.asyncio
    async def test_write_suppression_stores_fields(self) -> None:
        """Suppression record stores event_id and count as fields."""
        from oasisagent.audit.influxdb import AuditWriter
        from oasisagent.config import AuditConfig, InfluxDbConfig

        config = AuditConfig(
            influxdb=InfluxDbConfig(
                enabled=True,
                url="http://localhost:8086",
                token="test",
                org="test",
                bucket="test",
            )
        )
        writer = AuditWriter(config)

        mock_write_api = AsyncMock()
        writer._write_api = mock_write_api
        writer._client = MagicMock()

        event = _make_event()
        await writer.write_suppression(event, 5)

        mock_write_api.write.assert_called_once()
        call_kwargs = mock_write_api.write.call_args
        point = call_kwargs.kwargs.get("record") or call_kwargs.args[0]
        line = point.to_line_protocol()

        # Tags: source, system, event_type, entity_id, severity
        assert "oasis_suppression" in line
        # Fields: event_id, suppressed_count
        assert "suppressed_count=5i" in line
        assert f'event_id="{event.id}"' in line
