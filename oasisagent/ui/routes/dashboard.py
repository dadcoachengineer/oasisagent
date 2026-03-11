"""Dashboard routes with SSE real-time updates."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi.templating import Jinja2Templates

from oasisagent.ui.auth import TokenPayload, require_viewer

router = APIRouter(tags=["dashboard"])


def _get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _format_uptime(seconds: float) -> str:
    """Format seconds into a human-readable uptime string."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _gather_stats(request: Request) -> dict[str, Any]:
    """Collect current agent stats from the orchestrator."""
    orch = getattr(request.app.state, "orchestrator", None)
    start_time = getattr(request.app.state, "start_time", time.monotonic())
    uptime = time.monotonic() - start_time

    if orch is None:
        return {
            "events_processed": 0,
            "actions_taken": 0,
            "errors": 0,
            "queue_depth": 0,
            "pending_approvals": 0,
            "circuit_breaker_state": "closed",
            "uptime": _format_uptime(uptime),
        }

    queue_depth = 0
    if hasattr(orch, "_queue") and orch._queue is not None:
        queue_depth = getattr(orch._queue, "size", 0)
        if callable(queue_depth):
            queue_depth = queue_depth()

    pending = 0
    if hasattr(orch, "_pending_queue") and orch._pending_queue is not None:
        pending_count = getattr(orch._pending_queue, "pending_count", 0)
        pending = pending_count() if callable(pending_count) else pending_count

    cb_state = "closed"
    if hasattr(orch, "_circuit_breaker") and orch._circuit_breaker is not None:
        cb_state = getattr(orch._circuit_breaker, "state", "closed")
        if callable(cb_state):
            cb_state = cb_state()

    return {
        "events_processed": getattr(orch, "_events_processed", 0),
        "actions_taken": getattr(orch, "_actions_taken", 0),
        "errors": getattr(orch, "_errors", 0),
        "queue_depth": queue_depth,
        "pending_approvals": pending,
        "circuit_breaker_state": cb_state,
        "uptime": _format_uptime(uptime),
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Render the dashboard page."""
    templates = _get_templates(request)
    stats = _gather_stats(request)
    return templates.TemplateResponse(
        "dashboard/page.html",
        {
            "request": request,
            "current_user": current_user,
            "csrf_token": current_user.csrf,
            "version": request.app.version,
            "stats": stats,
        },
    )


@router.get("/events/stream")
async def sse_stream(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> EventSourceResponse:
    """SSE endpoint pushing agent stats every 2 seconds."""
    templates = _get_templates(request)

    async def generate() -> AsyncGenerator[dict[str, str]]:
        while True:
            if await request.is_disconnected():
                break
            stats = _gather_stats(request)
            # Render the stats partial as HTML
            html = templates.get_template("dashboard/_stats.html").render(stats=stats)
            yield {"event": "status", "data": html}
            await asyncio.sleep(2)

    return EventSourceResponse(generate())
