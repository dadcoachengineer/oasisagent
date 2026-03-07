"""Tests for the MQTT ingestion adapter."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from oasisagent.config import MqttIngestionConfig, MqttTopicMapping
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.mqtt import MqttAdapter, _topic_matches
from oasisagent.models import Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MqttIngestionConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "broker": "mqtt://localhost:1883",
        "username": "user",
        "password": "pass",
        "client_id": "test-agent",
        "qos": 1,
        "topics": [
            MqttTopicMapping(
                pattern="homeassistant/error/#",
                system="homeassistant",
                event_type="automation_error",
                severity="error",
            ),
        ],
    }
    defaults.update(overrides)
    return MqttIngestionConfig(**defaults)


def _make_message(
    topic: str = "homeassistant/error/test",
    payload: dict[str, Any] | str | None = None,
) -> MagicMock:
    """Create a mock MQTT message."""
    if payload is None:
        payload = {"entity_id": "automation.test", "error": "something broke"}
    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__ = MagicMock(return_value=topic)
    if isinstance(payload, dict):
        msg.payload = json.dumps(payload).encode("utf-8")
    else:
        msg.payload = payload.encode("utf-8") if isinstance(payload, str) else payload
    return msg


# ---------------------------------------------------------------------------
# Topic matching
# ---------------------------------------------------------------------------


class TestTopicMatching:
    """Tests for MQTT topic pattern matching."""

    def test_exact_match(self) -> None:
        assert _topic_matches("a/b/c", "a/b/c") is True

    def test_exact_no_match(self) -> None:
        assert _topic_matches("a/b/c", "a/b/d") is False

    def test_hash_wildcard(self) -> None:
        assert _topic_matches("a/b/#", "a/b/c") is True
        assert _topic_matches("a/b/#", "a/b/c/d") is True
        assert _topic_matches("a/#", "a/b/c") is True

    def test_plus_wildcard(self) -> None:
        assert _topic_matches("a/+/c", "a/b/c") is True
        assert _topic_matches("a/+/c", "a/x/c") is True
        assert _topic_matches("a/+/c", "a/b/d") is False

    def test_length_mismatch(self) -> None:
        assert _topic_matches("a/b", "a/b/c") is False
        assert _topic_matches("a/b/c", "a/b") is False


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------


class TestMessageHandling:
    """Tests for MQTT message → Event transformation."""

    def test_valid_json_message(self) -> None:
        queue = EventQueue(max_size=10)
        config = _make_config()
        adapter = MqttAdapter(config, queue)

        msg = _make_message(
            topic="homeassistant/error/kitchen",
            payload={"entity_id": "automation.kitchen", "error": "kelvin deprecated"},
        )
        adapter._handle_message(msg)

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.source == "mqtt"
        assert event.system == "homeassistant"
        assert event.event_type == "automation_error"
        assert event.entity_id == "automation.kitchen"
        assert event.severity == Severity.ERROR
        assert event.payload["error"] == "kelvin deprecated"
        assert event.metadata.dedup_key == "mqtt:automation.kitchen:automation_error"

    def test_non_json_payload_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(_make_config(), queue)

        msg = _make_message(topic="homeassistant/error/test", payload="not json {{{")

        import logging

        with caplog.at_level(logging.WARNING):
            adapter._handle_message(msg)

        assert queue.size == 0
        assert any("non-JSON" in r.message for r in caplog.records)

    def test_non_dict_json_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(_make_config(), queue)

        msg = _make_message(topic="homeassistant/error/test")
        msg.payload = json.dumps([1, 2, 3]).encode("utf-8")

        import logging

        with caplog.at_level(logging.WARNING):
            adapter._handle_message(msg)

        assert queue.size == 0

    def test_entity_id_from_topic_fallback(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(_make_config(), queue)

        msg = _make_message(
            topic="homeassistant/error/test",
            payload={"error": "no entity_id field"},
        )
        adapter._handle_message(msg)

        event = queue.drain()[0]
        assert event.entity_id == "homeassistant/error/test"

    def test_multiple_topic_mappings(self) -> None:
        config = _make_config(
            topics=[
                MqttTopicMapping(
                    pattern="ha/error/#",
                    system="homeassistant",
                    event_type="automation_error",
                    severity="error",
                ),
                MqttTopicMapping(
                    pattern="oasis/alerts/#",
                    system="oasis",
                    event_type="alert",
                    severity="warning",
                ),
            ]
        )
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(config, queue)

        msg1 = _make_message(topic="ha/error/test", payload={"entity_id": "a"})
        msg2 = _make_message(topic="oasis/alerts/test", payload={"entity_id": "b"})

        adapter._handle_message(msg1)
        adapter._handle_message(msg2)

        events = queue.drain()
        assert len(events) == 2
        assert events[0].system == "homeassistant"
        assert events[0].severity == Severity.ERROR
        assert events[1].system == "oasis"
        assert events[1].severity == Severity.WARNING

    def test_auto_severity_from_payload(self) -> None:
        config = _make_config(
            topics=[
                MqttTopicMapping(
                    pattern="test/#",
                    system="test",
                    event_type="test_event",
                    severity="auto",
                ),
            ]
        )
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(config, queue)

        msg = _make_message(
            topic="test/foo",
            payload={"entity_id": "x", "severity": "critical"},
        )
        adapter._handle_message(msg)

        event = queue.drain()[0]
        assert event.severity == Severity.CRITICAL

    def test_auto_severity_defaults_to_warning(self) -> None:
        config = _make_config(
            topics=[
                MqttTopicMapping(
                    pattern="test/#",
                    system="test",
                    event_type="test_event",
                    severity="auto",
                ),
            ]
        )
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(config, queue)

        msg = _make_message(
            topic="test/foo",
            payload={"entity_id": "x"},
        )
        adapter._handle_message(msg)

        event = queue.drain()[0]
        assert event.severity == Severity.WARNING

    def test_unmatched_topic_uses_defaults(self) -> None:
        config = _make_config(topics=[])
        queue = EventQueue(max_size=10)
        adapter = MqttAdapter(config, queue)

        msg = _make_message(
            topic="unknown/topic",
            payload={"entity_id": "x"},
        )
        adapter._handle_message(msg)

        event = queue.drain()[0]
        assert event.system == "unknown"
        assert event.event_type == "unknown"
        assert event.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Adapter lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    """Tests for adapter properties and lifecycle."""

    def test_name(self) -> None:
        adapter = MqttAdapter(_make_config(), EventQueue(max_size=10))
        assert adapter.name == "mqtt"

    async def test_healthy_before_connect(self) -> None:
        adapter = MqttAdapter(_make_config(), EventQueue(max_size=10))
        assert await adapter.healthy() is False

    async def test_stop_sets_stopping(self) -> None:
        adapter = MqttAdapter(_make_config(), EventQueue(max_size=10))
        await adapter.stop()
        assert adapter._stopping is True
        assert await adapter.healthy() is False

    def test_broker_url_parsing(self) -> None:
        adapter = MqttAdapter(
            _make_config(broker="mqtt://192.168.1.100:1884"),
            EventQueue(max_size=10),
        )
        assert adapter._hostname == "192.168.1.100"
        assert adapter._port == 1884
