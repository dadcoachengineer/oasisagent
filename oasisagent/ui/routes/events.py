"""Event explorer UI routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from oasisagent.ui.auth import TokenPayload, require_viewer

router = APIRouter(tags=["events-ui"])


@router.get("/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Event explorer page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "events/list.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
        },
    )
