"""Tests for T1 triage service and prompt templates."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from oasisagent.config import (
    CircuitBreakerConfig,
    GuardrailsConfig,
)
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionEngine,
    DecisionTier,
)
from oasisagent.engine.guardrails import GuardrailsEngine
from oasisagent.engine.known_fixes import KnownFixRegistry
from oasisagent.llm.client import LLMClient, LLMError, LLMResponse, LLMRole
from oasisagent.llm.prompts.classify_event import (
    T1ClassifyOutput,
    build_classify_messages,
)
from oasisagent.llm.triage import TriageService, _parse_disposition
from oasisagent.models import Disposition, Event, EventMetadata, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "test",
        "system": "homeassistant",
        "event_type": "state_unavailable",
        "entity_id": "sensor.temperature",
        "severity": Severity.WARNING,
        "timestamp": datetime.now(UTC),
        "payload": {"old_state": "23.5", "new_state": "unavailable"},
        "metadata": EventMetadata(),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _t1_json(
    disposition: str = "drop",
    confidence: float = 0.9,
    classification: str = "sensor_flap",
    summary: str = "Transient sensor blip",
    suggested_fix: str | None = None,
    reasoning: str = "Normal behavior",
) -> str:
    """Build a JSON string matching T1ClassifyOutput schema."""
    return json.dumps({
        "disposition": disposition,
        "confidence": confidence,
        "classification": classification,
        "summary": summary,
        "suggested_fix": suggested_fix,
        "reasoning": reasoning,
    })


def _mock_llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        role=LLMRole.TRIAGE,
        model="qwen2.5:7b",
        usage=None,
        latency_ms=100.0,
    )


def _mock_triage_service(content: str) -> TriageService:
    """Create a TriageService with a mocked LLMClient returning given content."""
    mock_client = AsyncMock(spec=LLMClient)
    mock_client.complete.return_value = _mock_llm_response(content)
    return TriageService(mock_client)


# ---------------------------------------------------------------------------
# T1ClassifyOutput schema
# ---------------------------------------------------------------------------


class TestT1ClassifyOutput:
    """The SLM output schema parses correctly."""

    def test_valid_output(self) -> None:
        raw = _t1_json()
        output = T1ClassifyOutput.model_validate_json(raw)

        assert output.disposition == "drop"
        assert output.confidence == 0.9
        assert output.classification == "sensor_flap"

    def test_with_suggested_fix(self) -> None:
        raw = _t1_json(disposition="known_pattern", suggested_fix="Restart integration")
        output = T1ClassifyOutput.model_validate_json(raw)

        assert output.suggested_fix == "Restart integration"

    def test_missing_optional_fields_ok(self) -> None:
        raw = json.dumps({
            "disposition": "drop",
            "confidence": 0.5,
            "classification": "noise",
            "summary": "Noise event",
        })
        output = T1ClassifyOutput.model_validate_json(raw)

        assert output.suggested_fix is None
        assert output.reasoning == ""


# ---------------------------------------------------------------------------
# Disposition parsing
# ---------------------------------------------------------------------------


class TestDispositionParsing:
    """Raw SLM strings map to canonical Disposition values."""

    def test_canonical_values(self) -> None:
        assert _parse_disposition("drop") == Disposition.DROP
        assert _parse_disposition("known_pattern") == Disposition.KNOWN_PATTERN
        assert _parse_disposition("escalate_t2") == Disposition.ESCALATE_T2
        assert _parse_disposition("escalate_human") == Disposition.ESCALATE_HUMAN

    def test_common_variations(self) -> None:
        assert _parse_disposition("ignore") == Disposition.DROP
        assert _parse_disposition("noise") == Disposition.DROP
        assert _parse_disposition("escalate") == Disposition.ESCALATE_HUMAN
        assert _parse_disposition("notify_human") == Disposition.ESCALATE_HUMAN
        assert _parse_disposition("notify") == Disposition.ESCALATE_HUMAN

    def test_case_insensitive(self) -> None:
        assert _parse_disposition("DROP") == Disposition.DROP
        assert _parse_disposition("Escalate_T2") == Disposition.ESCALATE_T2

    def test_whitespace_stripped(self) -> None:
        assert _parse_disposition("  drop  ") == Disposition.DROP

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown disposition"):
            _parse_disposition("investigate")


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestBuildClassifyMessages:
    """build_classify_messages produces valid chat messages."""

    def test_returns_system_and_user(self) -> None:
        messages = build_classify_messages(_make_event())

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_user_message_contains_event_data(self) -> None:
        event = _make_event(entity_id="sensor.special")
        messages = build_classify_messages(event)

        assert "sensor.special" in messages[1]["content"]

    def test_strips_context_and_metadata(self) -> None:
        event = _make_event()
        messages = build_classify_messages(event)
        user_content = messages[1]["content"]

        assert "context" not in json.loads(
            user_content.split("Classify this infrastructure event:\n\n")[1]
        )

    def test_system_prompt_lists_dispositions(self) -> None:
        messages = build_classify_messages(_make_event())
        system = messages[0]["content"]

        assert "drop" in system
        assert "known_pattern" in system
        assert "escalate_t2" in system
        assert "escalate_human" in system


# ---------------------------------------------------------------------------
# TriageService.classify
# ---------------------------------------------------------------------------


class TestTriageServiceClassify:
    """TriageService.classify calls LLM and parses response."""

    async def test_valid_drop_response(self) -> None:
        service = _mock_triage_service(_t1_json(disposition="drop"))
        result = await service.classify(_make_event())

        assert result.disposition == Disposition.DROP
        assert result.confidence == 0.9
        assert result.classification == "sensor_flap"

    async def test_valid_known_pattern_response(self) -> None:
        service = _mock_triage_service(
            _t1_json(
                disposition="known_pattern",
                suggested_fix="Restart the Z-Wave integration",
            )
        )
        result = await service.classify(_make_event())

        assert result.disposition == Disposition.KNOWN_PATTERN
        assert result.suggested_fix == "Restart the Z-Wave integration"

    async def test_valid_escalate_t2_response(self) -> None:
        service = _mock_triage_service(
            _t1_json(disposition="escalate_t2", confidence=0.6)
        )
        result = await service.classify(_make_event())

        assert result.disposition == Disposition.ESCALATE_T2
        assert result.confidence == 0.6

    async def test_confidence_clamped(self) -> None:
        service = _mock_triage_service(
            _t1_json(confidence=1.5)
        )
        result = await service.classify(_make_event())

        assert result.confidence == 1.0

    async def test_negative_confidence_clamped(self) -> None:
        service = _mock_triage_service(
            _t1_json(confidence=-0.5)
        )
        result = await service.classify(_make_event())

        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# TriageService error handling
# ---------------------------------------------------------------------------


class TestTriageServiceErrors:
    """Error paths return safe ESCALATE_HUMAN fallback."""

    async def test_llm_unavailable_returns_escalate_human(self) -> None:
        mock_client = AsyncMock(spec=LLMClient)
        mock_client.complete.side_effect = LLMError("connection refused")
        service = TriageService(mock_client)

        result = await service.classify(_make_event())

        assert result.disposition == Disposition.ESCALATE_HUMAN
        assert result.confidence == 0.0
        assert "llm_unavailable" in result.reasoning

    async def test_invalid_json_returns_escalate_human(self) -> None:
        service = _mock_triage_service("this is not json at all")

        result = await service.classify(_make_event())

        assert result.disposition == Disposition.ESCALATE_HUMAN
        assert "parse_failure" in result.reasoning

    async def test_invalid_disposition_returns_escalate_human(self) -> None:
        service = _mock_triage_service(
            _t1_json(disposition="investigate_further")
        )

        result = await service.classify(_make_event())

        assert result.disposition == Disposition.ESCALATE_HUMAN
        assert "parse_failure" in result.reasoning

    async def test_slm_variation_handled(self) -> None:
        """SLM returns 'notify' instead of 'escalate_human' — still works."""
        service = _mock_triage_service(_t1_json(disposition="notify"))

        result = await service.classify(_make_event())

        assert result.disposition == Disposition.ESCALATE_HUMAN


# ---------------------------------------------------------------------------
# Decision engine T1 integration
# ---------------------------------------------------------------------------


class TestDecisionEngineT1:
    """Decision engine uses T1 triage when T0 doesn't match."""

    async def test_t0_miss_t1_drop(self) -> None:
        service = _mock_triage_service(_t1_json(disposition="drop"))
        engine = DecisionEngine(
            registry=KnownFixRegistry(),
            guardrails=GuardrailsEngine(GuardrailsConfig(circuit_breaker=CircuitBreakerConfig())),
            triage_service=service,
        )

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T1
        assert result.disposition == DecisionDisposition.DROPPED

    async def test_t0_miss_t1_escalate_t2(self) -> None:
        service = _mock_triage_service(_t1_json(disposition="escalate_t2"))
        engine = DecisionEngine(
            registry=KnownFixRegistry(),
            guardrails=GuardrailsEngine(GuardrailsConfig(circuit_breaker=CircuitBreakerConfig())),
            triage_service=service,
        )

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T1
        assert result.disposition == DecisionDisposition.ESCALATED
        assert result.details["escalate_to"] == "t2"

    async def test_t0_miss_t1_escalate_human(self) -> None:
        service = _mock_triage_service(_t1_json(disposition="escalate_human"))
        engine = DecisionEngine(
            registry=KnownFixRegistry(),
            guardrails=GuardrailsEngine(GuardrailsConfig(circuit_breaker=CircuitBreakerConfig())),
            triage_service=service,
        )

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T1
        assert result.disposition == DecisionDisposition.ESCALATED
        assert result.details["escalate_to"] == "human"

    async def test_t0_miss_t1_known_pattern(self) -> None:
        service = _mock_triage_service(
            _t1_json(disposition="known_pattern", suggested_fix="Restart it")
        )
        engine = DecisionEngine(
            registry=KnownFixRegistry(),
            guardrails=GuardrailsEngine(GuardrailsConfig(circuit_breaker=CircuitBreakerConfig())),
            triage_service=service,
        )

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T1
        assert result.disposition == DecisionDisposition.MATCHED
        assert result.details["suggested_fix"] == "Restart it"

    async def test_t0_miss_t1_llm_failure_escalates(self) -> None:
        mock_client = AsyncMock(spec=LLMClient)
        mock_client.complete.side_effect = LLMError("down")
        service = TriageService(mock_client)
        engine = DecisionEngine(
            registry=KnownFixRegistry(),
            guardrails=GuardrailsEngine(GuardrailsConfig(circuit_breaker=CircuitBreakerConfig())),
            triage_service=service,
        )

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T1
        assert result.disposition == DecisionDisposition.ESCALATED
        assert result.details["escalate_to"] == "human"

    async def test_t0_match_skips_t1(self) -> None:
        """T0 match should not call T1 at all."""
        mock_client = AsyncMock(spec=LLMClient)
        service = TriageService(mock_client)

        from oasisagent.engine.known_fixes import FixAction, FixActionType, FixMatch, KnownFix

        fix = KnownFix(
            id="test-fix",
            match=FixMatch(system="homeassistant"),
            diagnosis="Known fix",
            action=FixAction(
                type=FixActionType.RECOMMEND,
                handler="homeassistant",
                operation="notify",
            ),
            risk_tier="recommend",
        )
        registry = KnownFixRegistry()
        registry._fixes = [fix]

        engine = DecisionEngine(
            registry=registry,
            guardrails=GuardrailsEngine(GuardrailsConfig(circuit_breaker=CircuitBreakerConfig())),
            triage_service=service,
        )

        result = await engine.process_event(_make_event())

        assert result.tier == DecisionTier.T0
        assert result.disposition == DecisionDisposition.MATCHED
        mock_client.complete.assert_not_called()
