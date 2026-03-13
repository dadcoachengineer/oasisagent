"""Approval queue UI routes.

Operators can view pending RECOMMEND-tier actions and approve or reject
them from the browser.  Approve triggers the orchestrator's handler
dispatch pipeline; reject records the decision and clears MQTT state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from oasisagent.ui.auth import TokenPayload, require_operator

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from oasisagent.approval.pending import PendingQueue
    from oasisagent.orchestrator import Orchestrator

router = APIRouter(tags=["approvals-ui"])


def _get_queue(request: Request) -> PendingQueue:
    orch: Orchestrator = request.app.state.orchestrator
    return orch._pending_queue


def _base_context(
    request: Request, current_user: TokenPayload,
) -> dict[str, object]:
    return {
        "request": request,
        "current_user": current_user,
        "csrf_token": current_user.csrf,
        "version": request.app.version,
    }


# ---------------------------------------------------------------------------
# GET /approvals — list pending actions
# ---------------------------------------------------------------------------


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(
    request: Request,
    current_user: TokenPayload = Depends(require_operator),
) -> HTMLResponse:
    """List pending approval actions."""
    queue = _get_queue(request)
    pending = queue.list_pending() if queue else []

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "approvals/list.html",
        {
            **_base_context(request, current_user),
            "pending": pending,
        },
    )


# ---------------------------------------------------------------------------
# POST /approvals/{id}/approve — approve a pending action
# ---------------------------------------------------------------------------


@router.post(
    "/approvals/{action_id}/approve",
    response_class=HTMLResponse,
    name="approval_approve",
)
async def approve_action(
    request: Request,
    action_id: str,
    current_user: TokenPayload = Depends(require_operator),
) -> HTMLResponse:
    """Approve a pending action — dispatches it through the handler pipeline."""
    orch: Orchestrator = request.app.state.orchestrator
    queue = _get_queue(request)
    if queue is None:
        raise HTTPException(status_code=503, detail="Approval queue unavailable")

    pending = queue.get(action_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Action not found")

    # Delegate to orchestrator — marks approved, dispatches handler, clears MQTT
    templates = request.app.state.templates
    try:
        await orch._process_approval(action_id)
    except Exception:
        logger.exception("Failed to process approval for %s", action_id)
        # Re-fetch — action may have been expired or resolved by another channel
        updated = queue.get(action_id)
        return templates.TemplateResponse(
            "approvals/_action_card.html",
            {
                **_base_context(request, current_user),
                "action": updated or pending,
                "error": "Approval failed — action may have expired or already been resolved.",
            },
            status_code=409,
        )

    updated = queue.get(action_id)
    return templates.TemplateResponse(
        "approvals/_action_card.html",
        {
            **_base_context(request, current_user),
            "action": updated or pending,
        },
    )


# ---------------------------------------------------------------------------
# POST /approvals/{id}/reject — reject a pending action
# ---------------------------------------------------------------------------


@router.post(
    "/approvals/{action_id}/reject",
    response_class=HTMLResponse,
    name="approval_reject",
)
async def reject_action(
    request: Request,
    action_id: str,
    current_user: TokenPayload = Depends(require_operator),
) -> HTMLResponse:
    """Reject a pending action — records the decision and clears MQTT state."""
    orch: Orchestrator = request.app.state.orchestrator
    queue = _get_queue(request)
    if queue is None:
        raise HTTPException(status_code=503, detail="Approval queue unavailable")

    pending = queue.get(action_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Action not found")

    # Delegate to orchestrator — marks rejected, clears MQTT
    templates = request.app.state.templates
    try:
        await orch._process_rejection(action_id)
    except Exception:
        logger.exception("Failed to process rejection for %s", action_id)
        updated = queue.get(action_id)
        return templates.TemplateResponse(
            "approvals/_action_card.html",
            {
                **_base_context(request, current_user),
                "action": updated or pending,
                "error": "Rejection failed — action may have expired or already been resolved.",
            },
            status_code=409,
        )

    updated = queue.get(action_id)
    return templates.TemplateResponse(
        "approvals/_action_card.html",
        {
            **_base_context(request, current_user),
            "action": updated or pending,
        },
    )
