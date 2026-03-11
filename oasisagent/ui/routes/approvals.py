"""Approval queue UI routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from oasisagent.ui.auth import TokenPayload, require_operator

router = APIRouter(tags=["approvals-ui"])


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(
    request: Request,
    current_user: TokenPayload = Depends(require_operator),
) -> HTMLResponse:
    """List pending approval actions."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "approvals/list.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
            "pending": [],
        },
    )
