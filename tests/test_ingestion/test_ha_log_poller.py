"""Tests for the Home Assistant log poller ingestion adapter."""

from __future__ import annotations

import logging
import time
from typing import Any

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


def _make_entry(
    name: str = "",
    message: str | list[str] = "",
    level: str = "ERROR",
    timestamp: float = 1741624926.123,
    count: int = 1,
    first_occurred: float = 1741624926.123,
    source: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "message": message,
        "level": level,
        "timestamp": timestamp,
        "count": count,
        "first_occurred": first_occurred,
        "source": source or [],
    }


# ---------------------------------------------------------------------------
# Log processing (structured entries via system_log/list)
# ---------------------------------------------------------------------------


class TestLogProcessing:
    """Tests for structured log entry matching and event generation."""

    def test_matching_entry_emits_event(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([
            _make_entry(
                name="homeassistant.components.zwave_js",
                message="Error setting up integration 'zwave_js'",
            )
        ])

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.source == "ha_log_poller"
        assert event.system == "homeassistant"
        assert event.event_type == "integration_failure"
        assert event.entity_id == "zwave_js"
        assert event.severity == Severity.ERROR
        assert event.payload["component"] == "homeassistant.components.zwave_js"
        assert event.payload["match_groups"] == ["zwave_js"]
        assert event.metadata.dedup_key == "ha_log:zwave_js:integration_failure"

    def test_multiple_patterns_matched(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([
            _make_entry(
                name="homeassistant.components.zwave_js",
                message="Error setting up integration 'zwave_js'",
            ),
            _make_entry(
                name="homeassistant.components.sensor",
                message="sensor.outdoor_temp is unavailable",
                level="WARNING",
            ),
        ])

        assert queue.size == 2
        events = queue.drain()
        assert events[0].event_type == "integration_failure"
        assert events[0].entity_id == "zwave_js"
        assert events[1].event_type == "state_unavailable"
        # (.+) captures from the combined "name: message" match text
        assert "sensor.outdoor_temp" in events[1].entity_id

    def test_non_matching_entry_ignored(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([
            _make_entry(
                name="homeassistant.core",
                message="Home Assistant started successfully",
                level="INFO",
            )
        ])

        assert queue.size == 0

    def test_empty_entries_produces_no_events(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        adapter._process_entries([])

        assert queue.size == 0

    def test_first_matching_pattern_wins(self) -> None:
        """If an entry matches multiple patterns, only the first fires."""
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
            _make_entry(
                name="homeassistant.components.sensor",
                message="sensor.temp is unavailable",
            )
        ])

        assert queue.size == 1
        assert queue.drain()[0].event_type == "first_match"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Tests for adapter-level dedup within poll window."""

    def test_duplicate_within_window_dropped(self) -> None:
        queue = EventQueue(max_size=10, dedup_window_seconds=0)  # Disable queue dedup
        adapter = HaLogPollerAdapter(_make_config(dedup_window=300), queue)

        entry = _make_entry(
            name="homeassistant.components.zwave_js",
            message="Error setting up integration 'zwave_js'",
        )
        adapter._process_entries([entry])
        adapter._process_entries([entry])

        assert queue.size == 1

    def test_duplicate_after_window_allowed(self) -> None:
        queue = EventQueue(max_size=10, dedup_window_seconds=0)
        adapter = HaLogPollerAdapter(_make_config(dedup_window=1), queue)

        entry = _make_entry(
            name="homeassistant.components.zwave_js",
            message="Error setting up integration 'zwave_js'",
        )
        adapter._process_entries([entry])

        # Age the seen entry past the window
        for key in adapter._seen:
            adapter._seen[key] = time.monotonic() - 2

        adapter._process_entries([entry])

        assert queue.size == 2

    def test_different_entries_not_deduped(self) -> None:
        queue = EventQueue(max_size=10, dedup_window_seconds=0)
        adapter = HaLogPollerAdapter(_make_config(dedup_window=300), queue)

        adapter._process_entries([
            _make_entry(
                name="homeassistant.components.zwave_js",
                message="Error setting up integration 'zwave_js'",
            )
        ])
        adapter._process_entries([
            _make_entry(
                name="homeassistant.components.mqtt",
                message="Error setting up integration 'mqtt'",
            )
        ])

        assert queue.size == 2

    def test_prune_cleans_expired_entries(self) -> None:
        adapter = HaLogPollerAdapter(
            _make_config(dedup_window=1),
            EventQueue(max_size=10),
        )

        adapter._mark_seen("old-fingerprint")
        adapter._seen["old-fingerprint"] = time.monotonic() - 2  # Expired

        adapter._prune_seen()

        assert "old-fingerprint" not in adapter._seen


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error handling during polling."""

    def test_invalid_regex_logged_at_init(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR):
            adapter = HaLogPollerAdapter(
                _make_config(
                    patterns=[
                        LogPattern(
                            regex=r"[invalid",
                            event_type="bad",
                            severity="error",
                        ),
                    ]
                ),
                EventQueue(max_size=10),
            )

        assert any("invalid regex" in r.message for r in caplog.records)
        assert len(adapter._compiled_patterns) == 0


# ---------------------------------------------------------------------------
# Adapter lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    """Tests for adapter properties and lifecycle."""

    def test_name(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))
        assert adapter.name == "ha_log_poller"

    async def test_healthy_before_poll(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))
        assert await adapter.healthy() is False

    async def test_stop_sets_stopping(self) -> None:
        adapter = HaLogPollerAdapter(_make_config(), EventQueue(max_size=10))
        await adapter.stop()
        assert adapter._stopping is True


# ---------------------------------------------------------------------------
# Cross-adapter integration
# ---------------------------------------------------------------------------


class TestCrossAdapterIntegration:
    """Verify multiple adapters can share the same queue."""

    def test_two_adapters_same_queue(self) -> None:
        queue = EventQueue(max_size=10, dedup_window_seconds=0)

        log_config = _make_config()

        from oasisagent.config import MqttIngestionConfig, MqttTopicMapping
        from oasisagent.ingestion.mqtt import MqttAdapter

        mqtt_adapter = MqttAdapter(
            MqttIngestionConfig(
                broker="mqtt://localhost:1883",
                topics=[
                    MqttTopicMapping(
                        pattern="test/#",
                        system="test",
                        event_type="test",
                        severity="info",
                    ),
                ],
            ),
            queue,
        )
        log_adapter = HaLogPollerAdapter(log_config, queue)

        # Both push to the same queue
        log_adapter._process_entries([
            _make_entry(
                name="homeassistant.components.zwave_js",
                message="Error setting up integration 'zwave_js'",
            )
        ])

        import json
        from unittest.mock import MagicMock

        msg = MagicMock()
        msg.topic = MagicMock()
        msg.topic.__str__ = MagicMock(return_value="test/event")
        msg.payload = json.dumps({"entity_id": "test.entity"}).encode()
        mqtt_adapter._handle_message(msg)

        assert queue.size == 2
        events = queue.drain()
        sources = {e.source for e in events}
        assert sources == {"ha_log_poller", "mqtt"}
