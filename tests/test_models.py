"""Tests for canonical data models (Event, TriageResult, DiagnosisResult, etc.)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oasisagent.models import (
    ActionResult,
    ActionStatus,
    DiagnosisResult,
    Disposition,
    Event,
    EventMetadata,
    Notification,
    RecommendedAction,
    RiskTier,
    Severity,
    TriageResult,
    VerifyResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> Event:
    """Create an Event with sensible defaults, overridable per-field."""
    defaults: dict = {
        "source": "mqtt",
        "system": "homeassistant",
        "event_type": "automation_error",
        "entity_id": "automation.kitchen_motion_lights",
        "severity": Severity.ERROR,
        "timestamp": datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Event(**defaults)


# ---------------------------------------------------------------------------
# Enum values match spec
# ---------------------------------------------------------------------------


class TestEnumValues:
    """Verify all enum members match ARCHITECTURE.md definitions."""

    def test_severity_values(self) -> None:
        assert set(Severity) == {
            Severity.INFO,
            Severity.WARNING,
            Severity.ERROR,
            Severity.CRITICAL,
        }
        assert Severity.INFO == "info"
        assert Severity.WARNING == "warning"
        assert Severity.ERROR == "error"
        assert Severity.CRITICAL == "critical"

    def test_disposition_values(self) -> None:
        assert set(Disposition) == {
            Disposition.DROP,
            Disposition.KNOWN_PATTERN,
            Disposition.ESCALATE_T2,
            Disposition.ESCALATE_HUMAN,
        }
        assert Disposition.DROP == "drop"
        assert Disposition.KNOWN_PATTERN == "known_pattern"
        assert Disposition.ESCALATE_T2 == "escalate_t2"
        assert Disposition.ESCALATE_HUMAN == "escalate_human"

    def test_risk_tier_values(self) -> None:
        assert set(RiskTier) == {
            RiskTier.AUTO_FIX,
            RiskTier.RECOMMEND,
            RiskTier.ESCALATE,
            RiskTier.BLOCK,
        }
        assert RiskTier.AUTO_FIX == "auto_fix"
        assert RiskTier.RECOMMEND == "recommend"
        assert RiskTier.ESCALATE == "escalate"
        assert RiskTier.BLOCK == "block"

    def test_action_status_values(self) -> None:
        assert set(ActionStatus) == {
            ActionStatus.SUCCESS,
            ActionStatus.FAILURE,
            ActionStatus.SKIPPED,
            ActionStatus.TIMEOUT,
            ActionStatus.DRY_RUN,
        }
        assert ActionStatus.DRY_RUN == "dry_run"


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


class TestEvent:
    """Tests for the canonical Event model."""

    def test_construction_with_defaults(self) -> None:
        event = _make_event()
        assert len(event.id) == 36  # UUID string format
        assert event.source == "mqtt"
        assert event.system == "homeassistant"
        assert event.severity == Severity.ERROR
        assert event.payload == {}
        assert event.context == {}
        assert isinstance(event.metadata, EventMetadata)
        assert event.metadata.retry_count == 0

    def test_id_auto_generated_unique(self) -> None:
        e1 = _make_event()
        e2 = _make_event()
        assert e1.id != e2.id

    def test_id_can_be_overridden(self) -> None:
        event = _make_event(id="custom-id-123")
        assert event.id == "custom-id-123"

    def test_ingested_at_auto_set(self) -> None:
        before = datetime.now(UTC)
        event = _make_event()
        after = datetime.now(UTC)
        assert before <= event.ingested_at <= after
        assert event.ingested_at.tzinfo is not None

    def test_all_fields_explicit(self) -> None:
        ts = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
        ingested = datetime(2026, 3, 7, 12, 0, 1, tzinfo=UTC)
        meta = EventMetadata(
            correlation_id="corr-1",
            dedup_key="mqtt:automation.test:automation_error",
            ttl=600,
            retry_count=2,
        )
        event = Event(
            id="evt-123",
            source="ha_websocket",
            system="homeassistant",
            event_type="state_unavailable",
            entity_id="light.living_room",
            severity=Severity.WARNING,
            timestamp=ts,
            ingested_at=ingested,
            payload={"old_state": "on", "new_state": "unavailable"},
            context={"integration": "zwave_js"},
            metadata=meta,
        )
        assert event.id == "evt-123"
        assert event.payload["old_state"] == "on"
        assert event.context["integration"] == "zwave_js"
        assert event.metadata.correlation_id == "corr-1"
        assert event.metadata.ttl == 600

    def test_context_is_mutable(self) -> None:
        """Event.context is progressively enriched per §3 lifecycle."""
        event = _make_event()
        event.context["t1_classification"] = "deprecated_parameter"
        event.context["t1_confidence"] = 0.95
        assert event.context["t1_classification"] == "deprecated_parameter"

    def test_payload_is_mutable(self) -> None:
        event = _make_event(payload={"raw": "data"})
        event.payload["enriched"] = True
        assert event.payload["enriched"] is True

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra_field"):
            _make_event(extra_field="not allowed")

    def test_severity_from_string(self) -> None:
        event = _make_event(severity="critical")
        assert event.severity == Severity.CRITICAL

    def test_invalid_severity(self) -> None:
        with pytest.raises(ValidationError):
            _make_event(severity="fatal")


# ---------------------------------------------------------------------------
# EventMetadata
# ---------------------------------------------------------------------------


class TestEventMetadata:
    """Tests for EventMetadata."""

    def test_defaults(self) -> None:
        meta = EventMetadata()
        assert meta.correlation_id is None
        assert meta.dedup_key == ""
        assert meta.ttl == 300
        assert meta.retry_count == 0

    def test_negative_ttl_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EventMetadata(ttl=-1)

    def test_negative_retry_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EventMetadata(retry_count=-1)


# ---------------------------------------------------------------------------
# TriageResult
# ---------------------------------------------------------------------------


class TestTriageResult:
    """Tests for T1 triage output."""

    def test_construction(self) -> None:
        result = TriageResult(
            disposition=Disposition.KNOWN_PATTERN,
            confidence=0.92,
            classification="deprecated_parameter",
            summary="kelvin parameter deprecated in HA 2024.x",
            suggested_fix="Replace 'kelvin' with 'color_temp_kelvin'",
            reasoning="Matched known deprecation pattern",
        )
        assert result.disposition == Disposition.KNOWN_PATTERN
        assert result.confidence == 0.92
        assert result.suggested_fix is not None

    def test_disposition_is_enum_not_str(self) -> None:
        result = TriageResult(
            disposition="drop",
            confidence=0.5,
            classification="noise",
            summary="Transient event",
        )
        assert isinstance(result.disposition, Disposition)
        assert result.disposition == Disposition.DROP

    def test_invalid_disposition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TriageResult(
                disposition="invalid_value",
                confidence=0.5,
                classification="test",
                summary="test",
            )

    def test_confidence_below_zero(self) -> None:
        with pytest.raises(ValidationError):
            TriageResult(
                disposition=Disposition.DROP,
                confidence=-0.1,
                classification="test",
                summary="test",
            )

    def test_confidence_above_one(self) -> None:
        with pytest.raises(ValidationError):
            TriageResult(
                disposition=Disposition.DROP,
                confidence=1.1,
                classification="test",
                summary="test",
            )

    def test_roundtrip_serialization(self) -> None:
        original = TriageResult(
            disposition=Disposition.ESCALATE_T2,
            confidence=0.7,
            classification="unknown_error",
            summary="Unrecognized automation failure",
            context_package={"error_log": "traceback...", "entity_state": "unavailable"},
            reasoning="No matching known fix",
        )
        data = original.model_dump()
        restored = TriageResult.model_validate(data)
        assert restored == original
        expected = {"error_log": "traceback...", "entity_state": "unavailable"}
        assert restored.context_package == expected

    def test_optional_fields_default_none(self) -> None:
        result = TriageResult(
            disposition=Disposition.DROP,
            confidence=0.9,
            classification="noise",
            summary="Ignored",
        )
        assert result.suggested_fix is None
        assert result.context_package is None
        assert result.reasoning == ""


# ---------------------------------------------------------------------------
# DiagnosisResult and RecommendedAction
# ---------------------------------------------------------------------------


class TestDiagnosisResult:
    """Tests for T2 diagnosis output."""

    def test_construction_with_actions(self) -> None:
        action = RecommendedAction(
            description="Restart zwave_js integration",
            handler="homeassistant",
            operation="restart_integration",
            params={"integration": "zwave_js"},
            risk_tier=RiskTier.RECOMMEND,
            reasoning="Z-Wave controller may need re-init",
        )
        result = DiagnosisResult(
            root_cause="Z-Wave USB controller lost connection",
            confidence=0.85,
            recommended_actions=[action],
            risk_assessment="Medium risk — restart may briefly disconnect all Z-Wave devices",
            additional_context="Check USB hub power supply",
        )
        assert result.root_cause == "Z-Wave USB controller lost connection"
        assert len(result.recommended_actions) == 1
        assert result.recommended_actions[0].risk_tier == RiskTier.RECOMMEND

    def test_risk_tier_is_enum_not_str(self) -> None:
        action = RecommendedAction(
            description="test",
            handler="test",
            operation="test",
            risk_tier="auto_fix",
        )
        assert isinstance(action.risk_tier, RiskTier)
        assert action.risk_tier == RiskTier.AUTO_FIX

    def test_invalid_risk_tier_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecommendedAction(
                description="test",
                handler="test",
                operation="test",
                risk_tier="yolo",
            )

    def test_confidence_validation(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosisResult(root_cause="test", confidence=1.5)

    def test_roundtrip_serialization(self) -> None:
        action = RecommendedAction(
            description="Restart container",
            handler="docker",
            operation="restart_container",
            params={"container": "grafana", "timeout": 30},
            risk_tier=RiskTier.AUTO_FIX,
            reasoning="Container is unhealthy",
        )
        original = DiagnosisResult(
            root_cause="OOM kill",
            confidence=0.95,
            recommended_actions=[action],
            risk_assessment="Low risk",
            additional_context="Consider increasing memory limit",
            suggested_known_fix={"match": {"event_type": "container_unhealthy"}},
        )
        data = original.model_dump()
        restored = DiagnosisResult.model_validate(data)
        assert restored == original
        assert restored.suggested_known_fix is not None

    def test_empty_actions_default(self) -> None:
        result = DiagnosisResult(root_cause="Unknown", confidence=0.3)
        assert result.recommended_actions == []
        assert result.suggested_known_fix is None


# ---------------------------------------------------------------------------
# ActionResult and VerifyResult
# ---------------------------------------------------------------------------


class TestActionResult:
    """Tests for handler action output."""

    def test_success(self) -> None:
        result = ActionResult(
            status=ActionStatus.SUCCESS,
            details={"restarted": True},
            duration_ms=1250.5,
        )
        assert result.status == ActionStatus.SUCCESS
        assert result.error_message is None

    def test_failure_with_error(self) -> None:
        result = ActionResult(
            status=ActionStatus.FAILURE,
            error_message="Connection refused",
            duration_ms=50.0,
        )
        assert result.status == ActionStatus.FAILURE
        assert result.error_message == "Connection refused"

    def test_dry_run_status(self) -> None:
        result = ActionResult(
            status=ActionStatus.DRY_RUN,
            details={"would_have": "restarted integration zwave_js"},
        )
        assert result.status == ActionStatus.DRY_RUN

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActionResult(status=ActionStatus.SUCCESS, duration_ms=-1.0)


class TestVerifyResult:
    """Tests for handler verification output."""

    def test_verified(self) -> None:
        result = VerifyResult(verified=True, message="Entity back online")
        assert result.verified is True
        assert result.checked_at.tzinfo is not None

    def test_not_verified(self) -> None:
        result = VerifyResult(verified=False, message="Entity still unavailable")
        assert result.verified is False

    def test_checked_at_auto_set(self) -> None:
        before = datetime.now(UTC)
        result = VerifyResult(verified=True)
        after = datetime.now(UTC)
        assert before <= result.checked_at <= after


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


class TestNotification:
    """Tests for notification model."""

    def test_construction(self) -> None:
        notif = Notification(
            title="Circuit breaker tripped",
            message="Entity automation.kitchen exceeded 3 attempts in 60 minutes",
            severity=Severity.WARNING,
            event_id="evt-123",
            metadata={"entity_id": "automation.kitchen", "attempts": 3},
        )
        assert len(notif.id) == 36
        assert notif.event_id == "evt-123"
        assert notif.severity == Severity.WARNING

    def test_lifecycle_notification_no_event(self) -> None:
        """Agent start/stop notifications have no associated event."""
        notif = Notification(
            title="OasisAgent started",
            message="Agent started successfully",
        )
        assert notif.event_id is None
        assert notif.severity == Severity.INFO

    def test_id_auto_generated(self) -> None:
        n1 = Notification(title="a", message="b")
        n2 = Notification(title="a", message="b")
        assert n1.id != n2.id

    def test_timestamp_auto_set(self) -> None:
        before = datetime.now(UTC)
        notif = Notification(title="test", message="test")
        after = datetime.now(UTC)
        assert before <= notif.timestamp <= after
        assert notif.timestamp.tzinfo is not None

    def test_roundtrip_serialization(self) -> None:
        original = Notification(
            title="Fix applied",
            message="Replaced kelvin with color_temp_kelvin",
            severity=Severity.INFO,
            event_id="evt-456",
            channel="mqtt",
        )
        data = original.model_dump()
        restored = Notification.model_validate(data)
        assert restored.title == original.title
        assert restored.event_id == original.event_id

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="bogus"):
            Notification(title="test", message="test", bogus="field")
