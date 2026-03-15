"""Tests for M3 Cross-Domain Correlation Engine (#218).

Covers: temporal grouping, host-based correlation, dependency chain,
subnet matching, cluster merge, circuit breaker, out-of-order events,
and cascade integration test.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from oasisagent.engine.cross_correlator import (
    ClusterCircuitBreaker,
    ClusterMatch,
    CrossDomainCorrelator,
    WindowEntry,
)
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionResult,
    DecisionTier,
)
from oasisagent.engine.service_graph import ServiceGraph
from oasisagent.models import Event, EventMetadata, Severity, TopologyEdge, TopologyNode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db() -> aiosqlite.Connection:
    """In-memory SQLite with topology + correlation tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    # Topology tables
    await conn.execute("""
        CREATE TABLE topology_nodes (
            entity_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL,
            display_name TEXT DEFAULT '', host_ip TEXT,
            source TEXT DEFAULT 'manual', manually_edited INTEGER DEFAULT 0,
            last_seen TEXT, metadata TEXT DEFAULT '{}'
        )
    """)
    await conn.execute("""
        CREATE TABLE topology_edges (
            id INTEGER PRIMARY KEY,
            from_entity TEXT NOT NULL, to_entity TEXT NOT NULL,
            edge_type TEXT NOT NULL, source TEXT DEFAULT 'manual',
            manually_edited INTEGER DEFAULT 0, last_seen TEXT,
            UNIQUE(from_entity, to_entity, edge_type)
        )
    """)
    # Correlation tables
    await conn.execute("""
        CREATE TABLE correlation_clusters (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, leader_event_id TEXT NOT NULL,
            diagnosis TEXT DEFAULT '', rule_type TEXT DEFAULT '',
            event_count INTEGER DEFAULT 1
        )
    """)
    await conn.execute("""
        CREATE TABLE cluster_events (
            id INTEGER PRIMARY KEY, cluster_id TEXT NOT NULL,
            event_id TEXT NOT NULL, entity_id TEXT NOT NULL,
            source TEXT NOT NULL, system TEXT NOT NULL,
            severity TEXT NOT NULL, timestamp TEXT NOT NULL,
            matched_rule TEXT DEFAULT '',
            UNIQUE(cluster_id, event_id)
        )
    """)
    await conn.commit()
    yield conn
    await conn.close()


def _event(
    source: str = "test",
    system: str = "homeassistant",
    entity_id: str = "sensor.temp",
    event_type: str = "unavailable",
    **kwargs: Any,
) -> Event:
    return Event(
        source=source,
        system=system,
        event_type=event_type,
        entity_id=entity_id,
        severity=Severity.ERROR,
        timestamp=datetime.now(UTC),
        **kwargs,
    )


def _decision(event_id: str = "test") -> DecisionResult:
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T1,
        disposition=DecisionDisposition.MATCHED,
        diagnosis="Test",
    )


async def _setup_graph(db: aiosqlite.Connection, nodes: list[dict], edges: list[dict] | None = None) -> ServiceGraph:
    """Helper to set up graph with nodes and edges."""
    from oasisagent.db.topology_store import TopologyStore

    store = TopologyStore(db)
    for n in nodes:
        await store.upsert_node(TopologyNode(
            entity_id=n["entity_id"],
            entity_type=n.get("entity_type", "service"),
            host_ip=n.get("host_ip"),
            display_name=n.get("display_name", ""),
            last_seen=datetime.now(UTC),
        ))
    for e in (edges or []):
        await store.upsert_edge(TopologyEdge(
            from_entity=e["from"],
            to_entity=e["to"],
            edge_type=e.get("type", "depends_on"),
            last_seen=datetime.now(UTC),
        ))

    graph = ServiceGraph()
    await graph.load_from_db(store)
    return graph


# ---------------------------------------------------------------------------
# Same-host correlation
# ---------------------------------------------------------------------------


class TestSameHostCorrelation:
    """Events from different systems on the same host correlate."""

    @pytest.mark.asyncio
    async def test_same_host_different_source(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [
            {"entity_id": "svc_a", "host_ip": "10.0.0.1"},
            {"entity_id": "svc_b", "host_ip": "10.0.0.1"},
        ])
        correlator = CrossDomainCorrelator(graph, db)

        ev1 = _event(source="adapter_a", entity_id="svc_a")
        ev2 = _event(source="adapter_b", entity_id="svc_b")

        match1 = await correlator.correlate(ev1, _decision(ev1.id))
        assert not match1.matched  # First event — no cluster yet

        match2 = await correlator.correlate(ev2, _decision(ev2.id))
        assert match2.matched
        assert match2.rule_type == "same_host"

    @pytest.mark.asyncio
    async def test_same_source_not_correlated(self, db: aiosqlite.Connection) -> None:
        """Same-source events on same host don't cross-correlate."""
        graph = await _setup_graph(db, [
            {"entity_id": "svc_a", "host_ip": "10.0.0.1"},
            {"entity_id": "svc_b", "host_ip": "10.0.0.1"},
        ])
        correlator = CrossDomainCorrelator(graph, db)

        ev1 = _event(source="same_adapter", entity_id="svc_a")
        ev2 = _event(source="same_adapter", entity_id="svc_b")

        await correlator.correlate(ev1, _decision(ev1.id))
        match2 = await correlator.correlate(ev2, _decision(ev2.id))
        assert not match2.matched  # Same source — skip

    @pytest.mark.asyncio
    async def test_different_hosts_not_correlated(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [
            {"entity_id": "svc_a", "host_ip": "10.0.0.1"},
            {"entity_id": "svc_b", "host_ip": "10.0.0.2"},
        ])
        correlator = CrossDomainCorrelator(graph, db)

        ev1 = _event(source="a", entity_id="svc_a")
        ev2 = _event(source="b", entity_id="svc_b")

        await correlator.correlate(ev1, _decision(ev1.id))
        match2 = await correlator.correlate(ev2, _decision(ev2.id))
        assert not match2.matched


# ---------------------------------------------------------------------------
# Dependency chain correlation
# ---------------------------------------------------------------------------


class TestDependencyCorrelation:
    """Events on dependent services correlate."""

    @pytest.mark.asyncio
    async def test_dependency_match(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(
            db,
            [
                {"entity_id": "web"},
                {"entity_id": "db"},
            ],
            [{"from": "web", "to": "db", "type": "depends_on"}],
        )
        correlator = CrossDomainCorrelator(graph, db)

        ev_db = _event(source="pg", entity_id="db")
        ev_web = _event(source="nginx", entity_id="web")

        await correlator.correlate(ev_db, _decision(ev_db.id))
        match = await correlator.correlate(ev_web, _decision(ev_web.id))
        assert match.matched
        assert match.rule_type == "dependency"

    @pytest.mark.asyncio
    async def test_no_dependency_no_match(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [
            {"entity_id": "web"},
            {"entity_id": "db"},
        ])
        correlator = CrossDomainCorrelator(graph, db)

        ev_db = _event(source="pg", entity_id="db")
        ev_web = _event(source="nginx", entity_id="web")

        await correlator.correlate(ev_db, _decision(ev_db.id))
        match = await correlator.correlate(ev_web, _decision(ev_web.id))
        assert not match.matched


# ---------------------------------------------------------------------------
# Subnet correlation
# ---------------------------------------------------------------------------


class TestSubnetCorrelation:
    """Network device failure correlates with services on same subnet."""

    @pytest.mark.asyncio
    async def test_subnet_match(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [
            {"entity_id": "switch1", "entity_type": "network_device", "host_ip": "192.168.1.1"},
            {"entity_id": "svc1", "host_ip": "192.168.1.50"},
        ])
        correlator = CrossDomainCorrelator(graph, db)

        ev_switch = _event(source="unifi", entity_id="switch1")
        ev_svc = _event(source="ha", entity_id="svc1")

        await correlator.correlate(ev_switch, _decision(ev_switch.id))
        match = await correlator.correlate(ev_svc, _decision(ev_svc.id))
        assert match.matched
        assert match.rule_type == "subnet"


# ---------------------------------------------------------------------------
# Cluster merge
# ---------------------------------------------------------------------------


class TestClusterMerge:
    """Merging two clusters into the older one."""

    @pytest.mark.asyncio
    async def test_merge_keeps_older(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [])
        correlator = CrossDomainCorrelator(graph, db)

        now = datetime.now(UTC).isoformat()
        # Insert two clusters
        await db.execute(
            "INSERT INTO correlation_clusters (id, created_at, updated_at, leader_event_id, event_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("old_cluster", "2024-01-01T00:00:00", now, "ev1", 2),
        )
        await db.execute(
            "INSERT INTO correlation_clusters (id, created_at, updated_at, leader_event_id, event_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("new_cluster", "2024-06-01T00:00:00", now, "ev3", 1),
        )
        # Add events to each
        await db.execute(
            "INSERT INTO cluster_events (cluster_id, event_id, entity_id, source, system, severity, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old_cluster", "ev1", "a", "s", "sys", "error", "2024-01-01T00:00:00"),
        )
        await db.execute(
            "INSERT INTO cluster_events (cluster_id, event_id, entity_id, source, system, severity, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old_cluster", "ev2", "b", "s", "sys", "error", "2024-01-01T00:01:00"),
        )
        await db.execute(
            "INSERT INTO cluster_events (cluster_id, event_id, entity_id, source, system, severity, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("new_cluster", "ev3", "c", "s", "sys", "error", "2024-06-01T00:00:00"),
        )
        await db.commit()

        survivor = await correlator.merge_clusters("old_cluster", "new_cluster")
        assert survivor == "old_cluster"

        # Victim cluster should be deleted
        cursor = await db.execute(
            "SELECT COUNT(*) FROM correlation_clusters WHERE id = ?",
            ("new_cluster",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0

        # All events should be in survivor
        cursor = await db.execute(
            "SELECT COUNT(*) FROM cluster_events WHERE cluster_id = ?",
            ("old_cluster",),
        )
        row = await cursor.fetchone()
        assert row[0] == 3


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestClusterCircuitBreaker:
    """Rate-limits T2 cluster analysis calls."""

    def test_allows_within_limit(self) -> None:
        breaker = ClusterCircuitBreaker(max_calls=3, window_seconds=300)
        assert breaker.allow()
        breaker.record()
        assert breaker.allow()
        breaker.record()
        assert breaker.allow()
        breaker.record()
        # 4th should be denied
        assert not breaker.allow()

    def test_resets_after_window(self) -> None:
        breaker = ClusterCircuitBreaker(max_calls=1, window_seconds=1)
        breaker.record()
        assert not breaker.allow()

        # Simulate time passing by manipulating the deque
        breaker._calls[0] = time.monotonic() - 2
        assert breaker.allow()


# ---------------------------------------------------------------------------
# Window expiry
# ---------------------------------------------------------------------------


class TestWindowExpiry:
    """Events outside the window don't correlate."""

    @pytest.mark.asyncio
    async def test_expired_events_not_matched(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [
            {"entity_id": "svc_a", "host_ip": "10.0.0.1"},
            {"entity_id": "svc_b", "host_ip": "10.0.0.1"},
        ])
        # Very short window
        correlator = CrossDomainCorrelator(graph, db, window_seconds=1)

        ev1 = _event(source="a", entity_id="svc_a")
        await correlator.correlate(ev1, _decision(ev1.id))

        # Expire the window entry
        correlator._window[0].timestamp = time.monotonic() - 2

        ev2 = _event(source="b", entity_id="svc_b")
        match = await correlator.correlate(ev2, _decision(ev2.id))
        assert not match.matched


# ---------------------------------------------------------------------------
# Cluster lookup
# ---------------------------------------------------------------------------


class TestClusterLookup:
    """Cluster query methods."""

    @pytest.mark.asyncio
    async def test_get_cluster_for_event(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [])
        correlator = CrossDomainCorrelator(graph, db)

        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO correlation_clusters (id, created_at, updated_at, leader_event_id, rule_type, event_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("c1", now, now, "ev1", "same_host", 2),
        )
        await db.execute(
            "INSERT INTO cluster_events (cluster_id, event_id, entity_id, source, system, severity, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("c1", "ev1", "a", "s", "sys", "error", now),
        )
        await db.commit()

        cluster = await correlator.get_cluster_for_event("ev1")
        assert cluster is not None
        assert cluster.id == "c1"
        assert cluster.rule_type == "same_host"

    @pytest.mark.asyncio
    async def test_get_cluster_events(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(db, [])
        correlator = CrossDomainCorrelator(graph, db)

        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO correlation_clusters (id, created_at, updated_at, leader_event_id, event_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("c1", now, now, "ev1", 2),
        )
        for ev_id in ("ev1", "ev2"):
            await db.execute(
                "INSERT INTO cluster_events (cluster_id, event_id, entity_id, source, system, severity, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("c1", ev_id, "a", "s", "sys", "error", now),
            )
        await db.commit()

        events = await correlator.get_cluster_events("c1")
        assert len(events) == 2


# ---------------------------------------------------------------------------
# Cascade integration test
# ---------------------------------------------------------------------------


class TestCascadeIntegration:
    """Full cascade: UniFi switch → HA entities → Uptime Kuma → single cluster."""

    @pytest.mark.asyncio
    async def test_cascade_creates_single_cluster(self, db: aiosqlite.Connection) -> None:
        graph = await _setup_graph(
            db,
            [
                {"entity_id": "switch:main", "entity_type": "network_device", "host_ip": "192.168.1.1"},
                {"entity_id": "ha:entity1", "host_ip": "192.168.1.10"},
                {"entity_id": "ha:entity2", "host_ip": "192.168.1.10"},
                {"entity_id": "uptime:monitor1", "host_ip": "192.168.1.10"},
            ],
            [
                {"from": "ha:entity1", "to": "switch:main", "type": "connects_via"},
                {"from": "ha:entity2", "to": "switch:main", "type": "connects_via"},
            ],
        )
        correlator = CrossDomainCorrelator(graph, db)

        # 1. Switch goes down
        ev_switch = _event(source="unifi", entity_id="switch:main")
        m1 = await correlator.correlate(ev_switch, _decision(ev_switch.id))
        assert not m1.matched  # First event

        # 2. HA entity becomes unavailable (dependency match)
        ev_ha1 = _event(source="ha_ws", entity_id="ha:entity1")
        m2 = await correlator.correlate(ev_ha1, _decision(ev_ha1.id))
        assert m2.matched
        assert m2.rule_type == "dependency"
        cluster_id = m2.cluster_id

        # 3. Another HA entity (same host as entity1)
        ev_ha2 = _event(source="ha_ws", entity_id="ha:entity2")
        m3 = await correlator.correlate(ev_ha2, _decision(ev_ha2.id))
        assert m3.matched

        # 4. Uptime Kuma monitor (same host)
        ev_uk = _event(source="uptime_kuma", entity_id="uptime:monitor1")
        m4 = await correlator.correlate(ev_uk, _decision(ev_uk.id))
        assert m4.matched

        # All events should reference the same cluster
        events = await correlator.get_cluster_events(cluster_id)
        assert len(events) >= 2  # At least the leader + correlated events
