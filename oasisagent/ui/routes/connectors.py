"""Connector, service, and notification CRUD UI routes.

Full implementation in PR B. Placeholder pages for now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from oasisagent.ui.auth import TokenPayload, require_viewer

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["connectors-ui"])


def _get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/connectors", response_class=HTMLResponse)
async def connectors_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """List all connectors."""
    templates = _get_templates(request)
    store = request.app.state.config_store
    rows = await store.list_connectors()
    masked = [store.mask_row("connectors", r) for r in rows]
    return templates.TemplateResponse(
        "connectors/list.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
            "rows": masked,
            "table": "connectors",
            "title": "Connectors",
        },
    )


@router.get("/services", response_class=HTMLResponse)
async def services_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """List all core services."""
    templates = _get_templates(request)
    store = request.app.state.config_store
    rows = await store.list_services()
    masked = [store.mask_row("core_services", r) for r in rows]
    return templates.TemplateResponse(
        "connectors/list.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
            "rows": masked,
            "table": "services",
            "title": "Services",
        },
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """List all notification channels."""
    templates = _get_templates(request)
    store = request.app.state.config_store
    rows = await store.list_notifications()
    masked = [store.mask_row("notification_channels", r) for r in rows]
    return templates.TemplateResponse(
        "connectors/list.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
            "rows": masked,
            "table": "notifications",
            "title": "Notification Channels",
        },
    )
