"""Service Map UI routes — interactive D3 topology viewer (#218 M2b).

Routes:
  GET  /ui/service-map         — full page with D3 force graph
  GET  /ui/service-map/data    — JSON topology data (HTMX refresh)
  POST /ui/service-map/nodes   — create a manual node
  PUT  /ui/service-map/nodes/{entity_id} — update a node
  DELETE /ui/service-map/nodes/{entity_id} — delete a node
  POST /ui/service-map/edges   — create an edge
  DELETE /ui/service-map/edges — delete an edge
  POST /ui/service-map/discover — trigger fresh topology discovery
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from oasisagent.models import TopologyEdge, TopologyNode
from oasisagent.ui.auth import TokenPayload, require_operator, require_viewer

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

    from oasisagent.db.topology_store import TopologyStore
    from oasisagent.engine.service_graph import ServiceGraph

logger = logging.getLogger(__name__)

router = APIRouter(tags=["service-map-ui"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _get_topology_store(request: Request) -> TopologyStore:
    """Get the topology store from the orchestrator."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    store = getattr(orch, "_topology_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Topology store not initialized")
    return store


def _get_service_graph(request: Request) -> ServiceGraph:
    """Get the service graph from the orchestrator."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    graph = getattr(orch, "_service_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="Service graph not initialized")
    return graph


def _base_context(
    request: Request, current_user: TokenPayload,
) -> dict[str, Any]:
    """Context keys required by every template."""
    return {
        "request": request,
        "current_user": current_user,
        "csrf_token": current_user.csrf,
        "version": request.app.version,
    }


# ---------------------------------------------------------------------------
# GET /service-map — full page
# ---------------------------------------------------------------------------


@router.get("/service-map", response_class=HTMLResponse)
async def service_map_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Render the interactive service map page."""
    templates = _get_templates(request)

    # Gracefully handle missing topology backend
    orch = getattr(request.app.state, "orchestrator", None)
    graph = getattr(orch, "_service_graph", None) if orch else None

    topology_json: dict[str, Any] = {"nodes": [], "links": []}
    if graph is not None:
        topology_json = graph.to_d3_json()

    return templates.TemplateResponse(
        "service_map/index.html",
        {
            **_base_context(request, current_user),
            "topology_json": json.dumps(topology_json),
        },
    )


# ---------------------------------------------------------------------------
# GET /service-map/data — JSON for HTMX/JS refresh
# ---------------------------------------------------------------------------


@router.get("/service-map/data")
async def service_map_data(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> JSONResponse:
    """Return D3 JSON topology data."""
    graph = _get_service_graph(request)
    return JSONResponse(content=graph.to_d3_json())


# ---------------------------------------------------------------------------
# POST /service-map/nodes — create manual node
# ---------------------------------------------------------------------------


@router.post("/service-map/nodes")
async def create_node(
    request: Request,
    current_user: TokenPayload = Depends(require_operator),
) -> Response:
    """Create a manual topology node."""
    store = _get_topology_store(request)
    graph = _get_service_graph(request)

    body = await request.json()
    entity_id = body.get("entity_id", "").strip()
    if not entity_id:
        raise HTTPException(status_code=422, detail="entity_id is required")

    existing = await store.get_node(entity_id)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"Node already exists: {entity_id}",
        )

    node = TopologyNode(
        entity_id=entity_id,
        entity_type=body.get("entity_type", "service"),
        display_name=body.get("display_name", entity_id),
        host_ip=body.get("host_ip") or None,
        source="manual",
        manually_edited=True,
    )
    await store.upsert_node(node)
    await graph.load_from_db(store)

    return JSONResponse(
        content={"status": "created", "entity_id": entity_id},
        status_code=201,
    )


# ---------------------------------------------------------------------------
# PUT /service-map/nodes/{entity_id} — update node
# ---------------------------------------------------------------------------


@router.put("/service-map/nodes/{entity_id}")
async def update_node(
    request: Request,
    entity_id: str,
    current_user: TokenPayload = Depends(require_operator),
) -> Response:
    """Update a topology node (sets manually_edited=True)."""
    store = _get_topology_store(request)
    graph = _get_service_graph(request)

    existing = await store.get_node(entity_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Node not found")

    body = await request.json()
    updated = TopologyNode(
        entity_id=entity_id,
        entity_type=body.get("entity_type", existing.entity_type),
        display_name=body.get("display_name", existing.display_name),
        host_ip=body.get("host_ip", existing.host_ip),
        source=existing.source,
        manually_edited=True,
        last_seen=existing.last_seen,
        metadata=body.get("metadata", existing.metadata),
    )
    await store.upsert_node(updated)
    await graph.load_from_db(store)

    return JSONResponse(content={"status": "updated", "entity_id": entity_id})


# ---------------------------------------------------------------------------
# DELETE /service-map/nodes/{entity_id} — delete node
# ---------------------------------------------------------------------------


@router.delete("/service-map/nodes/{entity_id}")
async def delete_node(
    request: Request,
    entity_id: str,
    current_user: TokenPayload = Depends(require_operator),
) -> Response:
    """Delete a topology node and its edges."""
    store = _get_topology_store(request)
    graph = _get_service_graph(request)

    existing = await store.get_node(entity_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Node not found")

    await store.delete_node(entity_id)
    await graph.load_from_db(store)

    return JSONResponse(content={"status": "deleted", "entity_id": entity_id})


# ---------------------------------------------------------------------------
# POST /service-map/edges — create edge
# ---------------------------------------------------------------------------


@router.post("/service-map/edges")
async def create_edge(
    request: Request,
    current_user: TokenPayload = Depends(require_operator),
) -> Response:
    """Create a topology edge between two nodes."""
    store = _get_topology_store(request)
    graph = _get_service_graph(request)

    body = await request.json()
    from_entity = body.get("from_entity", "").strip()
    to_entity = body.get("to_entity", "").strip()
    edge_type = body.get("edge_type", "").strip()

    if not from_entity or not to_entity or not edge_type:
        raise HTTPException(
            status_code=422,
            detail="from_entity, to_entity, and edge_type are required",
        )

    # Verify both endpoints exist
    if await store.get_node(from_entity) is None:
        raise HTTPException(status_code=404, detail=f"Source node not found: {from_entity}")
    if await store.get_node(to_entity) is None:
        raise HTTPException(status_code=404, detail=f"Target node not found: {to_entity}")

    edge = TopologyEdge(
        from_entity=from_entity,
        to_entity=to_entity,
        edge_type=edge_type,
        source="manual",
        manually_edited=True,
    )
    await store.upsert_edge(edge)
    await graph.load_from_db(store)

    return JSONResponse(
        content={"status": "created", "edge": f"{from_entity}->{to_entity}"},
        status_code=201,
    )


# ---------------------------------------------------------------------------
# DELETE /service-map/edges — delete edge
# ---------------------------------------------------------------------------


@router.delete("/service-map/edges")
async def delete_edge(
    request: Request,
    from_entity: str,
    to_entity: str,
    edge_type: str,
    current_user: TokenPayload = Depends(require_operator),
) -> Response:
    """Delete a topology edge."""
    store = _get_topology_store(request)
    graph = _get_service_graph(request)

    await store.delete_edge(from_entity, to_entity, edge_type)
    await graph.load_from_db(store)

    return JSONResponse(
        content={"status": "deleted", "edge": f"{from_entity}->{to_entity}"},
    )


# ---------------------------------------------------------------------------
# POST /service-map/discover — trigger fresh discovery
# ---------------------------------------------------------------------------


@router.post("/service-map/discover")
async def trigger_discovery(
    request: Request,
    current_user: TokenPayload = Depends(require_operator),
) -> Response:
    """Trigger a fresh topology discovery from all adapters."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")

    store = getattr(orch, "_topology_store", None)
    graph = getattr(orch, "_service_graph", None)
    adapters = getattr(orch, "_adapters", [])

    if store is None or graph is None:
        raise HTTPException(status_code=503, detail="Topology not initialized")

    all_nodes = []
    all_edges = []
    errors: list[str] = []

    for adapter in adapters:
        try:
            nodes, edges = await adapter.discover_topology()
            all_nodes.extend(nodes)
            all_edges.extend(edges)
        except Exception as exc:
            errors.append(f"{adapter.name}: {exc}")
            logger.debug("Discovery failed for %s: %s", adapter.name, exc)

    diffs = []
    if all_nodes or all_edges:
        diffs = await graph.merge_discovered(all_nodes, all_edges, store)

    return JSONResponse(content={
        "status": "completed",
        "nodes_discovered": len(all_nodes),
        "edges_discovered": len(all_edges),
        "changes": len(diffs),
        "errors": errors,
    })
