"""Event correlator — groups related events within a time window.

Cascading failures (e.g., network switch down → 10 entities unavailable)
produce a burst of events for the same system. The correlator ensures only
the first event (the "leader") is processed through the decision engine;
subsequent events within the window are tagged as CORRELATED and skipped.

ARCHITECTURE.md §16.5 defines the correlation specification.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, NamedTuple

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from oasisagent.models import Event


class CorrelationGroup(NamedTuple):
    """An active correlation group for a system."""

    leader_id: str
    correlation_id: str
    window_start: float  # time.monotonic()
    count: int


class CorrelationResult(BaseModel):
    """Result of checking an event against the correlator."""

    model_config = ConfigDict(extra="forbid")

    is_leader: bool
    leader_event_id: str | None = None
    correlation_id: str


class EventCorrelator:
    """Groups events by system within a configurable time window.

    The first event for a system within the window becomes the group
    leader and is processed normally. Subsequent events share the
    leader's correlation_id and are marked as correlated.

    Set ``window_seconds=0`` to disable correlation entirely.
    """

    def __init__(self, window_seconds: int) -> None:
        self._window = window_seconds
        self._groups: dict[str, CorrelationGroup] = {}

    @property
    def active_groups(self) -> int:
        """Number of currently tracked correlation groups."""
        return len(self._groups)

    def check(self, event: Event) -> CorrelationResult:
        """Check an event against active correlation groups.

        Returns a CorrelationResult indicating whether this event is a
        group leader or correlated to an existing leader.
        """
        # Prune stale groups on every call
        self._prune_expired()

        # Disabled — every event is its own leader
        if self._window <= 0:
            return CorrelationResult(
                is_leader=True,
                correlation_id=event.id,
            )

        now = time.monotonic()
        group = self._groups.get(event.system)

        # No active group or group expired — start a new one
        if group is None or (now - group.window_start) > self._window:
            correlation_id = str(uuid.uuid4())
            self._groups[event.system] = CorrelationGroup(
                leader_id=event.id,
                correlation_id=correlation_id,
                window_start=now,
                count=1,
            )
            return CorrelationResult(
                is_leader=True,
                correlation_id=correlation_id,
            )

        # Active group — this event is correlated
        self._groups[event.system] = group._replace(count=group.count + 1)
        return CorrelationResult(
            is_leader=False,
            leader_event_id=group.leader_id,
            correlation_id=group.correlation_id,
        )

    def _prune_expired(self) -> None:
        """Remove groups whose window has fully elapsed."""
        if self._window <= 0:
            return

        now = time.monotonic()
        expired = [
            system
            for system, group in self._groups.items()
            if (now - group.window_start) > self._window
        ]
        for system in expired:
            del self._groups[system]
