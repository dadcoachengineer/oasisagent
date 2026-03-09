"""Tests for the event correlator (§16.5).

Events for the same system within a time window are grouped. The first
event is the leader; subsequent correlated events skip the decision engine.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oasisagent.engine.correlator import EventCorrelator
from oasisagent.models import Event, EventMetadata, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    system: str = "homeassistant",
    entity_id: str = "sensor.test",
    event_id: str | None = None,
) -> Event:
    event = Event(
        source="test",
        system=system,
        event_type="state_changed",
        entity_id=entity_id,
        severity=Severity.WARNING,
        timestamp=datetime.now(UTC),
        metadata=EventMetadata(ttl=300),
    )
    if event_id is not None:
        event.id = event_id
    return event


def _age_group(correlator: EventCorrelator, system: str, seconds: float) -> None:
    """Shift a group's window_start backward to simulate elapsed time."""
    group = correlator._groups.get(system)
    if group is not None:
        correlator._groups[system] = group._replace(
            window_start=group.window_start - seconds
        )


# ---------------------------------------------------------------------------
# Disabled (window=0)
# ---------------------------------------------------------------------------


class TestCorrelationDisabled:
    def test_zero_window_passes_through(self) -> None:
        correlator = EventCorrelator(window_seconds=0)

        e1 = _make_event()
        e2 = _make_event()

        r1 = correlator.check(e1)
        r2 = correlator.check(e2)

        assert r1.is_leader is True
        assert r2.is_leader is True

    def test_zero_window_no_groups_tracked(self) -> None:
        correlator = EventCorrelator(window_seconds=0)

        correlator.check(_make_event())
        correlator.check(_make_event())

        assert correlator.active_groups == 0

    def test_zero_window_correlation_id_is_event_id(self) -> None:
        correlator = EventCorrelator(window_seconds=0)
        event = _make_event()

        result = correlator.check(event)

        assert result.correlation_id == event.id


# ---------------------------------------------------------------------------
# Time-window grouping
# ---------------------------------------------------------------------------


class TestTimeWindowGrouping:
    def test_first_event_is_leader(self) -> None:
        correlator = EventCorrelator(window_seconds=30)
        event = _make_event()

        result = correlator.check(event)

        assert result.is_leader is True
        assert result.leader_event_id is None

    def test_second_event_same_system_is_correlated(self) -> None:
        correlator = EventCorrelator(window_seconds=30)
        e1 = _make_event(entity_id="sensor.one")
        e2 = _make_event(entity_id="sensor.two")

        r1 = correlator.check(e1)
        r2 = correlator.check(e2)

        assert r1.is_leader is True
        assert r2.is_leader is False
        assert r2.leader_event_id == e1.id

    def test_different_systems_independent(self) -> None:
        correlator = EventCorrelator(window_seconds=30)
        e1 = _make_event(system="homeassistant")
        e2 = _make_event(system="docker")

        r1 = correlator.check(e1)
        r2 = correlator.check(e2)

        assert r1.is_leader is True
        assert r2.is_leader is True

    def test_correlation_id_shared_within_group(self) -> None:
        correlator = EventCorrelator(window_seconds=30)
        e1 = _make_event(entity_id="sensor.one")
        e2 = _make_event(entity_id="sensor.two")
        e3 = _make_event(entity_id="sensor.three")

        r1 = correlator.check(e1)
        r2 = correlator.check(e2)
        r3 = correlator.check(e3)

        assert r1.correlation_id == r2.correlation_id == r3.correlation_id

    def test_correlation_id_differs_across_systems(self) -> None:
        correlator = EventCorrelator(window_seconds=30)
        e1 = _make_event(system="homeassistant")
        e2 = _make_event(system="docker")

        r1 = correlator.check(e1)
        r2 = correlator.check(e2)

        assert r1.correlation_id != r2.correlation_id

    def test_window_expiry_creates_new_group(self) -> None:
        correlator = EventCorrelator(window_seconds=10)
        e1 = _make_event()

        r1 = correlator.check(e1)
        assert r1.is_leader is True

        # Age the group past the window
        _age_group(correlator, "homeassistant", 15)

        e2 = _make_event()
        r2 = correlator.check(e2)

        assert r2.is_leader is True
        assert r2.correlation_id != r1.correlation_id

    def test_multiple_correlated_events(self) -> None:
        correlator = EventCorrelator(window_seconds=30)
        events = [_make_event(entity_id=f"sensor.s{i}") for i in range(5)]

        results = [correlator.check(e) for e in events]

        # Only the first is a leader
        assert results[0].is_leader is True
        for r in results[1:]:
            assert r.is_leader is False
            assert r.leader_event_id == events[0].id

        # All share the same correlation_id
        ids = {r.correlation_id for r in results}
        assert len(ids) == 1

    def test_group_count_increments(self) -> None:
        correlator = EventCorrelator(window_seconds=30)

        for i in range(3):
            correlator.check(_make_event(entity_id=f"sensor.s{i}"))

        group = correlator._groups["homeassistant"]
        assert group.count == 3


# ---------------------------------------------------------------------------
# Group expiry and pruning
# ---------------------------------------------------------------------------


class TestGroupExpiry:
    def test_stale_groups_pruned(self) -> None:
        correlator = EventCorrelator(window_seconds=10)

        correlator.check(_make_event(system="sys_a"))
        correlator.check(_make_event(system="sys_b"))
        assert correlator.active_groups == 2

        # Age both groups past the window
        _age_group(correlator, "sys_a", 15)
        _age_group(correlator, "sys_b", 15)

        # Trigger prune via next check
        correlator.check(_make_event(system="sys_c"))

        # sys_a and sys_b pruned, sys_c is new
        assert correlator.active_groups == 1

    def test_prune_does_not_remove_active_groups(self) -> None:
        correlator = EventCorrelator(window_seconds=30)

        correlator.check(_make_event(system="active"))
        correlator.check(_make_event(system="stale"))
        _age_group(correlator, "stale", 60)

        # Trigger prune
        correlator.check(_make_event(system="another"))

        # "active" and "another" survive, "stale" pruned
        assert correlator.active_groups == 2
        assert "stale" not in correlator._groups
        assert "active" in correlator._groups
