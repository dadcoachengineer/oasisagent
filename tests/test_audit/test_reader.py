"""Tests for the InfluxDB audit reader."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.audit.reader import (
    AuditReader,
    AuditReaderNotStartedError,
    EventPage,
    FilterOptions,
    _validate_duration,
    _validate_tag,
)
from oasisagent.config import AuditConfig, InfluxDbConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(enabled: bool = True) -> AuditConfig:
    return AuditConfig(
        influxdb=InfluxDbConfig(
            enabled=enabled,
            url="http://localhost:8086",
            token="test-token",
            org="testorg",
            bucket="testbucket",
        ),
    )


def _make_flux_record(values: dict, time_val: datetime | None = None) -> MagicMock:
    """Create a mock FluxRecord with given values and time."""
    rec = MagicMock()
    rec.values = dict(values)
    rec.get_time.return_value = time_val or datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
    rec.get_value.return_value = values.get("_value", "")
    return rec


def _make_table(records: list) -> MagicMock:
    """Create a mock FluxTable with given records."""
    table = MagicMock()
    table.records = records
    return table


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_tags(self) -> None:
        assert _validate_tag("mqtt")
        assert _validate_tag("homeassistant")
        assert _validate_tag("sensor.temperature")
        assert _validate_tag("integration:mqtt")
        assert _validate_tag("state_unavailable")
        assert _validate_tag("ha-websocket")
        assert _validate_tag("path/to/thing")

    def test_invalid_tags(self) -> None:
        assert not _validate_tag("")
        assert not _validate_tag('"; DROP TABLE')
        assert not _validate_tag("value with spaces")
        assert not _validate_tag("injection\n|> drop()")

    def test_valid_durations(self) -> None:
        assert _validate_duration("1h")
        assert _validate_duration("24h")
        assert _validate_duration("7d")
        assert _validate_duration("30d")
        assert _validate_duration("60s")
        assert _validate_duration("10m")

    def test_invalid_durations(self) -> None:
        assert not _validate_duration("")
        assert not _validate_duration("0h")
        assert not _validate_duration("abc")
        assert not _validate_duration("-1h")
        assert not _validate_duration("1h; malicious")


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_disabled(self) -> None:
        reader = AuditReader(_make_config(enabled=False))
        await reader.start()
        assert reader._client is None
        assert reader._query_api is None

    @pytest.mark.asyncio
    async def test_start_enabled(self) -> None:
        reader = AuditReader(_make_config())
        with patch(
            "influxdb_client.client.influxdb_client_async.InfluxDBClientAsync"
        ) as mock_cls:
            mock_client = MagicMock()
            mock_client.query_api.return_value = MagicMock()
            mock_client.close = AsyncMock()
            mock_cls.return_value = mock_client

            await reader.start()
            assert reader._client is not None
            assert reader._query_api is not None

            await reader.stop()
            mock_client.close.assert_awaited_once()
            assert reader._client is None

    @pytest.mark.asyncio
    async def test_not_started_raises(self) -> None:
        reader = AuditReader(_make_config())
        with pytest.raises(AuditReaderNotStartedError):
            await reader.list_events()

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self) -> None:
        reader = AuditReader(_make_config())
        await reader.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------


class TestListEvents:
    @pytest.mark.asyncio
    async def test_list_events_empty(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        reader._query_api.query = AsyncMock(return_value=[])

        page = await reader.list_events()
        assert isinstance(page, EventPage)
        assert page.rows == []
        assert page.has_next is False
        assert page.has_prev is False

    @pytest.mark.asyncio
    async def test_list_events_with_data(self) -> None:
        ts = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
        event_rec = _make_flux_record(
            {
                "event_id": "evt-001",
                "source": "mqtt",
                "system": "homeassistant",
                "event_type": "state_unavailable",
                "entity_id": "light.kitchen",
                "severity": "warning",
            },
            time_val=ts,
        )
        event_table = _make_table([event_rec])

        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        # First call: events, second call: decisions batch
        reader._query_api.query = AsyncMock(side_effect=[[event_table], []])

        page = await reader.list_events(limit=25)
        assert len(page.rows) == 1
        row = page.rows[0]
        assert row.event_id == "evt-001"
        assert row.source == "mqtt"
        assert row.severity == "warning"
        assert page.has_next is False

    @pytest.mark.asyncio
    async def test_list_events_has_next(self) -> None:
        """When limit+1 records returned, has_next is True."""
        ts = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
        records = [
            _make_flux_record(
                {
                    "event_id": f"evt-{i:03d}",
                    "source": "mqtt",
                    "system": "ha",
                    "event_type": "state_changed",
                    "entity_id": f"sensor.temp_{i}",
                    "severity": "info",
                },
                time_val=ts,
            )
            for i in range(3)  # limit=2, so 3 records means has_next
        ]
        event_table = _make_table(records)

        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        reader._query_api.query = AsyncMock(side_effect=[[event_table], []])

        page = await reader.list_events(limit=2)
        assert len(page.rows) == 2
        assert page.has_next is True

    @pytest.mark.asyncio
    async def test_list_events_with_filters(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        reader._query_api.query = AsyncMock(return_value=[])

        page = await reader.list_events(
            severity="critical",
            source="mqtt",
            duration="7d",
        )
        assert isinstance(page, EventPage)
        # Verify the query was called (we check it doesn't crash)
        assert reader._query_api.query.call_count == 1  # No decisions if no events

    @pytest.mark.asyncio
    async def test_invalid_duration_defaults(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        reader._query_api.query = AsyncMock(return_value=[])

        page = await reader.list_events(duration="invalid")
        assert isinstance(page, EventPage)
        # Should have fallen back to 24h, no crash
        query_call = reader._query_api.query.call_args[0][0]
        assert "-24h" in query_call

    @pytest.mark.asyncio
    async def test_invalid_filter_tag_ignored(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        reader._query_api.query = AsyncMock(return_value=[])

        page = await reader.list_events(severity='"; DROP TABLE')
        assert isinstance(page, EventPage)
        # Invalid tag should be silently ignored (not injected)
        query_call = reader._query_api.query.call_args[0][0]
        assert "DROP TABLE" not in query_call


class TestGetEventTimeline:
    @pytest.mark.asyncio
    async def test_event_not_found(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        reader._query_api.query = AsyncMock(return_value=[])

        result = await reader.get_event_timeline("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_event_id(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()

        result = await reader.get_event_timeline('"; DROP TABLE')
        assert result is None
        reader._query_api.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_timeline(self) -> None:
        ts = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)

        # Event record
        event_rec = _make_flux_record(
            {
                "event_id": "evt-001",
                "source": "mqtt",
                "system": "homeassistant",
                "event_type": "state_unavailable",
                "entity_id": "light.kitchen",
                "severity": "warning",
                "payload": '{"old_state": "on"}',
                "correlation_id": "corr-123",
            },
            time_val=ts,
        )
        event_table = _make_table([event_rec])

        # Decision record
        decision_rec = _make_flux_record(
            {
                "tier": "t0",
                "disposition": "remediated",
                "risk_tier": "auto",
                "diagnosis": "Known fix matched",
                "matched_fix_id": "fix-001",
            },
            time_val=ts,
        )
        decision_table = _make_table([decision_rec])

        # Action record
        action_rec = _make_flux_record(
            {
                "handler": "homeassistant",
                "operation": "restart_integration",
                "action_status": "success",
                "duration_ms": 1234.5,
                "error_message": "",
            },
            time_val=ts,
        )
        action_table = _make_table([action_rec])

        # Verify record
        verify_rec = _make_flux_record(
            {
                "handler": "homeassistant",
                "operation": "restart_integration",
                "verified": "true",
                "message": "Integration back online",
            },
            time_val=ts,
        )
        verify_table = _make_table([verify_rec])

        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        # Calls: event query, then 3 parallel (decision, actions, verifications)
        reader._query_api.query = AsyncMock(
            side_effect=[
                [event_table],      # event
                [decision_table],   # decision
                [action_table],     # actions
                [verify_table],     # verifications
            ]
        )

        timeline = await reader.get_event_timeline("evt-001")
        assert timeline is not None
        assert timeline.event.event_id == "evt-001"
        assert timeline.payload == {"old_state": "on"}
        assert timeline.correlation_id == "corr-123"
        assert timeline.decision is not None
        assert timeline.decision.disposition == "remediated"
        assert len(timeline.actions) == 1
        assert timeline.actions[0].status == "success"
        assert len(timeline.verifications) == 1
        assert timeline.verifications[0].verified is True


class TestFilterOptions:
    @pytest.mark.asyncio
    async def test_get_filter_options(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()

        # Each tag query returns a table with one value record
        def _tag_table(value: str) -> list:
            rec = MagicMock()
            rec.get_value.return_value = value
            table = MagicMock()
            table.records = [rec]
            return [table]

        reader._query_api.query = AsyncMock(
            side_effect=[
                _tag_table("warning"),      # severities
                _tag_table("mqtt"),          # sources
                _tag_table("homeassistant"), # systems
                _tag_table("state_changed"), # event_types
                _tag_table("remediated"),    # dispositions
            ]
        )

        options = await reader.get_filter_options()
        assert isinstance(options, FilterOptions)
        assert "warning" in options.severities
        assert "mqtt" in options.sources

    @pytest.mark.asyncio
    async def test_filter_options_cached(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()

        def _empty_table() -> list:
            return []

        reader._query_api.query = AsyncMock(return_value=[])

        await reader.get_filter_options()
        call_count_first = reader._query_api.query.call_count

        # Second call should use cache
        await reader.get_filter_options()
        assert reader._query_api.query.call_count == call_count_first

    @pytest.mark.asyncio
    async def test_filter_options_query_failure(self) -> None:
        reader = AuditReader(_make_config())
        reader._query_api = AsyncMock()
        reader._query_api.query = AsyncMock(side_effect=Exception("connection refused"))

        options = await reader.get_filter_options()
        assert options.severities == []
        assert options.sources == []
