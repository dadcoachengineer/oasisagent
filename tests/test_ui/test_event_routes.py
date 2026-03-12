"""Tests for Event Explorer UI routes."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from oasisagent.audit.reader import (
    ActionRecord,
    DecisionRecord,
    EventPage,
    EventRow,
    EventTimeline,
    FilterOptions,
    VerifyRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)


def _make_event_row(**overrides: object) -> EventRow:
    defaults = {
        "event_id": "evt-001",
        "timestamp": _TS,
        "source": "mqtt",
        "system": "homeassistant",
        "event_type": "state_unavailable",
        "entity_id": "light.kitchen",
        "severity": "warning",
    }
    defaults.update(overrides)
    return EventRow(**defaults)


def _make_filter_options() -> FilterOptions:
    return FilterOptions(
        severities=["info", "warning", "critical"],
        sources=["mqtt", "ha-websocket"],
        systems=["homeassistant"],
        event_types=["state_unavailable", "state_changed"],
        dispositions=["remediated", "escalated"],
    )


def _make_page(rows: list[EventRow] | None = None) -> EventPage:
    return EventPage(
        rows=[_make_event_row()] if rows is None else rows,
        offset=0,
        limit=25,
        has_next=False,
        has_prev=False,
    )


def _make_timeline() -> EventTimeline:
    return EventTimeline(
        event=_make_event_row(),
        payload={"old_state": "on", "new_state": "unavailable"},
        correlation_id="corr-123",
        decision=DecisionRecord(
            tier="t0",
            disposition="remediated",
            risk_tier="auto",
            diagnosis="Known fix matched",
            matched_fix_id="fix-001",
            timestamp=_TS,
        ),
        actions=[
            ActionRecord(
                handler="homeassistant",
                operation="restart_integration",
                status="success",
                duration_ms=1234.5,
                error_message="",
                timestamp=_TS,
            ),
        ],
        verifications=[
            VerifyRecord(
                handler="homeassistant",
                operation="restart_integration",
                verified=True,
                message="Integration back online",
                timestamp=_TS,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Tests — events list page
# ---------------------------------------------------------------------------


class TestEventsPage:
    @pytest.mark.asyncio
    async def test_no_audit_reader(self, auth_client: AsyncClient) -> None:
        """Without audit_reader, shows 'not configured' message."""
        # auth_client fixture doesn't set audit_reader by default
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "requires audit" in resp.text.lower() or "influxdb" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_with_audit_reader(self, auth_client: AsyncClient) -> None:
        """With a mock audit_reader, shows the table."""
        mock_reader = AsyncMock()
        mock_reader.list_events = AsyncMock(return_value=_make_page())
        mock_reader.get_filter_options = AsyncMock(return_value=_make_filter_options())
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "evt-001" in resp.text
        assert "light.kitchen" in resp.text
        assert "warning" in resp.text

    @pytest.mark.asyncio
    async def test_empty_results(self, auth_client: AsyncClient) -> None:
        """With audit_reader but no events, shows empty message."""
        mock_reader = AsyncMock()
        mock_reader.list_events = AsyncMock(return_value=_make_page(rows=[]))
        mock_reader.get_filter_options = AsyncMock(return_value=_make_filter_options())
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "No events recorded" in resp.text

    @pytest.mark.asyncio
    async def test_query_error_handled(self, auth_client: AsyncClient) -> None:
        """Query errors are caught, page still renders."""
        mock_reader = AsyncMock()
        mock_reader.list_events = AsyncMock(side_effect=Exception("connection refused"))
        mock_reader.get_filter_options = AsyncMock(
            side_effect=Exception("connection refused")
        )
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_malformed_offset_limit_defaults(self, auth_client: AsyncClient) -> None:
        """Non-numeric offset/limit falls back to defaults instead of 500."""
        mock_reader = AsyncMock()
        mock_reader.list_events = AsyncMock(return_value=_make_page())
        mock_reader.get_filter_options = AsyncMock(return_value=_make_filter_options())
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events?offset=abc&limit=xyz")
        assert resp.status_code == 200
        call_kwargs = mock_reader.list_events.call_args[1]
        assert call_kwargs["offset"] == 0
        assert call_kwargs["limit"] == 25


# ---------------------------------------------------------------------------
# Tests — HTMX table partial
# ---------------------------------------------------------------------------


class TestEventsTablePartial:
    @pytest.mark.asyncio
    async def test_table_partial(self, auth_client: AsyncClient) -> None:
        mock_reader = AsyncMock()
        mock_reader.list_events = AsyncMock(return_value=_make_page())
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events/table")
        assert resp.status_code == 200
        assert "evt-001" in resp.text
        # Should NOT contain full page structure (no <html>)
        assert "<html" not in resp.text

    @pytest.mark.asyncio
    async def test_table_partial_with_filters(self, auth_client: AsyncClient) -> None:
        mock_reader = AsyncMock()
        mock_reader.list_events = AsyncMock(return_value=_make_page())
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get(
            "/ui/events/table?severity=critical&duration=7d&offset=0"
        )
        assert resp.status_code == 200
        mock_reader.list_events.assert_awaited_once()
        call_kwargs = mock_reader.list_events.call_args[1]
        assert call_kwargs["severity"] == "critical"
        assert call_kwargs["duration"] == "7d"


# ---------------------------------------------------------------------------
# Tests — event detail page
# ---------------------------------------------------------------------------


class TestEventDetailPage:
    @pytest.mark.asyncio
    async def test_no_audit_reader(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "requires audit" in resp.text.lower() or "influxdb" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_event_not_found(self, auth_client: AsyncClient) -> None:
        mock_reader = AsyncMock()
        mock_reader.get_event_timeline = AsyncMock(return_value=None)
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events/nonexistent")
        assert resp.status_code == 200
        assert "not found" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_full_timeline(self, auth_client: AsyncClient) -> None:
        mock_reader = AsyncMock()
        mock_reader.get_event_timeline = AsyncMock(return_value=_make_timeline())
        auth_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "light.kitchen" in resp.text
        assert "remediated" in resp.text
        assert "restart_integration" in resp.text
        assert "Integration back online" in resp.text
        assert "corr-123" in resp.text

    @pytest.mark.asyncio
    async def test_viewer_can_access(self, viewer_client: AsyncClient) -> None:
        mock_reader = AsyncMock()
        mock_reader.get_event_timeline = AsyncMock(return_value=_make_timeline())
        viewer_client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]

        resp = await viewer_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "light.kitchen" in resp.text
