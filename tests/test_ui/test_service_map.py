"""Tests for Service Map UI routes (#218 M2b)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from oasisagent.db.topology_store import TopologyStore
from oasisagent.engine.service_graph import ServiceGraph
from oasisagent.models import TopologyEdge, TopologyNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)


def _make_node(**overrides: object) -> TopologyNode:
    defaults = {
        "entity_id": "service:homeassistant",
        "entity_type": "service",
        "display_name": "Home Assistant",
        "host_ip": "192.168.1.100",
        "source": "auto:ha-websocket",
        "manually_edited": False,
        "last_seen": _TS,
    }
    defaults.update(overrides)
    return TopologyNode(**defaults)


def _make_edge(**overrides: object) -> TopologyEdge:
    defaults = {
        "from_entity": "service:homeassistant",
        "to_entity": "host:proxmox",
        "edge_type": "runs_on",
        "source": "auto:ha-websocket",
        "manually_edited": False,
        "last_seen": _TS,
    }
    defaults.update(overrides)
    return TopologyEdge(**defaults)


def _setup_topology(client: AsyncClient) -> tuple[MagicMock, MagicMock]:
    """Attach mock TopologyStore and ServiceGraph to the orchestrator."""
    orch = client._transport.app.state.orchestrator  # type: ignore[union-attr]

    store = MagicMock(spec=TopologyStore)
    store.list_nodes = AsyncMock(return_value=[_make_node()])
    store.list_edges = AsyncMock(return_value=[_make_edge()])
    store.get_node = AsyncMock(return_value=_make_node())
    store.upsert_node = AsyncMock()
    store.delete_node = AsyncMock()
    store.upsert_edge = AsyncMock()
    store.delete_edge = AsyncMock()

    graph = MagicMock(spec=ServiceGraph)
    graph.to_d3_json = MagicMock(return_value={
        "nodes": [
            {
                "id": "service:homeassistant",
                "type": "service",
                "name": "Home Assistant",
                "ip": "192.168.1.100",
                "source": "auto:ha-websocket",
                "manually_edited": False,
            },
        ],
        "links": [
            {
                "source": "service:homeassistant",
                "target": "host:proxmox",
                "type": "runs_on",
            },
        ],
    })
    graph.load_from_db = AsyncMock()
    graph.merge_discovered = AsyncMock(return_value=[])

    orch._topology_store = store
    orch._service_graph = graph

    return store, graph


# ---------------------------------------------------------------------------
# Tests — service map page
# ---------------------------------------------------------------------------


class TestServiceMapPage:
    @pytest.mark.asyncio
    async def test_page_renders(self, auth_client: AsyncClient) -> None:
        """GET /ui/service-map returns 200 with topology data."""
        _setup_topology(auth_client)
        resp = await auth_client.get("/ui/service-map")
        assert resp.status_code == 200
        assert "Service Map" in resp.text
        assert "service:homeassistant" in resp.text

    @pytest.mark.asyncio
    async def test_page_without_topology(self, auth_client: AsyncClient) -> None:
        """Page renders gracefully when topology is not initialized."""
        orch = auth_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        orch._topology_store = None
        orch._service_graph = None

        resp = await auth_client.get("/ui/service-map")
        assert resp.status_code == 200
        assert "Service Map" in resp.text

    @pytest.mark.asyncio
    async def test_viewer_can_access(self, viewer_client: AsyncClient) -> None:
        """Viewers can access the service map page."""
        _setup_topology(viewer_client)
        resp = await viewer_client.get("/ui/service-map")
        assert resp.status_code == 200
        assert "Service Map" in resp.text


# ---------------------------------------------------------------------------
# Tests — JSON data endpoint
# ---------------------------------------------------------------------------


class TestServiceMapData:
    @pytest.mark.asyncio
    async def test_returns_d3_json(self, auth_client: AsyncClient) -> None:
        """GET /ui/service-map/data returns nodes and links."""
        _setup_topology(auth_client)
        resp = await auth_client.get("/ui/service-map/data")
        assert resp.status_code == 200

        data = resp.json()
        assert "nodes" in data
        assert "links" in data
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["id"] == "service:homeassistant"
        assert len(data["links"]) == 1
        assert data["links"][0]["type"] == "runs_on"

    @pytest.mark.asyncio
    async def test_no_topology_returns_503(self, auth_client: AsyncClient) -> None:
        """Returns 503 when service graph is not available."""
        orch = auth_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        orch._topology_store = None
        orch._service_graph = None

        resp = await auth_client.get("/ui/service-map/data")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Tests — node CRUD
# ---------------------------------------------------------------------------


class TestNodeCrud:
    @pytest.mark.asyncio
    async def test_create_node(self, auth_client: AsyncClient) -> None:
        """POST /ui/service-map/nodes creates a new node."""
        store, graph = _setup_topology(auth_client)
        store.get_node = AsyncMock(return_value=None)  # No existing node

        resp = await auth_client.post(
            "/ui/service-map/nodes",
            json={
                "entity_id": "service:new-app",
                "entity_type": "service",
                "display_name": "New App",
                "host_ip": "192.168.1.200",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "created"
        assert data["entity_id"] == "service:new-app"
        store.upsert_node.assert_awaited_once()
        graph.load_from_db.assert_awaited()

    @pytest.mark.asyncio
    async def test_create_duplicate_node(self, auth_client: AsyncClient) -> None:
        """Creating a node with existing entity_id returns 409."""
        _setup_topology(auth_client)
        resp = await auth_client.post(
            "/ui/service-map/nodes",
            json={"entity_id": "service:homeassistant"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_node_missing_id(self, auth_client: AsyncClient) -> None:
        """Missing entity_id returns 422."""
        _setup_topology(auth_client)
        resp = await auth_client.post(
            "/ui/service-map/nodes",
            json={"entity_type": "service"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_node(self, auth_client: AsyncClient) -> None:
        """PUT /ui/service-map/nodes/{id} updates and sets manually_edited."""
        store, graph = _setup_topology(auth_client)
        resp = await auth_client.put(
            "/ui/service-map/nodes/service:homeassistant",
            json={"display_name": "HA Core", "entity_type": "service"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        store.upsert_node.assert_awaited_once()
        # Verify manually_edited was set
        call_args = store.upsert_node.call_args[0][0]
        assert call_args.manually_edited is True
        graph.load_from_db.assert_awaited()

    @pytest.mark.asyncio
    async def test_update_nonexistent_node(self, auth_client: AsyncClient) -> None:
        """Updating a missing node returns 404."""
        store, _ = _setup_topology(auth_client)
        store.get_node = AsyncMock(return_value=None)

        resp = await auth_client.put(
            "/ui/service-map/nodes/service:nonexistent",
            json={"display_name": "Ghost"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_node(self, auth_client: AsyncClient) -> None:
        """DELETE /ui/service-map/nodes/{id} removes node and edges."""
        store, graph = _setup_topology(auth_client)
        resp = await auth_client.delete(
            "/ui/service-map/nodes/service:homeassistant",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        store.delete_node.assert_awaited_once_with("service:homeassistant")
        graph.load_from_db.assert_awaited()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_node(self, auth_client: AsyncClient) -> None:
        """Deleting a missing node returns 404."""
        store, _ = _setup_topology(auth_client)
        store.get_node = AsyncMock(return_value=None)

        resp = await auth_client.delete(
            "/ui/service-map/nodes/service:nonexistent",
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — edge CRUD
# ---------------------------------------------------------------------------


class TestEdgeCrud:
    @pytest.mark.asyncio
    async def test_create_edge(self, auth_client: AsyncClient) -> None:
        """POST /ui/service-map/edges creates an edge."""
        store, graph = _setup_topology(auth_client)
        resp = await auth_client.post(
            "/ui/service-map/edges",
            json={
                "from_entity": "service:homeassistant",
                "to_entity": "host:proxmox",
                "edge_type": "depends_on",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "created"
        store.upsert_edge.assert_awaited_once()
        graph.load_from_db.assert_awaited()

    @pytest.mark.asyncio
    async def test_create_edge_missing_fields(self, auth_client: AsyncClient) -> None:
        """Missing required fields returns 422."""
        _setup_topology(auth_client)
        resp = await auth_client.post(
            "/ui/service-map/edges",
            json={"from_entity": "service:homeassistant"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_edge_missing_source_node(self, auth_client: AsyncClient) -> None:
        """Edge with nonexistent source node returns 404."""
        store, _ = _setup_topology(auth_client)
        store.get_node = AsyncMock(return_value=None)

        resp = await auth_client.post(
            "/ui/service-map/edges",
            json={
                "from_entity": "service:nonexistent",
                "to_entity": "host:proxmox",
                "edge_type": "depends_on",
            },
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_edge(self, auth_client: AsyncClient) -> None:
        """DELETE /ui/service-map/edges removes an edge."""
        store, graph = _setup_topology(auth_client)
        resp = await auth_client.delete(
            "/ui/service-map/edges",
            params={
                "from_entity": "service:homeassistant",
                "to_entity": "host:proxmox",
                "edge_type": "runs_on",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        store.delete_edge.assert_awaited_once_with(
            "service:homeassistant", "host:proxmox", "runs_on",
        )
        graph.load_from_db.assert_awaited()


# ---------------------------------------------------------------------------
# Tests — discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_trigger_discovery(self, auth_client: AsyncClient) -> None:
        """POST /ui/service-map/discover triggers adapter discovery."""
        _setup_topology(auth_client)
        orch = auth_client._transport.app.state.orchestrator  # type: ignore[union-attr]

        mock_adapter = MagicMock()
        mock_adapter.name = "ha-websocket"
        mock_adapter.discover_topology = AsyncMock(
            return_value=([_make_node()], [_make_edge()]),
        )
        orch._adapters = [mock_adapter]

        resp = await auth_client.post("/ui/service-map/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["nodes_discovered"] == 1
        assert data["edges_discovered"] == 1
        mock_adapter.discover_topology.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discovery_handles_adapter_errors(self, auth_client: AsyncClient) -> None:
        """Discovery continues when individual adapters fail."""
        _setup_topology(auth_client)
        orch = auth_client._transport.app.state.orchestrator  # type: ignore[union-attr]

        failing_adapter = MagicMock()
        failing_adapter.name = "unifi"
        failing_adapter.discover_topology = AsyncMock(
            side_effect=ConnectionError("timeout"),
        )

        ok_adapter = MagicMock()
        ok_adapter.name = "ha-websocket"
        ok_adapter.discover_topology = AsyncMock(
            return_value=([_make_node()], []),
        )

        orch._adapters = [failing_adapter, ok_adapter]

        resp = await auth_client.post("/ui/service-map/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes_discovered"] == 1
        assert len(data["errors"]) == 1
        assert "unifi" in data["errors"][0]

    @pytest.mark.asyncio
    async def test_viewer_cannot_discover(self, viewer_client: AsyncClient) -> None:
        """Viewers cannot trigger discovery (requires operator)."""
        _setup_topology(viewer_client)
        resp = await viewer_client.post("/ui/service-map/discover")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests — RBAC
# ---------------------------------------------------------------------------


class TestServiceMapRbac:
    @pytest.mark.asyncio
    async def test_viewer_cannot_create_node(self, viewer_client: AsyncClient) -> None:
        """Viewers cannot create nodes."""
        _setup_topology(viewer_client)
        resp = await viewer_client.post(
            "/ui/service-map/nodes",
            json={"entity_id": "test:node"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_delete_node(self, viewer_client: AsyncClient) -> None:
        """Viewers cannot delete nodes."""
        _setup_topology(viewer_client)
        resp = await viewer_client.delete(
            "/ui/service-map/nodes/service:homeassistant",
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_create_edge(self, viewer_client: AsyncClient) -> None:
        """Viewers cannot create edges."""
        _setup_topology(viewer_client)
        resp = await viewer_client.post(
            "/ui/service-map/edges",
            json={
                "from_entity": "service:a",
                "to_entity": "service:b",
                "edge_type": "depends_on",
            },
        )
        assert resp.status_code == 403
