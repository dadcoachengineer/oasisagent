"""Notification feed UI routes.

Displays the in-app notification stream with real-time SSE updates
and inline approve/reject for RECOMMEND-tier actions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from oasisagent.ui.auth import TokenPayload, require_operator, require_viewer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from oasisagent.db.notification_store import NotificationStore
    from oasisagent.notifications.web_channel import WebNotificationChannel
    from oasisagent.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notifications-ui"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> NotificationStore:
    store = getattr(request.app.state, "notification_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Notification store unavailable")
    return store


def _get_web_channel(request: Request) -> WebNotificationChannel | None:
    return getattr(request.app.state, "web_notification_channel", None)


def _base_context(
    request: Request, current_user: TokenPayload,
) -> dict[str, Any]:
    return {
        "request": request,
        "current_user": current_user,
        "csrf_token": current_user.csrf,
        "version": request.app.version,
    }


# ---------------------------------------------------------------------------
# GET /notifications — full page
# ---------------------------------------------------------------------------


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Render the notification feed page."""
    store = _get_store(request)
    notifications = await store.list_recent(limit=50)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "notifications/list.html",
        {
            **_base_context(request, current_user),
            "notifications": notifications,
        },
    )


# ---------------------------------------------------------------------------
# GET /notifications/list — HTMX partial
# ---------------------------------------------------------------------------


@router.get("/notifications/list", response_class=HTMLResponse)
async def notifications_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    severity: str | None = None,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Return notification cards as an HTMX partial."""
    store = _get_store(request)
    notifications = await store.list_recent(
        limit=limit, offset=offset, severity=severity,
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "notifications/_card_list.html",
        {
            **_base_context(request, current_user),
            "notifications": notifications,
        },
    )


# ---------------------------------------------------------------------------
# GET /notifications/stream — SSE endpoint
# ---------------------------------------------------------------------------


@router.get("/notifications/stream")
async def notifications_stream(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> EventSourceResponse:
    """SSE endpoint for real-time notification updates."""
    channel = _get_web_channel(request)

    async def generate() -> AsyncGenerator[dict[str, str]]:
        if channel is None:
            return

        queue = channel.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": message.get("event", "notification"),
                        "data": message.get("data", ""),
                    }
                except TimeoutError:
                    # Send keepalive comment
                    yield {"event": "keepalive", "data": ""}
        finally:
            channel.unsubscribe(queue)

    return EventSourceResponse(generate())


# ---------------------------------------------------------------------------
# GET /notifications/unread-count — badge count for nav
# ---------------------------------------------------------------------------


@router.get("/notifications/unread-count", response_class=HTMLResponse)
async def unread_count(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Return unread notification count as a badge (or empty)."""
    store = _get_store(request)
    count = await store.count_unarchived()
    if count > 0:
        badge = (
            f'<span class="ml-1 px-1.5 py-0.5 text-xs font-medium '
            f'bg-red-500 text-white rounded-full">{count}</span>'
        )
        return HTMLResponse(content=badge)
    return HTMLResponse(content="")


# ---------------------------------------------------------------------------
# POST /notifications/{id}/approve — inline approve
# ---------------------------------------------------------------------------


@router.post(
    "/notifications/{notification_id}/approve",
    response_class=HTMLResponse,
    name="notification_approve",
)
async def approve_action(
    request: Request,
    notification_id: str,
    current_user: TokenPayload = Depends(require_operator),
) -> HTMLResponse:
    """Approve a RECOMMEND-tier action from the notification card."""
    store = _get_store(request)
    notification = await store.get(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    action_id = notification.get("action_id")
    if not action_id:
        raise HTTPException(status_code=400, detail="No action linked to this notification")

    # Race condition guard: check if already resolved
    if notification.get("action_status") != "pending":
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "notifications/_card.html",
            {
                **_base_context(request, current_user),
                "n": notification,
            },
        )

    # Delegate to orchestrator
    orch: Orchestrator = request.app.state.orchestrator
    templates = request.app.state.templates
    try:
        await orch._process_approval(action_id)
    except Exception:
        logger.exception("Failed to process approval for %s", action_id)
        # Re-fetch to get current state
        notification = await store.get(notification_id) or notification
        return templates.TemplateResponse(
            "notifications/_card.html",
            {
                **_base_context(request, current_user),
                "n": notification,
                "error": "Approval failed — action may have expired or already been resolved.",
            },
            status_code=409,
        )

    # Re-fetch to get updated status
    notification = await store.get(notification_id) or notification
    return templates.TemplateResponse(
        "notifications/_card.html",
        {
            **_base_context(request, current_user),
            "n": notification,
        },
    )


# ---------------------------------------------------------------------------
# POST /notifications/{id}/reject — inline reject
# ---------------------------------------------------------------------------


@router.post(
    "/notifications/{notification_id}/reject",
    response_class=HTMLResponse,
    name="notification_reject",
)
async def reject_action(
    request: Request,
    notification_id: str,
    current_user: TokenPayload = Depends(require_operator),
) -> HTMLResponse:
    """Reject a RECOMMEND-tier action from the notification card."""
    store = _get_store(request)
    notification = await store.get(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    action_id = notification.get("action_id")
    if not action_id:
        raise HTTPException(status_code=400, detail="No action linked to this notification")

    # Race condition guard
    if notification.get("action_status") != "pending":
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "notifications/_card.html",
            {
                **_base_context(request, current_user),
                "n": notification,
            },
        )

    orch: Orchestrator = request.app.state.orchestrator
    templates = request.app.state.templates
    try:
        await orch._process_rejection(action_id)
    except Exception:
        logger.exception("Failed to process rejection for %s", action_id)
        notification = await store.get(notification_id) or notification
        return templates.TemplateResponse(
            "notifications/_card.html",
            {
                **_base_context(request, current_user),
                "n": notification,
                "error": "Rejection failed — action may have expired or already been resolved.",
            },
            status_code=409,
        )

    notification = await store.get(notification_id) or notification
    return templates.TemplateResponse(
        "notifications/_card.html",
        {
            **_base_context(request, current_user),
            "n": notification,
        },
    )
