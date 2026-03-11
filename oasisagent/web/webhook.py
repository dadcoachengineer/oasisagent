"""Webhook receiver router — ``/ingest/webhook/{source}``.

Accepts JSON payloads from external services (Radarr, Sonarr, Plex,
Proxmox, etc.) and converts them to canonical Events via per-source
configuration stored in SQLite as ``webhook_receiver`` connectors.

The connector ``name`` is the URL slug: creating a connector named
``radarr`` enables ``POST /ingest/webhook/radarr``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from oasisagent.ingestion.webhook import map_event, validate_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingestion"])


def _get_webhook_source(
    connectors: list[dict[str, Any]], source: str,
) -> dict[str, Any] | None:
    """Find the webhook_receiver connector matching the source slug."""
    for c in connectors:
        if c["type"] == "webhook_receiver" and c["name"] == source and c["enabled"]:
            return c
    return None


@router.post("/{source}")
async def receive_webhook(source: str, request: Request) -> JSONResponse:
    """Accept webhook payloads from external services.

    Looks up the source in configured ``webhook_receiver`` connectors,
    validates auth, maps to a canonical Event, and enqueues it.
    """
    store = getattr(request.app.state, "config_store", None)
    if store is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Service not ready"},
        )

    # Look up source config
    connectors = await store.list_connectors()
    source_row = _get_webhook_source(connectors, source)
    if source_row is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Unknown webhook source: {source}"},
        )

    # Build the typed config for auth and mapping
    from oasisagent.config import WebhookSourceConfig

    config = WebhookSourceConfig.model_validate(source_row["config"])

    # Auth validation
    headers = {k.lower(): v for k, v in request.headers.items()}
    query_params = dict(request.query_params)
    if not validate_auth(config, headers, query_params):
        return JSONResponse(
            status_code=401,
            content={"error": "Authentication failed"},
        )

    # Parse payload
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON payload"},
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"error": "Payload must be a JSON object"},
        )

    # Map to canonical Event and enqueue
    event = map_event(config, source, payload)

    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Orchestrator not ready"},
        )

    try:
        orchestrator._queue.put_nowait(event)
    except Exception:
        logger.exception("Failed to enqueue webhook event from %s", source)
        return JSONResponse(
            status_code=503,
            content={"error": "Event queue full"},
        )

    logger.info(
        "Webhook event enqueued: source=%s event_type=%s entity_id=%s",
        source, event.event_type, event.entity_id,
    )

    return JSONResponse(
        status_code=202,
        content={"event_id": event.id, "source": source},
    )
