"""Config CRUD API endpoints for connectors, services, and notification channels.

Three logically grouped routers mounted under ``/api/v1/``:
- ``/api/v1/connectors/`` — Ingestion adapters
- ``/api/v1/services/`` — Handlers, LLM endpoints, audit, guardrails
- ``/api/v1/notifications/`` — Notification channels

All follow the same CRUD pattern backed by ``ConfigStore``. Secrets are
masked in responses (shown as "********") — never leaked via the API.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from oasisagent.db.api_models import RowCreate, RowResponse, RowUpdate
from oasisagent.db.config_store import ConfigStore  # noqa: TC001 — used at runtime

connectors_router = APIRouter(prefix="/connectors", tags=["connectors"])
services_router = APIRouter(prefix="/services", tags=["services"])
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])


def _get_store(request: Request) -> ConfigStore:
    """Extract the config store from app state."""
    store: ConfigStore = request.app.state.config_store
    return store


# ---------------------------------------------------------------------------
# Generic CRUD factory — avoids triplicating the same pattern
# ---------------------------------------------------------------------------


def _build_crud_routes(
    router: APIRouter,
    table: str,
    list_method: str,
    get_method: str,
    create_method: str,
    update_method: str,
    delete_method: str,
) -> None:
    """Register CRUD endpoints on *router* delegating to ConfigStore methods."""

    @router.get("/", response_model=list[RowResponse])
    async def list_rows(request: Request) -> list[dict[str, Any]]:
        store = _get_store(request)
        rows = await getattr(store, list_method)()
        return [store.mask_row(table, r) for r in rows]

    @router.get("/{row_id}", response_model=RowResponse)
    async def get_row(request: Request, row_id: int) -> dict[str, Any]:
        store = _get_store(request)
        row = await getattr(store, get_method)(row_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        return store.mask_row(table, row)

    @router.post("/", response_model=RowResponse, status_code=201)
    async def create_row(request: Request, body: RowCreate) -> dict[str, Any]:
        store = _get_store(request)
        try:
            row_id = await getattr(store, create_method)(
                body.type, body.name, body.config, enabled=body.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            msg = f"Name already exists: {body.name}"
            raise HTTPException(status_code=409, detail=msg) from exc

        row = await getattr(store, get_method)(row_id)
        return store.mask_row(table, row)  # type: ignore[arg-type]

    @router.put("/{row_id}", response_model=RowResponse)
    async def update_row(request: Request, row_id: int, body: RowUpdate) -> dict[str, Any]:
        store = _get_store(request)
        updates = body.model_dump(exclude_none=True)
        try:
            row = await getattr(store, update_method)(row_id, updates)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        return store.mask_row(table, row)

    @router.delete("/{row_id}", status_code=204)
    async def delete_row(request: Request, row_id: int) -> None:
        store = _get_store(request)
        deleted = await getattr(store, delete_method)(row_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Not found")


# Register CRUD on each router
_build_crud_routes(
    connectors_router, "connectors",
    "list_connectors", "get_connector", "create_connector",
    "update_connector", "delete_connector",
)
_build_crud_routes(
    services_router, "core_services",
    "list_services", "get_service", "create_service",
    "update_service", "delete_service",
)
_build_crud_routes(
    notifications_router, "notification_channels",
    "list_notifications", "get_notification", "create_notification",
    "update_notification", "delete_notification",
)
