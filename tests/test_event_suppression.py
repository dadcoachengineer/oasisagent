"""Tests for EventSuppressionTracker — repeated-event suppression (#168)."""

from __future__ import annotations

from datetime import UTC, datetime

from oasisagent.models import Event, Severity
from oasisagent.orchestrator import EventSuppressionTracker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    entity_id: str = "camera.g5_flex",
    event_type: str = "ha-entity-unavailable-generic",
) -> Event:
    return Event(
        source="ha_websocket",
        system="homeassistant",
        event_type=event_type,
        entity_id=entity_id,
        severity=Severity.WARNING,
        timestamp=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Suppression activates after N consecutive identical events
# ---------------------------------------------------------------------------


class TestSuppressionActivation:
    """After threshold consecutive identical events, further events are suppressed."""

    def test_below_threshold_not_suppressed(self) -> None:
        tracker = EventSuppressionTracker(threshold=3)
        assert tracker.check(_event()) is False  # 1
        assert tracker.check(_event()) is False  # 2
        assert tracker.check(_event()) is False  # 3 (== threshold, log fires)

    def test_above_threshold_suppressed(self) -> None:
        tracker = EventSuppressionTracker(threshold=3)
        for _ in range(3):
            tracker.check(_event())
        # 4th and beyond are suppressed
        assert tracker.check(_event()) is True
        assert tracker.check(_event()) is True

    def test_threshold_of_one(self) -> None:
        tracker = EventSuppressionTracker(threshold=1)
        assert tracker.check(_event()) is False  # 1 == threshold
        assert tracker.check(_event()) is True   # 2 > threshold

    def test_different_entities_independent(self) -> None:
        tracker = EventSuppressionTracker(threshold=2)
        # Exhaust entity A
        tracker.check(_event(entity_id="sensor.a"))
        tracker.check(_event(entity_id="sensor.a"))
        assert tracker.check(_event(entity_id="sensor.a")) is True

        # Entity B is still fresh
        assert tracker.check(_event(entity_id="sensor.b")) is False

    def test_different_event_types_independent(self) -> None:
        tracker = EventSuppressionTracker(threshold=2)
        tracker.check(_event(event_type="unavailable"))
        tracker.check(_event(event_type="unavailable"))
        assert tracker.check(_event(event_type="unavailable")) is True

        # Same entity, different event_type — not suppressed
        assert tracker.check(_event(event_type="error")) is False


# ---------------------------------------------------------------------------
# Suppression resets on state change
# ---------------------------------------------------------------------------


class TestSuppressionReset:
    """Suppression resets when the entity produces a recovery or different event."""

    def test_recovery_event_resets_suppression(self) -> None:
        tracker = EventSuppressionTracker(threshold=2)
        entity = "camera.g5_flex"

        # Exhaust the counter
        tracker.check(_event(entity_id=entity, event_type="state_unavailable"))
        tracker.check(_event(entity_id=entity, event_type="state_unavailable"))
        assert tracker.check(_event(entity_id=entity, event_type="state_unavailable")) is True

        # Recovery event resets
        result = tracker.check(
            _event(entity_id=entity, event_type="device_reconnected")
        )
        assert result is False

        # Original event_type is no longer suppressed
        assert tracker.check(_event(entity_id=entity, event_type="state_unavailable")) is False

    def test_recovered_suffix_resets(self) -> None:
        tracker = EventSuppressionTracker(threshold=2)
        entity = "sensor.x"

        tracker.check(_event(entity_id=entity, event_type="health_check_failed"))
        tracker.check(_event(entity_id=entity, event_type="health_check_failed"))
        assert tracker.check(_event(entity_id=entity, event_type="health_check_failed")) is True

        tracker.check(_event(entity_id=entity, event_type="health_check_recovered"))

        assert tracker.check(_event(entity_id=entity, event_type="health_check_failed")) is False

    def test_renewed_suffix_resets(self) -> None:
        tracker = EventSuppressionTracker(threshold=2)
        entity = "sensor.cert"

        tracker.check(_event(entity_id=entity, event_type="certificate_expiring"))
        tracker.check(_event(entity_id=entity, event_type="certificate_expiring"))
        assert tracker.check(_event(entity_id=entity, event_type="certificate_expiring")) is True

        tracker.check(_event(entity_id=entity, event_type="certificate_renewed"))

        assert tracker.check(_event(entity_id=entity, event_type="certificate_expiring")) is False

    def test_different_event_type_resets_previous(self) -> None:
        """A non-recovery event_type for the same entity resets the old counter."""
        tracker = EventSuppressionTracker(threshold=2)
        entity = "switch.poe"

        tracker.check(_event(entity_id=entity, event_type="unavailable"))
        tracker.check(_event(entity_id=entity, event_type="unavailable"))
        assert tracker.check(_event(entity_id=entity, event_type="unavailable")) is True

        # Different (non-recovery) event type resets the "unavailable" counter
        tracker.check(_event(entity_id=entity, event_type="power_cycle_failed"))

        assert tracker.check(_event(entity_id=entity, event_type="unavailable")) is False

    def test_recovery_event_itself_not_suppressed(self) -> None:
        """Recovery events are never suppressed, even if repeated."""
        tracker = EventSuppressionTracker(threshold=1)
        entity = "sensor.x"

        # Recovery events reset on every call, so counter never accumulates
        assert tracker.check(_event(entity_id=entity, event_type="health_check_recovered")) is False
        assert tracker.check(_event(entity_id=entity, event_type="health_check_recovered")) is False
        assert tracker.check(_event(entity_id=entity, event_type="health_check_recovered")) is False


# ---------------------------------------------------------------------------
# reset() clears all state
# ---------------------------------------------------------------------------


class TestTrackerReset:
    """The reset() method clears all tracking state."""

    def test_reset_clears_suppression(self) -> None:
        tracker = EventSuppressionTracker(threshold=2)
        tracker.check(_event())
        tracker.check(_event())
        assert tracker.check(_event()) is True

        tracker.reset()

        assert tracker.check(_event()) is False
