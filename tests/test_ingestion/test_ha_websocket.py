"""Tests for the Home Assistant WebSocket ingestion adapter."""

from __future__ import annotations

from typing import Any

import pytest

from oasisagent.config import (
    AutomationFailuresSubscription,
    HaWebSocketConfig,
    HaWebSocketSubscriptions,
    ServiceCallErrorsSubscription,
    StateChangesSubscription,
)
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.ha_websocket import HaWebSocketAdapter
from oasisagent.models import Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> HaWebSocketConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "url": "ws://localhost:8123/api/websocket",
        "token": "test-token",
    }
    defaults.update(overrides)
    return HaWebSocketConfig(**defaults)


def _make_state_changed_event(
    entity_id: str = "light.living_room",
    old_state: str = "on",
    new_state: str = "unavailable",
) -> dict[str, Any]:
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "entity_id": entity_id,
                "old_state": {"state": old_state, "entity_id": entity_id},
                "new_state": {
                    "state": new_state,
                    "entity_id": entity_id,
                    "attributes": {"friendly_name": "Living Room Light"},
                },
            },
        },
    }


def _make_automation_triggered_event(
    entity_id: str = "automation.kitchen_lights",
    has_error: bool = True,
) -> dict[str, Any]:
    data: dict[str, Any] = {"entity_id": entity_id}
    if has_error:
        data["error"] = "Service call failed: kelvin deprecated"
    return {
        "type": "event",
        "event": {
            "event_type": "automation_triggered",
            "data": data,
            "context": {"id": "ctx-123"},
        },
    }


def _make_service_call_event(
    domain: str = "light",
    service: str = "turn_on",
    has_error: bool = True,
) -> dict[str, Any]:
    data: dict[str, Any] = {"domain": domain, "service": service}
    if has_error:
        data["error"] = "Service not found"
    return {
        "type": "event",
        "event": {
            "event_type": "call_service",
            "data": data,
        },
    }


# ---------------------------------------------------------------------------
# Event handling — state_changed
# ---------------------------------------------------------------------------


class TestStateChanged:
    """Tests for state_changed event processing."""

    def test_unavailable_state_emits_event(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event(_make_state_changed_event(
            entity_id="sensor.temp",
            new_state="unavailable",
        ))

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.source == "ha_websocket"
        assert event.system == "homeassistant"
        assert event.event_type == "state_unavailable"
        assert event.entity_id == "sensor.temp"
        assert event.severity == Severity.WARNING
        assert event.payload["old_state"] == "on"
        assert event.payload["new_state"] == "unavailable"
        assert event.metadata.dedup_key == "ha_ws:sensor.temp:state_unavailable"

    def test_unknown_state_emits_event(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event(_make_state_changed_event(
            entity_id="sensor.temp",
            new_state="unknown",
        ))

        assert queue.size == 1

    def test_normal_state_change_ignored(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event(_make_state_changed_event(
            new_state="on",
        ))

        assert queue.size == 0

    def test_ignored_entity_filtered(self) -> None:
        config = _make_config(
            subscriptions=HaWebSocketSubscriptions(
                state_changes=StateChangesSubscription(
                    enabled=True,
                    ignore_entities=["sensor.flaky"],
                ),
            )
        )
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(config, queue)

        adapter._handle_event(_make_state_changed_event(
            entity_id="sensor.flaky",
            new_state="unavailable",
        ))

        assert queue.size == 0

    def test_disabled_subscription_ignored(self) -> None:
        config = _make_config(
            subscriptions=HaWebSocketSubscriptions(
                state_changes=StateChangesSubscription(enabled=False),
            )
        )
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(config, queue)

        adapter._handle_event(_make_state_changed_event(
            new_state="unavailable",
        ))

        assert queue.size == 0

    def test_min_duration_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """min_duration > 0 is acknowledged but not enforced yet."""
        import logging

        config = _make_config(
            subscriptions=HaWebSocketSubscriptions(
                state_changes=StateChangesSubscription(
                    enabled=True,
                    min_duration=120,
                ),
            )
        )
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(config, queue)

        with caplog.at_level(logging.DEBUG):
            adapter._handle_event(_make_state_changed_event(
                new_state="unavailable",
            ))

        # Event should still be emitted (no debounce yet)
        assert queue.size == 1
        assert any("min_duration" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Event handling — automation_triggered
# ---------------------------------------------------------------------------


class TestAutomationTriggered:
    """Tests for automation_triggered event processing."""

    def test_automation_failure_emits_event(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event(_make_automation_triggered_event(
            entity_id="automation.kitchen",
            has_error=True,
        ))

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "automation_error"
        assert event.entity_id == "automation.kitchen"
        assert event.severity == Severity.ERROR

    def test_successful_automation_ignored(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event(_make_automation_triggered_event(
            has_error=False,
        ))

        assert queue.size == 0

    def test_disabled_automation_subscription(self) -> None:
        config = _make_config(
            subscriptions=HaWebSocketSubscriptions(
                automation_failures=AutomationFailuresSubscription(enabled=False),
            )
        )
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(config, queue)

        adapter._handle_event(_make_automation_triggered_event(has_error=True))

        assert queue.size == 0


# ---------------------------------------------------------------------------
# Event handling — call_service
# ---------------------------------------------------------------------------


class TestServiceCall:
    """Tests for call_service event processing."""

    def test_service_error_emits_event(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event(_make_service_call_event(
            domain="light",
            service="turn_on",
            has_error=True,
        ))

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.event_type == "service_call_failure"
        assert event.entity_id == "light.turn_on"

    def test_successful_service_call_ignored(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event(_make_service_call_event(has_error=False))

        assert queue.size == 0

    def test_disabled_service_call_subscription(self) -> None:
        config = _make_config(
            subscriptions=HaWebSocketSubscriptions(
                service_call_errors=ServiceCallErrorsSubscription(enabled=False),
            )
        )
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(config, queue)

        adapter._handle_event(_make_service_call_event(has_error=True))

        assert queue.size == 0


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------


class TestEventRouting:
    """Tests for event type routing."""

    def test_non_event_type_ignored(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event({"type": "result", "success": True})

        assert queue.size == 0

    def test_unknown_event_type_ignored(self) -> None:
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        adapter._handle_event({
            "type": "event",
            "event": {"event_type": "timer_finished", "data": {}},
        })

        assert queue.size == 0


# ---------------------------------------------------------------------------
# Adapter lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    """Tests for adapter properties and lifecycle."""

    def test_name(self) -> None:
        adapter = HaWebSocketAdapter(_make_config(), EventQueue(max_size=10))
        assert adapter.name == "ha_websocket"

    async def test_healthy_before_connect(self) -> None:
        adapter = HaWebSocketAdapter(_make_config(), EventQueue(max_size=10))
        assert await adapter.healthy() is False

    async def test_stop_sets_stopping(self) -> None:
        adapter = HaWebSocketAdapter(_make_config(), EventQueue(max_size=10))
        await adapter.stop()
        assert adapter._stopping is True
