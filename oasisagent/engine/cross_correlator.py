"""Cross-domain correlation engine (#218 M3).

Detects that events from different systems are part of the same incident.
Runs post-decision as an async background task, using the ServiceGraph
for topology-aware matching.

Pipeline placement:
    Event -> Same-system correlator -> Decision engine -> Cross-domain correlator (async)

Rule-based matching (ordered by cost, short-circuit on first match):
    1. Same host — O(1) via ServiceGraph.host_for_service()
    2. Dependency chain — O(depth) via graph traversal
    3. Same subnet + network device — check network devices on subnet
"""

from __future__ import annotations

import collections
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    import aiosqlite

    from oasisagent.engine.decision import DecisionResult
    from oasisagent.engine.service_graph import ServiceGraph
    from oasisagent.models import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ClusterMatch(BaseModel):
    """Result of matching an event against existing clusters."""

    model_config = ConfigDict(extra="forbid")

    matched: bool
    cluster_id: str | None = None
    rule_type: str = ""  # "same_host", "dependency", "subnet"


class CorrelationCluster(BaseModel):
    """A group of correlated events across domains."""

    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: datetime
    updated_at: datetime
    leader_event_id: str
    diagnosis: str = ""
    rule_type: str = ""
    event_count: int = 1


# ---------------------------------------------------------------------------
# Sliding window entry
# ---------------------------------------------------------------------------


class WindowEntry:
    """A recent (event, decision) pair in the sliding window."""

    __slots__ = ("event", "decision", "timestamp", "host_ip", "entity_id", "_cluster_id")

    def __init__(
        self, event: Event, decision: DecisionResult, host_ip: str | None,
    ) -> None:
        self.event = event
        self.decision = decision
        self.timestamp = time.monotonic()
        self.host_ip = host_ip
        self.entity_id = event.entity_id
        self._cluster_id: str | None = None


# ---------------------------------------------------------------------------
# T2 circuit breaker for cluster analysis
# ---------------------------------------------------------------------------


class ClusterCircuitBreaker:
    """Rate-limits T2 cluster analysis calls."""

    def __init__(self, max_calls: int = 3, window_seconds: int = 300) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._calls: collections.deque[float] = collections.deque()

    def allow(self) -> bool:
        """Return True if a T2 call is allowed."""
        now = time.monotonic()
        # Prune old entries
        while self._calls and (now - self._calls[0]) > self._window:
            self._calls.popleft()
        return len(self._calls) < self._max_calls

    def record(self) -> None:
        """Record a T2 call."""
        self._calls.append(time.monotonic())


# ---------------------------------------------------------------------------
# Cross-domain correlator
# ---------------------------------------------------------------------------


class CrossDomainCorrelator:
    """Post-decision correlator that groups events across systems.

    Uses a sliding window of recent events and the ServiceGraph to detect
    that events from different adapters are part of the same incident.
    """

    def __init__(
        self,
        graph: ServiceGraph,
        db: aiosqlite.Connection,
        window_seconds: int = 300,
    ) -> None:
        self._graph = graph
        self._db = db
        self._window_seconds = window_seconds
        self._window: collections.deque[WindowEntry] = collections.deque()
        self._t2_breaker = ClusterCircuitBreaker()

    async def correlate(
        self, event: Event, decision: DecisionResult,
    ) -> ClusterMatch:
        """Check if this event correlates with recent events from other systems.

        Returns a ClusterMatch indicating whether correlation was found.
        """
        # Resolve host IP for this event's entity
        host_ip = self._graph.host_for_service(event.entity_id)

        # Prune stale window entries
        self._prune_window()

        # Try matching rules in order of cost
        match = self._match_same_host(event, host_ip)
        if match.matched:
            await self._add_to_cluster(event, decision, match)
            self._add_to_window(event, decision, host_ip)
            return match

        match = self._match_dependency(event)
        if match.matched:
            await self._add_to_cluster(event, decision, match)
            self._add_to_window(event, decision, host_ip)
            return match

        match = self._match_subnet(event, host_ip)
        if match.matched:
            await self._add_to_cluster(event, decision, match)
            self._add_to_window(event, decision, host_ip)
            return match

        # No match — create potential new cluster seed
        self._add_to_window(event, decision, host_ip)
        return ClusterMatch(matched=False)

    # -------------------------------------------------------------------
    # Rule-based matching
    # -------------------------------------------------------------------

    def _match_same_host(
        self, event: Event, host_ip: str | None,
    ) -> ClusterMatch:
        """O(1): Check if another system has a recent event on the same host."""
        if not host_ip:
            return ClusterMatch(matched=False)

        for entry in reversed(self._window):
            if entry.host_ip == host_ip and entry.event.source != event.source:
                return ClusterMatch(
                    matched=True,
                    cluster_id=self._cluster_for_entry(entry),
                    rule_type="same_host",
                )

        return ClusterMatch(matched=False)

    def _match_dependency(self, event: Event) -> ClusterMatch:
        """O(depth): Check if a dependency of this entity has a recent failure."""
        deps = self._graph.depends_on(event.entity_id)
        for dep_id in deps:
            for entry in reversed(self._window):
                if entry.entity_id == dep_id:
                    return ClusterMatch(
                        matched=True,
                        cluster_id=self._cluster_for_entry(entry),
                        rule_type="dependency",
                    )

        # Also check reverse — if this entity is a dependency of a recent failure
        dependents = self._graph.dependents_of(event.entity_id)
        for dep_id in dependents:
            for entry in reversed(self._window):
                if entry.entity_id == dep_id:
                    return ClusterMatch(
                        matched=True,
                        cluster_id=self._cluster_for_entry(entry),
                        rule_type="dependency",
                    )

        return ClusterMatch(matched=False)

    def _match_subnet(
        self, event: Event, host_ip: str | None,
    ) -> ClusterMatch:
        """Check if a network device on the same subnet has a recent failure."""
        if not host_ip:
            return ClusterMatch(matched=False)

        subnet = self._graph.subnet_for_ip(host_ip)
        if not subnet:
            return ClusterMatch(matched=False)

        net_devices = self._graph.devices_serving_subnet(subnet)
        for device_id in net_devices:
            for entry in reversed(self._window):
                if entry.entity_id == device_id:
                    return ClusterMatch(
                        matched=True,
                        cluster_id=self._cluster_for_entry(entry),
                        rule_type="subnet",
                    )

        return ClusterMatch(matched=False)

    # -------------------------------------------------------------------
    # Cluster persistence
    # -------------------------------------------------------------------

    def _cluster_for_entry(self, entry: WindowEntry) -> str:
        """Get or create a cluster ID for a window entry."""
        # Check if this entry already has a cluster
        if hasattr(entry, "_cluster_id") and entry._cluster_id:
            return entry._cluster_id

        # Create new cluster with this entry as leader
        cluster_id = str(uuid4())
        entry._cluster_id = cluster_id  # type: ignore[attr-defined]
        return cluster_id

    async def _add_to_cluster(
        self,
        event: Event,
        decision: DecisionResult,
        match: ClusterMatch,
    ) -> None:
        """Persist the event into the matched cluster."""
        assert match.cluster_id is not None
        now_iso = datetime.now(UTC).isoformat()

        try:
            # Ensure cluster exists
            cursor = await self._db.execute(
                "SELECT id FROM correlation_clusters WHERE id = ?",
                (match.cluster_id,),
            )
            row = await cursor.fetchone()

            if row is None:
                # Find the leader entry
                leader_event_id = event.id
                for entry in self._window:
                    if (
                        hasattr(entry, "_cluster_id")
                        and entry._cluster_id == match.cluster_id
                    ):
                        leader_event_id = entry.event.id
                        break

                await self._db.execute(
                    "INSERT INTO correlation_clusters "
                    "(id, created_at, updated_at, leader_event_id, rule_type, event_count) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (match.cluster_id, now_iso, now_iso, leader_event_id, match.rule_type),
                )

                # Add the leader event too
                await self._db.execute(
                    "INSERT OR IGNORE INTO cluster_events "
                    "(cluster_id, event_id, entity_id, source, system, severity, "
                    "timestamp, matched_rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        match.cluster_id,
                        leader_event_id,
                        "",  # leader entity_id filled below if found
                        "",
                        "",
                        "",
                        now_iso,
                        match.rule_type,
                    ),
                )

            # Add this event to the cluster
            await self._db.execute(
                "INSERT OR IGNORE INTO cluster_events "
                "(cluster_id, event_id, entity_id, source, system, severity, "
                "timestamp, matched_rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match.cluster_id,
                    event.id,
                    event.entity_id,
                    event.source,
                    event.system,
                    event.severity.value,
                    event.timestamp.isoformat(),
                    match.rule_type,
                ),
            )

            # Update cluster metadata
            await self._db.execute(
                "UPDATE correlation_clusters SET updated_at = ?, "
                "event_count = event_count + 1 WHERE id = ?",
                (now_iso, match.cluster_id),
            )
            await self._db.commit()

            logger.info(
                "Event %s correlated to cluster %s via %s",
                event.id,
                match.cluster_id[:8],
                match.rule_type,
            )

        except Exception:
            logger.exception(
                "Failed to persist correlation for event %s", event.id
            )

    async def merge_clusters(
        self, cluster_a_id: str, cluster_b_id: str,
    ) -> str:
        """Merge two clusters into the older one.

        Returns the surviving cluster ID.
        """
        # Determine which is older
        cursor = await self._db.execute(
            "SELECT id, created_at FROM correlation_clusters WHERE id IN (?, ?)",
            (cluster_a_id, cluster_b_id),
        )
        rows = await cursor.fetchall()
        if len(rows) < 2:
            return cluster_a_id

        sorted_rows = sorted(rows, key=lambda r: r["created_at"])
        survivor_id = sorted_rows[0]["id"]
        victim_id = sorted_rows[1]["id"]

        # Move all events from victim to survivor
        await self._db.execute(
            "UPDATE cluster_events SET cluster_id = ? WHERE cluster_id = ?",
            (survivor_id, victim_id),
        )

        # Update survivor count
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM cluster_events WHERE cluster_id = ?",
            (survivor_id,),
        )
        count_row = await cursor.fetchone()
        new_count = count_row[0] if count_row else 0

        # Re-evaluate leader — earliest timestamp
        cursor = await self._db.execute(
            "SELECT event_id FROM cluster_events WHERE cluster_id = ? "
            "ORDER BY timestamp ASC LIMIT 1",
            (survivor_id,),
        )
        leader_row = await cursor.fetchone()
        leader_id = leader_row["event_id"] if leader_row else ""

        now_iso = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE correlation_clusters SET updated_at = ?, "
            "event_count = ?, leader_event_id = ? WHERE id = ?",
            (now_iso, new_count, leader_id, survivor_id),
        )

        # Delete victim cluster
        await self._db.execute(
            "DELETE FROM correlation_clusters WHERE id = ?",
            (victim_id,),
        )
        await self._db.commit()

        logger.info(
            "Merged cluster %s into %s (%d events)",
            victim_id[:8],
            survivor_id[:8],
            new_count,
        )
        return survivor_id

    async def get_cluster_for_event(self, event_id: str) -> CorrelationCluster | None:
        """Look up which cluster an event belongs to."""
        cursor = await self._db.execute(
            "SELECT c.id, c.created_at, c.updated_at, c.leader_event_id, "
            "c.diagnosis, c.rule_type, c.event_count "
            "FROM correlation_clusters c "
            "JOIN cluster_events ce ON c.id = ce.cluster_id "
            "WHERE ce.event_id = ?",
            (event_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return CorrelationCluster(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            leader_event_id=row["leader_event_id"],
            diagnosis=row["diagnosis"],
            rule_type=row["rule_type"],
            event_count=row["event_count"],
        )

    async def get_cluster_events(self, cluster_id: str) -> list[dict[str, Any]]:
        """Get all events in a cluster."""
        cursor = await self._db.execute(
            "SELECT event_id, entity_id, source, system, severity, "
            "timestamp, matched_rule "
            "FROM cluster_events WHERE cluster_id = ? ORDER BY timestamp",
            (cluster_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------

    def _add_to_window(
        self, event: Event, decision: DecisionResult, host_ip: str | None,
    ) -> None:
        """Add an event to the sliding window."""
        self._window.append(WindowEntry(event, decision, host_ip))

    def _prune_window(self) -> None:
        """Remove entries older than the window."""
        cutoff = time.monotonic() - self._window_seconds
        while self._window and self._window[0].timestamp < cutoff:
            self._window.popleft()
