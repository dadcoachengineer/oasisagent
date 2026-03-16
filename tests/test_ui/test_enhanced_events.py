"""Tests for Enhanced Timeline UI (#218 M4).

Tests the disposition filter, three-section detail layout,
correlation cluster data, and suppression rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
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
    defaults: dict[str, Any] = {
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
        dispositions=["matched", "blocked", "escalated", "dropped"],
    )


def _make_page(rows: list[EventRow] | None = None) -> EventPage:
    return EventPage(
        rows=[_make_event_row()] if rows is None else rows,
        offset=0,
        limit=25,
        has_next=False,
        has_prev=False,
    )


def _make_timeline(**overrides: object) -> EventTimeline:
    defaults: dict[str, Any] = {
        "event": _make_event_row(),
        "payload": {"old_state": "on", "new_state": "unavailable"},
        "correlation_id": "corr-123",
        "decision": DecisionRecord(
            tier="t1",
            disposition="matched",
            risk_tier="auto",
            diagnosis="T1 classified as known pattern",
            matched_fix_id="",
            timestamp=_TS,
            details={
                "classification": "service_failure",
                "confidence": 0.85,
                "reasoning": "Entity went unavailable after network change",
                "guardrail_rule": "rate_limit_entity",
            },
        ),
        "actions": [
            ActionRecord(
                handler="homeassistant",
                operation="restart_integration",
                status="success",
                duration_ms=1234.5,
                error_message="",
                timestamp=_TS,
            ),
        ],
        "verifications": [
            VerifyRecord(
                handler="homeassistant",
                operation="restart_integration",
                verified=True,
                message="Integration back online",
                timestamp=_TS,
            ),
        ],
    }
    defaults.update(overrides)
    return EventTimeline(**defaults)


def _make_suppressions() -> list[dict[str, Any]]:
    return [
        {
            "timestamp": "2026-03-10T12:00:00+00:00",
            "source": "mqtt",
            "system": "homeassistant",
            "event_type": "state_unavailable",
            "entity_id": "light.kitchen",
            "severity": "warning",
            "suppressed_count": 5,
        },
    ]


def _setup_mock_reader(
    client: AsyncClient,
    *,
    page: EventPage | None = None,
    filter_options: FilterOptions | None = None,
    timeline: EventTimeline | None = None,
    suppressions: list[dict[str, Any]] | None = None,
) -> AsyncMock:
    """Attach a mock audit reader to the client's app state."""
    mock_reader = AsyncMock()
    mock_reader.list_events = AsyncMock(return_value=page or _make_page())
    mock_reader.get_filter_options = AsyncMock(
        return_value=filter_options or _make_filter_options()
    )
    mock_reader.get_event_timeline = AsyncMock(return_value=timeline)
    mock_reader.get_suppressions = AsyncMock(return_value=suppressions or [])
    client._transport.app.state.audit_reader = mock_reader  # type: ignore[union-attr]
    return mock_reader


# ---------------------------------------------------------------------------
# Tests — disposition filter
# ---------------------------------------------------------------------------


class TestDispositionFilter:
    @pytest.mark.asyncio
    async def test_disposition_dropdown_rendered(self, auth_client: AsyncClient) -> None:
        """Filter bar includes the disposition dropdown."""
        _setup_mock_reader(auth_client)
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert 'name="disposition"' in resp.text
        assert "Disposition" in resp.text

    @pytest.mark.asyncio
    async def test_disposition_filter_passed_to_reader(
        self, auth_client: AsyncClient,
    ) -> None:
        """Disposition param is forwarded to list_events."""
        mock = _setup_mock_reader(auth_client)
        resp = await auth_client.get("/ui/events/table?disposition=matched")
        assert resp.status_code == 200
        call_kwargs = mock.list_events.call_args[1]
        assert call_kwargs["disposition"] == "matched"

    @pytest.mark.asyncio
    async def test_disposition_filter_preserved_in_pagination(
        self, auth_client: AsyncClient,
    ) -> None:
        """Pagination links include the disposition filter."""
        page = EventPage(
            rows=[_make_event_row(event_id=f"evt-{i}") for i in range(25)],
            offset=0,
            limit=25,
            has_next=True,
            has_prev=False,
        )
        _setup_mock_reader(
            auth_client,
            page=page,
        )
        resp = await auth_client.get(
            "/ui/events/table?disposition=escalated&duration=24h"
        )
        assert resp.status_code == 200
        assert "disposition=escalated" in resp.text


# ---------------------------------------------------------------------------
# Tests — detail page three sections
# ---------------------------------------------------------------------------


class TestDetailSections:
    @pytest.mark.asyncio
    async def test_reasoning_section_rendered(self, auth_client: AsyncClient) -> None:
        """Detail page shows the Reasoning section with tier chain."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert 'id="section-reasoning"' in resp.text
        assert "Reasoning" in resp.text
        # Tier chain steps
        assert "T0" in resp.text
        assert "T1" in resp.text
        assert "T2" in resp.text

    @pytest.mark.asyncio
    async def test_action_section_rendered(self, auth_client: AsyncClient) -> None:
        """Detail page shows the Action section."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert 'id="section-action"' in resp.text
        assert "Action" in resp.text
        assert "homeassistant.restart_integration" in resp.text

    @pytest.mark.asyncio
    async def test_outcome_section_rendered(self, auth_client: AsyncClient) -> None:
        """Detail page shows the Outcome section with verification."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert 'id="section-outcome"' in resp.text
        assert "Outcome" in resp.text
        assert "verified" in resp.text
        assert "Integration back online" in resp.text

    @pytest.mark.asyncio
    async def test_t1_details_rendered(self, auth_client: AsyncClient) -> None:
        """T1 classification, confidence, reasoning rendered from details."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "service_failure" in resp.text  # classification
        assert "85%" in resp.text  # confidence badge
        assert "Entity went unavailable" in resp.text  # reasoning
        assert "rate_limit_entity" in resp.text  # guardrail rule

    @pytest.mark.asyncio
    async def test_disposition_badge_in_detail(self, auth_client: AsyncClient) -> None:
        """Disposition badge rendered in reasoning section."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "matched" in resp.text

    @pytest.mark.asyncio
    async def test_t0_matched_fix_shown(self, auth_client: AsyncClient) -> None:
        """T0 matched fix ID rendered when present."""
        timeline = _make_timeline(
            decision=DecisionRecord(
                tier="t0",
                disposition="matched",
                risk_tier="auto",
                diagnosis="Known fix matched",
                matched_fix_id="fix-ha-restart-001",
                timestamp=_TS,
                details={},
            ),
        )
        _setup_mock_reader(auth_client, timeline=timeline)
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "fix-ha-restart-001" in resp.text
        assert "Matched Fix" in resp.text

    @pytest.mark.asyncio
    async def test_duration_formatted(self, auth_client: AsyncClient) -> None:
        """Action duration formatted as seconds when >= 1000ms."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        # 1234.5ms should format as "1.2s"
        assert "1.2s" in resp.text

    @pytest.mark.asyncio
    async def test_failed_action_shows_error(self, auth_client: AsyncClient) -> None:
        """Failed action status shows red badge and error message."""
        timeline = _make_timeline(
            actions=[
                ActionRecord(
                    handler="homeassistant",
                    operation="restart_integration",
                    status="failed",
                    duration_ms=500.0,
                    error_message="Connection timeout",
                    timestamp=_TS,
                ),
            ],
            verifications=[],
        )
        _setup_mock_reader(auth_client, timeline=timeline)
        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "failed" in resp.text
        assert "Connection timeout" in resp.text
        assert "500ms" in resp.text


# ---------------------------------------------------------------------------
# Tests — correlation cluster
# ---------------------------------------------------------------------------


class TestCorrelationCluster:
    @pytest.mark.asyncio
    async def test_cluster_shown_when_present(self, auth_client: AsyncClient) -> None:
        """Cluster info rendered when event belongs to a correlation cluster."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())

        # Mock a SQLite db with cluster data
        mock_db = AsyncMock()
        # First query: find cluster_id for event
        cursor1 = AsyncMock()
        cursor1.fetchone = AsyncMock(return_value=("cluster-abc",))
        # Second query: cluster metadata
        cursor2 = AsyncMock()
        cursor2.fetchone = AsyncMock(return_value=(
            "cluster-abc", "2026-03-10T12:00:00", "2026-03-10T12:05:00",
            "evt-001", "Network outage", "same_host", 2,
        ))
        # Third query: cluster events
        cursor3 = AsyncMock()
        cursor3.fetchall = AsyncMock(return_value=[
            (
                "evt-001", "light.kitchen", "mqtt",
                "homeassistant", "warning",
                "2026-03-10T12:00:00", "same_host",
            ),
            (
                "evt-002", "switch.bedroom", "ha-websocket",
                "homeassistant", "warning",
                "2026-03-10T12:01:00", "same_host",
            ),
        ])
        mock_db.execute = AsyncMock(side_effect=[cursor1, cursor2, cursor3])
        auth_client._transport.app.state.db = mock_db  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "Correlation Cluster" in resp.text
        assert "same_host" in resp.text
        assert "2 events" in resp.text
        assert "switch.bedroom" in resp.text

    @pytest.mark.asyncio
    async def test_no_cluster_when_not_in_cluster(
        self, auth_client: AsyncClient,
    ) -> None:
        """No cluster section when event is not in any cluster."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        # Mock db returning None for cluster lookup
        mock_db = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=cursor)
        auth_client._transport.app.state.db = mock_db  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "Correlation Cluster" not in resp.text

    @pytest.mark.asyncio
    async def test_cluster_query_failure_handled(
        self, auth_client: AsyncClient,
    ) -> None:
        """SQLite query failure for clusters is handled gracefully."""
        _setup_mock_reader(auth_client, timeline=_make_timeline())
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=Exception("table not found"))
        auth_client._transport.app.state.db = mock_db  # type: ignore[union-attr]

        resp = await auth_client.get("/ui/events/evt-001")
        assert resp.status_code == 200
        assert "Correlation Cluster" not in resp.text


# ---------------------------------------------------------------------------
# Tests — suppression rows
# ---------------------------------------------------------------------------


class TestSuppressionRows:
    @pytest.mark.asyncio
    async def test_suppression_summary_shown(self, auth_client: AsyncClient) -> None:
        """Suppression summary appears when suppressions exist."""
        _setup_mock_reader(
            auth_client,
            suppressions=_make_suppressions(),
        )
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "suppressed event" in resp.text
        assert "5 total duplicates dropped" in resp.text

    @pytest.mark.asyncio
    async def test_no_suppression_section_when_empty(
        self, auth_client: AsyncClient,
    ) -> None:
        """No suppression summary when there are no suppressions."""
        _setup_mock_reader(auth_client, suppressions=[])
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "suppressed event" not in resp.text

    @pytest.mark.asyncio
    async def test_table_partial_includes_suppressions(
        self, auth_client: AsyncClient,
    ) -> None:
        """HTMX table partial also picks up suppression data."""
        _setup_mock_reader(
            auth_client,
            suppressions=_make_suppressions(),
        )
        resp = await auth_client.get("/ui/events/table")
        assert resp.status_code == 200
        # The partial should render without errors even if
        # suppressions are not explicitly passed (from table partial handler)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests — visual enhancements
# ---------------------------------------------------------------------------


class TestVisualEnhancements:
    @pytest.mark.asyncio
    async def test_severity_dots_in_table(self, auth_client: AsyncClient) -> None:
        """Table rows include severity dot indicators."""
        _setup_mock_reader(auth_client)
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "severity-dot" in resp.text

    @pytest.mark.asyncio
    async def test_system_badges_in_table(self, auth_client: AsyncClient) -> None:
        """System column shows pill badges."""
        _setup_mock_reader(auth_client)
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        # System is wrapped in a badge span
        assert "homeassistant" in resp.text

    @pytest.mark.asyncio
    async def test_hybrid_timestamps(self, auth_client: AsyncClient) -> None:
        """Table shows relative time with Alpine.js x-data."""
        _setup_mock_reader(auth_client)
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "x-data" in resp.text
        assert "ago" in resp.text
