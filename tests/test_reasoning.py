"""Tests for T2 reasoning service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from oasisagent.llm.client import LLMError, LLMResponse, LLMRole
from oasisagent.llm.reasoning import (
    ReasoningService,
    _parse_diagnosis,
    _validate_risk_tier,
)
from oasisagent.models import (
    Disposition,
    Event,
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


def _valid_diagnosis_json(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "root_cause": "ZWave USB stick needs reset",
        "confidence": 0.85,
        "recommended_actions": [
            {
                "description": "Restart ZWave integration",
                "handler": "homeassistant",
                "operation": "restart_integration",
                "params": {"integration": "zwave_js"},
                "risk_tier": "auto_fix",
                "reasoning": "Safe restart of non-critical integration",
            }
        ],
        "risk_assessment": "Low risk — only affects ZWave devices",
    }
    data.update(overrides)
    return json.dumps(data)


def _mock_llm_client(response_content: str) -> AsyncMock:
    mock = AsyncMock()
    mock.complete.return_value = LLMResponse(
        content=response_content,
        role=LLMRole.REASONING,
        model="test-model",
        latency_ms=50.0,
    )
    return mock


# ---------------------------------------------------------------------------
# _validate_risk_tier
# ---------------------------------------------------------------------------


class TestValidateRiskTier:
    def test_valid_tiers(self) -> None:
        assert _validate_risk_tier("auto_fix") == RiskTier.AUTO_FIX
        assert _validate_risk_tier("recommend") == RiskTier.RECOMMEND
        assert _validate_risk_tier("escalate") == RiskTier.ESCALATE
        assert _validate_risk_tier("block") == RiskTier.BLOCK

    def test_case_insensitive(self) -> None:
        assert _validate_risk_tier("AUTO_FIX") == RiskTier.AUTO_FIX
        assert _validate_risk_tier("Recommend") == RiskTier.RECOMMEND

    def test_whitespace_stripped(self) -> None:
        assert _validate_risk_tier("  auto_fix  ") == RiskTier.AUTO_FIX

    def test_invalid_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid risk_tier"):
            _validate_risk_tier("dangerous")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid risk_tier"):
            _validate_risk_tier("")


# ---------------------------------------------------------------------------
# _parse_diagnosis
# ---------------------------------------------------------------------------


class TestParseDiagnosis:
    def test_valid_json(self) -> None:
        result = _parse_diagnosis(_valid_diagnosis_json())

        assert result.root_cause == "ZWave USB stick needs reset"
        assert result.confidence == 0.85
        assert len(result.recommended_actions) == 1
        assert result.recommended_actions[0].handler == "homeassistant"
        assert result.recommended_actions[0].risk_tier == RiskTier.AUTO_FIX

    def test_confidence_clamped_high(self) -> None:
        result = _parse_diagnosis(_valid_diagnosis_json(confidence=1.5))
        assert result.confidence == 1.0

    def test_confidence_clamped_low(self) -> None:
        result = _parse_diagnosis(_valid_diagnosis_json(confidence=-0.5))
        assert result.confidence == 0.0

    def test_invalid_risk_tier_defaults_to_escalate(self) -> None:
        data = json.loads(_valid_diagnosis_json())
        data["recommended_actions"][0]["risk_tier"] = "yolo"
        result = _parse_diagnosis(json.dumps(data))

        assert result.recommended_actions[0].risk_tier == RiskTier.ESCALATE

    def test_empty_risk_tier_defaults_to_escalate(self) -> None:
        data = json.loads(_valid_diagnosis_json())
        data["recommended_actions"][0]["risk_tier"] = ""
        result = _parse_diagnosis(json.dumps(data))

        assert result.recommended_actions[0].risk_tier == RiskTier.ESCALATE

    def test_multiple_actions(self) -> None:
        data = json.loads(_valid_diagnosis_json())
        data["recommended_actions"].append({
            "description": "Notify operator",
            "handler": "homeassistant",
            "operation": "notify",
            "risk_tier": "recommend",
            "reasoning": "Operator should verify",
        })
        result = _parse_diagnosis(json.dumps(data))

        assert len(result.recommended_actions) == 2
        assert result.recommended_actions[1].risk_tier == RiskTier.RECOMMEND

    def test_no_actions(self) -> None:
        result = _parse_diagnosis(
            _valid_diagnosis_json(recommended_actions=[])
        )
        assert result.recommended_actions == []

    def test_non_dict_action_skipped(self) -> None:
        data = json.loads(_valid_diagnosis_json())
        data["recommended_actions"].insert(0, "not a dict")
        result = _parse_diagnosis(json.dumps(data))

        assert len(result.recommended_actions) == 1

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _parse_diagnosis("not json at all")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON object"):
            _parse_diagnosis('"just a string"')

    def test_additional_context_preserved(self) -> None:
        result = _parse_diagnosis(
            _valid_diagnosis_json(additional_context="USB device log shows errors")
        )
        assert result.additional_context == "USB device log shows errors"


# ---------------------------------------------------------------------------
# ReasoningService.diagnose
# ---------------------------------------------------------------------------


class TestReasoningServiceDiagnose:
    async def test_successful_diagnosis(self) -> None:
        mock_llm = _mock_llm_client(_valid_diagnosis_json())
        service = ReasoningService(mock_llm)

        result = await service.diagnose(_make_event(), _make_triage())

        assert result.root_cause == "ZWave USB stick needs reset"
        assert result.confidence == 0.85
        assert len(result.recommended_actions) == 1
        mock_llm.complete.assert_awaited_once()

    async def test_llm_called_with_reasoning_role(self) -> None:
        mock_llm = _mock_llm_client(_valid_diagnosis_json())
        service = ReasoningService(mock_llm)

        await service.diagnose(_make_event(), _make_triage())

        call_kwargs = mock_llm.complete.call_args
        assert call_kwargs.kwargs["role"] == LLMRole.REASONING

    async def test_llm_unavailable_returns_safe_fallback(self) -> None:
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = LLMError("Service unavailable")
        service = ReasoningService(mock_llm)

        result = await service.diagnose(_make_event(), _make_triage())

        assert result.confidence == 0.0
        assert "llm_unavailable" in result.root_cause
        assert len(result.recommended_actions) == 1
        assert result.recommended_actions[0].risk_tier == RiskTier.ESCALATE

    async def test_parse_failure_returns_safe_fallback(self) -> None:
        mock_llm = _mock_llm_client("not valid json")
        service = ReasoningService(mock_llm)

        result = await service.diagnose(_make_event(), _make_triage())

        assert result.confidence == 0.0
        assert "parse_failure" in result.root_cause
        assert result.recommended_actions[0].risk_tier == RiskTier.ESCALATE

    async def test_entity_context_forwarded(self) -> None:
        mock_llm = _mock_llm_client(_valid_diagnosis_json())
        service = ReasoningService(mock_llm)
        context = {"state": "unavailable"}

        await service.diagnose(
            _make_event(), _make_triage(), entity_context=context
        )

        call_args = mock_llm.complete.call_args
        messages = call_args.kwargs["messages"]
        user_content = messages[1]["content"]
        assert "unavailable" in user_content

    async def test_known_fixes_forwarded(self) -> None:
        mock_llm = _mock_llm_client(_valid_diagnosis_json())
        service = ReasoningService(mock_llm)
        fixes = [{"id": "test-fix"}]

        await service.diagnose(
            _make_event(), _make_triage(), known_fixes=fixes
        )

        call_args = mock_llm.complete.call_args
        messages = call_args.kwargs["messages"]
        user_content = messages[1]["content"]
        assert "test-fix" in user_content

    async def test_safe_fallback_has_notify_action(self) -> None:
        result = ReasoningService._safe_fallback(
            _make_event(), reason="test_reason"
        )

        assert result.recommended_actions[0].handler == "homeassistant"
        assert result.recommended_actions[0].operation == "notify"
        assert "test_reason" in result.recommended_actions[0].reasoning
