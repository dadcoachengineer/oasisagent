"""User management UI routes (admin-only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from oasisagent.ui.auth import TokenPayload, require_admin

router = APIRouter(tags=["users-ui"])


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    current_user: TokenPayload = Depends(require_admin),
) -> HTMLResponse:
    """List all users."""
    templates = request.app.state.templates
    store = request.app.state.config_store
    users = await store.list_users()

    return templates.TemplateResponse(
        "users/list.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
            "users": users,
        },
    )
