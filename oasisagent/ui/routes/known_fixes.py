"""Known fixes browser UI routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from oasisagent.ui.auth import TokenPayload, require_viewer

router = APIRouter(tags=["known-fixes-ui"])


@router.get("/known-fixes", response_class=HTMLResponse)
async def known_fixes_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Read-only view of the known fixes registry."""
    templates = request.app.state.templates

    # Try to read fixes from the orchestrator's registry
    fixes: list[dict[str, Any]] = []
    orch = getattr(request.app.state, "orchestrator", None)
    if orch and hasattr(orch, "_registry") and orch._registry:
        registry = orch._registry
        if hasattr(registry, "fixes"):
            for fix in registry.fixes:
                fixes.append({
                    "id": getattr(fix, "id", "unknown"),
                    "match": getattr(fix, "match", {}),
                    "diagnosis": getattr(fix, "diagnosis", ""),
                    "risk_tier": getattr(fix, "risk_tier", ""),
                })

    return templates.TemplateResponse(
        "known_fixes/list.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
            "fixes": fixes,
        },
    )
