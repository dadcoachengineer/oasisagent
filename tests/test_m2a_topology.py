"""Tests for M2a Service Topology Backend (#218).

Covers: TopologyStore CRUD, ServiceGraph merge/query, adapter discovery mocks,
manually_edited preservation, stale detection, migration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from oasisagent.db.topology_store import TopologyStore
from oasisagent.engine.service_graph import ServiceGraph
from oasisagent.models import TopologyEdge, TopologyNode

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db() -> aiosqlite.Connection:
    """In-memory SQLite database with topology tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("""
        CREATE TABLE topology_nodes (
            entity_id       TEXT PRIMARY KEY,
            entity_type     TEXT NOT NULL,
            display_name    TEXT DEFAULT '',
            host_ip         TEXT,
            source          TEXT DEFAULT 'manual',
            manually_edited INTEGER DEFAULT 0,
            last_seen       TEXT,
            metadata        TEXT DEFAULT '{}'
        )
    """)
    await conn.execute("""
        CREATE TABLE topology_edges (
            id              INTEGER PRIMARY KEY,
            from_entity     TEXT NOT NULL REFERENCES topology_nodes(entity_id),
            to_entity       TEXT NOT NULL REFERENCES topology_nodes(entity_id),
            edge_type       TEXT NOT NULL,
            source          TEXT DEFAULT 'manual',
            manually_edited INTEGER DEFAULT 0,
            last_seen       TEXT,
            UNIQUE(from_entity, to_entity, edge_type)
        )
    """)
    await conn.commit()
    yield conn
    await conn.close()


def _node(
    entity_id: str = "test:service",
    entity_type: str = "service",
    display_name: str = "Test Service",
    host_ip: str | None = "192.168.1.10",
    source: str = "auto:test",
    manually_edited: bool = False,
    **kwargs: Any,
) -> TopologyNode:
    return TopologyNode(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=display_name,
        host_ip=host_ip,
        source=source,
        manually_edited=manually_edited,
        last_seen=datetime.now(UTC),
        **kwargs,
    )


def _edge(
    from_entity: str = "a",
    to_entity: str = "b",
    edge_type: str = "depends_on",
    source: str = "auto:test",
    manually_edited: bool = False,
) -> TopologyEdge:
    return TopologyEdge(
        from_entity=from_entity,
        to_entity=to_entity,
        edge_type=edge_type,
        source=source,
        manually_edited=manually_edited,
        last_seen=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# TopologyStore CRUD
# ---------------------------------------------------------------------------


class TestTopologyStoreNodes:
    """Node CRUD operations."""

    @pytest.mark.asyncio
    async def test_upsert_and_get_node(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        node = _node(entity_id="plex:plex")
        await store.upsert_node(node)

        result = await store.get_node("plex:plex")
        assert result is not None
        assert result.entity_id == "plex:plex"
        assert result.display_name == "Test Service"
        assert result.host_ip == "192.168.1.10"

    @pytest.mark.asyncio
    async def test_list_nodes(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="a"))
        await store.upsert_node(_node(entity_id="b"))
        nodes = await store.list_nodes()
        assert len(nodes) == 2

    @pytest.mark.asyncio
    async def test_delete_node_removes_edges(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="a"))
        await store.upsert_node(_node(entity_id="b"))
        await store.upsert_edge(_edge(from_entity="a", to_entity="b"))

        await store.delete_node("a")
        edges = await store.list_edges()
        assert len(edges) == 0

    @pytest.mark.asyncio
    async def test_manually_edited_preserved_on_auto_update(
        self, db: aiosqlite.Connection,
    ) -> None:
        """Auto-discovery skips updating manually edited nodes."""
        store = TopologyStore(db)
        # Operator manually edits the node
        manual_node = _node(
            entity_id="svc",
            display_name="My Custom Name",
            manually_edited=True,
        )
        await store.upsert_node(manual_node)

        # Auto-discovery tries to overwrite
        auto_node = _node(
            entity_id="svc",
            display_name="Auto Name",
            manually_edited=False,
        )
        await store.upsert_node(auto_node)

        result = await store.get_node("svc")
        assert result is not None
        assert result.display_name == "My Custom Name"
        assert result.manually_edited is True

    @pytest.mark.asyncio
    async def test_manually_edited_last_seen_updated(
        self, db: aiosqlite.Connection,
    ) -> None:
        """Auto-discovery updates last_seen even for manually edited nodes."""
        store = TopologyStore(db)
        manual_node = _node(entity_id="svc", manually_edited=True)
        await store.upsert_node(manual_node)

        old_node = await store.get_node("svc")
        assert old_node is not None
        old_last_seen = old_node.last_seen

        # Auto-discovery pass
        auto_node = _node(entity_id="svc", manually_edited=False)
        await store.upsert_node(auto_node)

        new_node = await store.get_node("svc")
        assert new_node is not None
        assert new_node.last_seen is not None
        assert old_last_seen is not None
        assert new_node.last_seen >= old_last_seen


class TestTopologyStoreEdges:
    """Edge CRUD operations."""

    @pytest.mark.asyncio
    async def test_upsert_and_list_edges(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="a"))
        await store.upsert_node(_node(entity_id="b"))
        await store.upsert_edge(_edge(from_entity="a", to_entity="b"))
        edges = await store.list_edges()
        assert len(edges) == 1
        assert edges[0].from_entity == "a"
        assert edges[0].to_entity == "b"

    @pytest.mark.asyncio
    async def test_delete_edge(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="a"))
        await store.upsert_node(_node(entity_id="b"))
        await store.upsert_edge(_edge(from_entity="a", to_entity="b"))
        await store.delete_edge("a", "b", "depends_on")
        edges = await store.list_edges()
        assert len(edges) == 0


# ---------------------------------------------------------------------------
# ServiceGraph queries
# ---------------------------------------------------------------------------


class TestServiceGraphQueries:
    """In-memory graph traversal."""

    @pytest.mark.asyncio
    async def test_services_on_host(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="svc1", host_ip="10.0.0.1"))
        await store.upsert_node(_node(entity_id="svc2", host_ip="10.0.0.1"))
        await store.upsert_node(_node(entity_id="svc3", host_ip="10.0.0.2"))

        graph = ServiceGraph()
        await graph.load_from_db(store)

        result = graph.services_on_host("10.0.0.1")
        assert set(result) == {"svc1", "svc2"}
        assert graph.services_on_host("10.0.0.99") == []

    @pytest.mark.asyncio
    async def test_depends_on_and_dependents(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="web"))
        await store.upsert_node(_node(entity_id="db"))
        await store.upsert_edge(_edge(from_entity="web", to_entity="db", edge_type="depends_on"))

        graph = ServiceGraph()
        await graph.load_from_db(store)

        assert graph.depends_on("web") == ["db"]
        assert graph.dependents_of("db") == ["web"]
        assert graph.depends_on("db") == []

    @pytest.mark.asyncio
    async def test_host_for_service(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="svc", host_ip="10.0.0.5"))

        graph = ServiceGraph()
        await graph.load_from_db(store)

        assert graph.host_for_service("svc") == "10.0.0.5"
        assert graph.host_for_service("nonexistent") is None

    def test_subnet_for_ip(self) -> None:
        graph = ServiceGraph()
        assert graph.subnet_for_ip("192.168.1.100") == "192.168.1.0/24"
        assert graph.subnet_for_ip("10.0.0.1") == "10.0.0.0/24"
        assert graph.subnet_for_ip("not-an-ip") is None

    @pytest.mark.asyncio
    async def test_devices_serving_subnet(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(
            entity_id="switch1", entity_type="network_device", host_ip="192.168.1.1",
        ))
        await store.upsert_node(_node(
            entity_id="svc1", entity_type="service", host_ip="192.168.1.10",
        ))

        graph = ServiceGraph()
        await graph.load_from_db(store)

        result = graph.devices_serving_subnet("192.168.1.0/24")
        assert result == ["switch1"]

    @pytest.mark.asyncio
    async def test_to_d3_json(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="a", display_name="A"))
        await store.upsert_node(_node(entity_id="b", display_name="B"))
        await store.upsert_edge(_edge(from_entity="a", to_entity="b"))

        graph = ServiceGraph()
        await graph.load_from_db(store)
        d3 = graph.to_d3_json()

        assert len(d3["nodes"]) == 2
        assert len(d3["links"]) == 1
        assert d3["links"][0]["source"] == "a"
        assert d3["links"][0]["target"] == "b"


# ---------------------------------------------------------------------------
# Merge and stale detection
# ---------------------------------------------------------------------------


class TestServiceGraphMerge:
    """Merge logic and stale detection."""

    @pytest.mark.asyncio
    async def test_merge_new_nodes(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        graph = ServiceGraph()
        await graph.load_from_db(store)

        new_nodes = [_node(entity_id="new_svc")]
        diffs = await graph.merge_discovered(new_nodes, [], store)

        assert len(diffs) == 1
        assert diffs[0].action == "added"
        assert diffs[0].entity_id == "new_svc"
        assert graph.get_node("new_svc") is not None

    @pytest.mark.asyncio
    async def test_merge_preserves_manually_edited(
        self, db: aiosqlite.Connection,
    ) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(
            entity_id="manual_svc",
            display_name="Custom",
            manually_edited=True,
        ))

        graph = ServiceGraph()
        await graph.load_from_db(store)

        auto_node = _node(entity_id="manual_svc", display_name="Auto")
        await graph.merge_discovered([auto_node], [], store)

        node = graph.get_node("manual_svc")
        assert node is not None
        assert node.display_name == "Custom"

    @pytest.mark.asyncio
    async def test_stale_detection(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        old_time = datetime.now(UTC) - timedelta(hours=1)
        await store.upsert_node(_node(entity_id="stale_svc"))

        # Manually set last_seen to old time
        await db.execute(
            "UPDATE topology_nodes SET last_seen = ? WHERE entity_id = ?",
            (old_time.isoformat(), "stale_svc"),
        )
        await db.commit()

        graph = ServiceGraph()
        await graph.load_from_db(store)

        stale = graph.detect_stale(max_missed_cycles=3, cycle_seconds=300)
        assert len(stale) == 1
        assert stale[0].action == "stale"
        assert stale[0].entity_id == "stale_svc"

    @pytest.mark.asyncio
    async def test_fresh_nodes_not_stale(self, db: aiosqlite.Connection) -> None:
        store = TopologyStore(db)
        await store.upsert_node(_node(entity_id="fresh_svc"))

        graph = ServiceGraph()
        await graph.load_from_db(store)

        stale = graph.detect_stale(max_missed_cycles=3, cycle_seconds=300)
        assert len(stale) == 0


# ---------------------------------------------------------------------------
# Adapter discovery mocks
# ---------------------------------------------------------------------------


class TestAdapterDiscovery:
    """Adapter discover_topology() returns valid nodes."""

    @pytest.mark.asyncio
    async def test_base_adapter_returns_empty(self) -> None:
        """Default IngestAdapter.discover_topology returns empty."""
        from oasisagent.ingestion.base import IngestAdapter

        class Stub(IngestAdapter):
            @property
            def name(self) -> str:
                return "stub"

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

            async def healthy(self) -> bool:
                return True

        stub = Stub(queue=AsyncMock())
        nodes, edges = await stub.discover_topology()
        assert nodes == []
        assert edges == []
