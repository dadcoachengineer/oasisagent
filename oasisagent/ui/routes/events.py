"""Event explorer UI routes.

Three routes:
  GET /ui/events           — full page with filters + table
  GET /ui/events/table     — HTMX partial: table body only
  GET /ui/events/{event_id} — detail page with full timeline
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from oasisagent.ui.auth import TokenPayload, require_viewer

if TYPE_CHECKING:
    from oasisagent.audit.reader import AuditReader

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events-ui"])


def _get_audit_reader(request: Request) -> AuditReader | None:
    """Get the audit reader from app state, or None if not configured."""
    return getattr(request.app.state, "audit_reader", None)


def _safe_int(value: str | None, default: int) -> int:
    """Parse an int from a query param, falling back to default on junk input."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_event_filters(request: Request) -> dict:
    """Extract and validate event filter params from query string."""
    params = request.query_params
    return {
        "duration": params.get("duration", "24h"),
        "severity": params.get("severity") or None,
        "source": params.get("source") or None,
        "system": params.get("system") or None,
        "event_type": params.get("event_type") or None,
        "offset": _safe_int(params.get("offset"), 0),
        "limit": _safe_int(params.get("limit"), 25),
    }


def _filters_for_template(filters: dict) -> dict:
    """Convert parsed filters to template-friendly format (None → "")."""
    return {
        "duration": filters["duration"],
        "severity": filters["severity"] or "",
        "source": filters["source"] or "",
        "system": filters["system"] or "",
        "event_type": filters["event_type"] or "",
    }


@router.get("/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Event explorer page — full page with filters + table."""
    templates = request.app.state.templates
    reader = _get_audit_reader(request)

    ctx: dict = {
        "request": request,
        "current_user": current_user,
        "csrf_token": current_user.csrf,
        "version": request.app.version,
        "audit_configured": reader is not None,
    }

    if reader is not None:
        filters = _parse_event_filters(request)

        try:
            page = await reader.list_events(**filters)
            filter_options = await reader.get_filter_options()
        except Exception:
            logger.exception("Failed to query events from InfluxDB")
            page = None
            filter_options = None

        ctx["page"] = page
        ctx["filter_options"] = filter_options
        ctx["filters"] = _filters_for_template(filters)

    return templates.TemplateResponse("events/list.html", ctx)


@router.get("/events/table", response_class=HTMLResponse)
async def events_table_partial(
    request: Request,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """HTMX partial — returns table body only for filter/pagination swaps."""
    templates = request.app.state.templates
    reader = _get_audit_reader(request)

    ctx: dict = {
        "request": request,
        "current_user": current_user,
    }

    if reader is not None:
        filters = _parse_event_filters(request)

        try:
            page = await reader.list_events(**filters)
        except Exception:
            logger.exception("Failed to query events from InfluxDB")
            page = None

        ctx["page"] = page
        ctx["filters"] = _filters_for_template(filters)

    return templates.TemplateResponse("events/_table.html", ctx)


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail_page(
    request: Request,
    event_id: str,
    current_user: TokenPayload = Depends(require_viewer),
) -> HTMLResponse:
    """Event detail page with full timeline."""
    templates = request.app.state.templates
    reader = _get_audit_reader(request)

    ctx: dict = {
        "request": request,
        "current_user": current_user,
        "csrf_token": current_user.csrf,
        "version": request.app.version,
        "audit_configured": reader is not None,
        "timeline": None,
    }

    if reader is not None:
        try:
            timeline = await reader.get_event_timeline(event_id)
            ctx["timeline"] = timeline
        except Exception:
            logger.exception("Failed to query event timeline from InfluxDB")

    return templates.TemplateResponse("events/detail.html", ctx)
