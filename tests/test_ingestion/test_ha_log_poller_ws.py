"""Tests for WebSocket-based HA system log polling."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from oasisagent.config import HaLogPollerConfig, LogPattern
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.ha_log_poller import HaLogPollerAdapter
from oasisagent.models import Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> HaLogPollerConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "url": "http://localhost:8123",
        "token": "test-token",
        "poll_interval": 30,
        "dedup_window": 300,
        "patterns": [
            LogPattern(
                regex=r"Error setting up integration '(.+)'",
                event_type="integration_failure",
                severity="error",
            ),
            LogPattern(
                regex=r"(.+) is unavailable",
                event_type="state_unavailable",
                severity="warning",
            ),
        ],
    }
    defaults.update(overrides)
    return HaLogPollerConfig(**defaults)


async def _async_iter(items: list[Any]):  # type: ignore[type-arg]
    for item in items:
        yield item


def _make_log_entry(
    name: str = "homeassistant.components.zwave_js",
    message: str | list[str] = "Error setting up integration 'zwave_js'",
    level: str = "ERROR",
    timestamp: float = 1741624926.123,
    count: int = 5,
    first_occurred: float = 1741192255.0,
    source: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "message": message,
        "level": level,
        "timestamp": timestamp,
        "count": count,
        "first_occurred": first_occurred,
        "source": source or ["components/zwave_js/__init__.py", 100],
    }


# ---------------------------------------------------------------------------
# WebSocket URL derivation
# ---------------------------------------------------------------------------


class TestWsUrlDerivation:
    """Tests for _ws_url() URL conversion."""

    def test_http_to_ws(self) -> None:
        adapter = HaLogPollerAdapter(
            _make_config(url="http://localhost:8123"), EventQueue(max_size=10)
        )
        assert adapter._ws_url() == "ws://localhost:8123/api/websocket"

    def test_https_to_wss(self) -> None:
        adapter = HaLogPollerAdapter(
            _make_config(url="https://oasis.shearer.live"), EventQueue(max_size=10)
        )
        assert adapter._ws_url() == "wss://oasis.shearer.live/api/websocket"

    def test_trailing_slash_stripped(self) -> None:
        adapter = HaLogPollerAdapter(
            _make_config(url="http://localhost:8123/"), EventQueue(max_size=10)
        )
        assert adapter._ws_url() == "ws://localhost:8123/api/websocket"

    def test_ws_url_passthrough(self) -> None:
        adapter = HaLogPollerAdapter(
            _make_config(url="ws://localhost:8123/api/websocket"),
            EventQueue(max_size=10),
        )
        assert adapter._ws_url() == "ws://localhost:8123/api/websocket"


# ---------------------------------------------------------------------------
# Structured entry processing
# ---------------------------------------------------------------------------


class TestEntryProcessing:
    """Tests for _process_entries with structured system_log/list data."""

    def test_matching_entry_emits_event(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([_make_log_entry()])

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.source == "ha_log_poller"
        assert event.system == "homeassistant"
        assert event.event_type == "integration_failure"
        assert event.entity_id == "zwave_js"
        assert event.severity == Severity.ERROR

    def test_payload_contains_structured_fields(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([_make_log_entry(count=42)])

        event = queue.drain()[0]
        assert event.payload["component"] == "homeassistant.components.zwave_js"
        assert event.payload["count"] == 42
        assert event.payload["first_occurred"] == 1741192255.0
        assert event.payload["source"] == ["components/zwave_js/__init__.py", 100]
        assert event.payload["match_groups"] == ["zwave_js"]

    def test_severity_from_ha_level(self) -> None:
        """HA log level overrides pattern severity."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([_make_log_entry(level="WARNING")])

        event = queue.drain()[0]
        assert event.severity == Severity.WARNING

    def test_critical_severity(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([_make_log_entry(level="CRITICAL")])

        event = queue.drain()[0]
        assert event.severity == Severity.CRITICAL

    def test_message_as_list(self) -> None:
        """HA sometimes returns message as a list of strings."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        entry = _make_log_entry(
            message=["Error setting up integration", "'zwave_js'"]
        )
        adapter._process_entries([entry])

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.entity_id == "zwave_js"

    def test_non_matching_entry_ignored(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([
            _make_log_entry(
                name="homeassistant.core",
                message="Home Assistant started",
                level="INFO",
            )
        ])

        assert queue.size == 0

    def test_multiple_entries(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([
            _make_log_entry(),
            _make_log_entry(
                name="homeassistant.components.sensor",
                message="sensor.outdoor_temp is unavailable",
                level="WARNING",
            ),
        ])

        assert queue.size == 2
        events = queue.drain()
        assert events[0].event_type == "integration_failure"
        assert events[1].event_type == "state_unavailable"

    def test_first_pattern_wins(self) -> None:
        config = _make_config(
            patterns=[
                LogPattern(
                    regex=r"(.+) is unavailable",
                    event_type="first_match",
                    severity="warning",
                ),
                LogPattern(
                    regex=r"sensor.temp is (.+)",
                    event_type="second_match",
                    severity="error",
                ),
            ]
        )
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(config, queue)

        adapter._process_entries([
            _make_log_entry(
                name="homeassistant.components.sensor",
                message="sensor.temp is unavailable",
            )
        ])

        assert queue.size == 1
        assert queue.drain()[0].event_type == "first_match"

    def test_match_against_component_and_message(self) -> None:
        """Regex can match against 'component: message' combined text."""
        config = _make_config(
            patterns=[
                LogPattern(
                    regex=r"zwave_js.*Error setting up",
                    event_type="zwave_setup_error",
                    severity="error",
                ),
            ]
        )
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(config, queue)

        adapter._process_entries([_make_log_entry()])

        assert queue.size == 1
        assert queue.drain()[0].event_type == "zwave_setup_error"

    def test_timestamp_from_entry(self) -> None:
        """Event timestamp comes from HA entry, not system clock."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([_make_log_entry(timestamp=1700000000.0)])

        event = queue.drain()[0]
        assert event.timestamp.year == 2023  # Nov 2023

    def test_empty_entries_no_events(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([])

        assert queue.size == 0


# ---------------------------------------------------------------------------
# Deduplication with structured entries
# ---------------------------------------------------------------------------


class TestStructuredDedup:
    """Tests for deduplication with structured log entries."""

    def test_duplicate_entry_dropped(self) -> None:
        queue = EventQueue(max_size=10, dedup_window_seconds=0)
        adapter = HaLogPollerAdapter(_make_config(dedup_window=300), queue)

        entry = _make_log_entry()
        adapter._process_entries([entry])
        adapter._process_entries([entry])

        assert queue.size == 1

    def test_different_entries_not_deduped(self) -> None:
        queue = EventQueue(max_size=10, dedup_window_seconds=0)
        adapter = HaLogPollerAdapter(_make_config(dedup_window=300), queue)

        adapter._process_entries([
            _make_log_entry(message="Error setting up integration 'zwave_js'"),
        ])
        adapter._process_entries([
            _make_log_entry(message="Error setting up integration 'mqtt'"),
        ])

        assert queue.size == 2

    def test_duplicate_after_window_allowed(self) -> None:
        queue = EventQueue(max_size=10, dedup_window_seconds=0)
        adapter = HaLogPollerAdapter(_make_config(dedup_window=1), queue)

        entry = _make_log_entry()
        adapter._process_entries([entry])

        # Age the seen entry past the window
        for key in adapter._seen:
            adapter._seen[key] = time.monotonic() - 2

        adapter._process_entries([entry])

        assert queue.size == 2


# ---------------------------------------------------------------------------
# WebSocket auth
# ---------------------------------------------------------------------------


class TestWsAuth:
    """Tests for WebSocket authentication."""

    async def test_auth_success(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))

        ws = AsyncMock()
        ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_ok"},
            ]
        )

        await adapter._authenticate(ws)

        ws.send_json.assert_called_once_with({
            "type": "auth",
            "access_token": "test-token",
        })

    async def test_auth_invalid_raises(self) -> None:
        from oasisagent.ingestion.ha_log_poller import _AuthError

        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))

        ws = AsyncMock()
        ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_invalid", "message": "bad token"},
            ]
        )

        with pytest.raises(_AuthError):
            await adapter._authenticate(ws)

    async def test_unexpected_first_message_raises(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))

        ws = AsyncMock()
        ws.receive_json = AsyncMock(return_value={"type": "something_else"})

        with pytest.raises(ConnectionError):
            await adapter._authenticate(ws)


# ---------------------------------------------------------------------------
# fetch_system_log
# ---------------------------------------------------------------------------


class TestFetchSystemLog:
    """Tests for _fetch_system_log WebSocket command."""

    async def test_returns_entries(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))
        adapter._msg_id = 0

        entries = [_make_log_entry(), _make_log_entry(message="other error")]

        ws_msg = MagicMock()
        ws_msg.type = aiohttp.WSMsgType.TEXT
        ws_msg.data = json.dumps({
            "id": 1,
            "type": "result",
            "success": True,
            "result": entries,
        })

        ws = MagicMock()
        ws.send_json = AsyncMock()
        ws.__aiter__ = lambda self: _async_iter([ws_msg])

        result = await adapter._fetch_system_log(ws)

        assert len(result) == 2
        ws.send_json.assert_called_once_with({
            "id": 1,
            "type": "system_log/list",
        })

    async def test_returns_empty_on_failure(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))
        adapter._msg_id = 0

        ws_msg = MagicMock()
        ws_msg.type = aiohttp.WSMsgType.TEXT
        ws_msg.data = json.dumps({
            "id": 1,
            "type": "result",
            "success": False,
            "error": {"message": "Unknown command"},
        })

        ws = MagicMock()
        ws.send_json = AsyncMock()
        ws.__aiter__ = lambda self: _async_iter([ws_msg])

        result = await adapter._fetch_system_log(ws)

        assert result == []

    async def test_returns_empty_on_ws_close(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))
        adapter._msg_id = 0

        ws_msg = MagicMock()
        ws_msg.type = aiohttp.WSMsgType.CLOSED

        ws = MagicMock()
        ws.send_json = AsyncMock()
        ws.__aiter__ = lambda self: _async_iter([ws_msg])

        result = await adapter._fetch_system_log(ws)

        assert result == []


# ---------------------------------------------------------------------------
# Real HA log pattern matching
# ---------------------------------------------------------------------------

# All seven default patterns from config.default.yaml
_DEFAULT_PATTERNS = [
    LogPattern(
        regex=r"Error setting up integration '(.+)'",
        event_type="integration_failure",
        severity="error",
    ),
    LogPattern(
        regex=r"Error while executing automation automation\.([^:]+)",
        event_type="automation_error",
        severity="error",
    ),
    LogPattern(
        regex=r"Error executing script.+for (\w+) at pos",
        event_type="automation_error",
        severity="error",
    ),
    LogPattern(
        regex=r"Template variable error: (.+)",
        event_type="template_error",
        severity="error",
    ),
    LogPattern(
        regex=r"Got `(.+)` argument.+which is deprecated",
        event_type="deprecation_warning",
        severity="warning",
    ),
    LogPattern(
        regex=r"Invalid config for \[([^\]]+)\]",
        event_type="config_error",
        severity="error",
    ),
    LogPattern(
        regex=r"(.+) is unavailable",
        event_type="state_unavailable",
        severity="warning",
    ),
]


class TestDefaultPatterns:
    """Tests that default patterns match real HA system log entries."""

    def _make_adapter(self) -> tuple[HaLogPollerAdapter, EventQueue]:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(
            _make_config(patterns=_DEFAULT_PATTERNS), queue
        )
        return adapter, queue

    def test_automation_execution_error(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.components.automation.living_room_lights",
            message="Error while executing automation automation.living_room_lights: "
                    "service not found",
            level="ERROR",
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "automation_error"
        assert event.entity_id == "living_room_lights"

    def test_automation_error_with_colon_in_details(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.components.automation.night_mode",
            message="Error while executing automation automation.night_mode: "
                    "Error for call_service at pos 2: device unavailable",
            level="ERROR",
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "automation_error"
        assert event.entity_id == "night_mode"

    def test_script_execution_error_service_call(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.components.automation.dimmer",
            message="Living Room Dimmer: Error executing script. "
                    "Error for call_service at pos 2: Failed to send request",
            level="ERROR",
            source=["helpers/script.py", 1792],
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "automation_error"
        assert event.entity_id == "call_service"

    def test_template_variable_error(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.helpers.template",
            message="Template variable error: 'dict object' has no attribute 'payload_json'",
            level="ERROR",
            source=["helpers/template.py", 456],
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "template_error"

    def test_deprecated_kelvin_parameter(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.components.light",
            message="Got `kelvin` argument in `turn_on` service, which is deprecated "
                    "and will break in Home Assistant 2026.1, please use "
                    "`color_temp_kelvin` argument",
            level="WARNING",
            source=["components/light/__init__.py", 331],
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "deprecation_warning"
        assert event.entity_id == "kelvin"
        assert event.severity == Severity.WARNING

    def test_deprecated_color_temp_parameter(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.components.light",
            message="Got `color_temp` argument in `turn_on` service, which is deprecated "
                    "and will break in Home Assistant 2026.1, please use "
                    "`color_temp_kelvin` argument",
            level="WARNING",
            source=["components/light/__init__.py", 331],
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "deprecation_warning"
        assert event.entity_id == "color_temp"

    def test_invalid_config(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.config",
            message="Invalid config for [automation]: not a valid value for "
                    "dictionary value @ data['type']. Got None",
            level="ERROR",
            source=["config.py", 464],
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "config_error"
        assert event.entity_id == "automation"

    def test_integration_failure_still_works(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry()])
        assert queue.size == 1
        assert queue.drain()[0].event_type == "integration_failure"

    def test_entity_unavailable_still_works(self) -> None:
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.components.sensor",
            message="sensor.outdoor_temp is unavailable",
            level="WARNING",
        )])
        assert queue.size == 1
        assert queue.drain()[0].event_type == "state_unavailable"

    def test_message_as_list_matches_automation_error(self) -> None:
        """HA sometimes returns message as a list of strings."""
        adapter, queue = self._make_adapter()
        adapter._process_entries([_make_log_entry(
            name="homeassistant.components.automation.garage_door",
            message=["Error while executing automation", "automation.garage_door: timeout"],
            level="ERROR",
        )])
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "automation_error"
