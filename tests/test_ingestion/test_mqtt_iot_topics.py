"""Tests for IoT service MQTT topic mappings.

Verifies that Zigbee2MQTT, Frigate, ESPresense, Valetudo, WLED,
and BirdNET messages are correctly transformed into Events by the
MQTT adapter with appropriate topic mappings configured.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from oasisagent.config import MqttIngestionConfig, MqttTopicMapping
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.mqtt import MqttAdapter
from oasisagent.models import Event, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# All IoT topic mappings matching config.example.yaml
_IOT_TOPICS = [
    # Zigbee2MQTT
    MqttTopicMapping(
        pattern="zigbee2mqtt/bridge/state",
        system="zigbee2mqtt",
        event_type="bridge_state",
        severity="auto",
    ),
    MqttTopicMapping(
        pattern="zigbee2mqtt/bridge/logging",
        system="zigbee2mqtt",
        event_type="bridge_log",
        severity="auto",
    ),
    MqttTopicMapping(
        pattern="zigbee2mqtt/+/availability",
        system="zigbee2mqtt",
        event_type="device_availability",
        severity="auto",
    ),
    # Frigate
    MqttTopicMapping(
        pattern="frigate/available",
        system="frigate",
        event_type="service_availability",
        severity="auto",
    ),
    MqttTopicMapping(
        pattern="frigate/events",
        system="frigate",
        event_type="detection_event",
        severity="info",
    ),
    MqttTopicMapping(
        pattern="frigate/stats",
        system="frigate",
        event_type="stats",
        severity="auto",
    ),
    # ESPresense
    MqttTopicMapping(
        pattern="espresense/rooms/+/status",
        system="espresense",
        event_type="room_status",
        severity="auto",
    ),
    MqttTopicMapping(
        pattern="espresense/rooms/+/telemetry",
        system="espresense",
        event_type="room_telemetry",
        severity="info",
    ),
    # Valetudo
    MqttTopicMapping(
        pattern="valetudo/+/StatusStateAttribute/status",
        system="valetudo",
        event_type="vacuum_status",
        severity="auto",
    ),
    MqttTopicMapping(
        pattern="valetudo/+/BatteryStateAttribute/level",
        system="valetudo",
        event_type="vacuum_battery",
        severity="info",
    ),
    MqttTopicMapping(
        pattern="valetudo/+/events",
        system="valetudo",
        event_type="vacuum_event",
        severity="auto",
    ),
    # WLED
    MqttTopicMapping(
        pattern="wled/+/status",
        system="wled",
        event_type="device_status",
        severity="auto",
    ),
    # BirdNET
    MqttTopicMapping(
        pattern="birdnet/sightings",
        system="birdnet",
        event_type="sighting",
        severity="info",
    ),
]


def _make_config() -> MqttIngestionConfig:
    return MqttIngestionConfig(
        enabled=True,
        broker="mqtt://localhost:1883",
        username="user",
        password="pass",
        client_id="test-agent",
        qos=1,
        topics=_IOT_TOPICS,
    )


def _make_message(topic: str, payload: dict[str, Any]) -> MagicMock:
    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__ = MagicMock(return_value=topic)
    msg.payload = json.dumps(payload).encode("utf-8")
    return msg


def _ingest(topic: str, payload: dict[str, Any]) -> Event:
    """Send a message through the adapter and return the resulting Event."""
    queue = EventQueue(max_size=10)
    adapter = MqttAdapter(_make_config(), queue)
    adapter._handle_message(_make_message(topic, payload))
    events = queue.drain()
    assert len(events) == 1, f"Expected 1 event, got {len(events)}"
    return events[0]


# ---------------------------------------------------------------------------
# Zigbee2MQTT
# ---------------------------------------------------------------------------


class TestZigbee2Mqtt:
    def test_bridge_state_online(self) -> None:
        event = _ingest("zigbee2mqtt/bridge/state", {
            "state": "online",
        })
        assert event.system == "zigbee2mqtt"
        assert event.event_type == "bridge_state"
        assert event.payload["state"] == "online"

    def test_bridge_state_offline(self) -> None:
        event = _ingest("zigbee2mqtt/bridge/state", {
            "state": "offline",
            "severity": "error",
        })
        assert event.system == "zigbee2mqtt"
        assert event.event_type == "bridge_state"
        assert event.severity == Severity.ERROR

    def test_bridge_logging(self) -> None:
        event = _ingest("zigbee2mqtt/bridge/logging", {
            "level": "error",
            "message": "Coordinator failed to start",
            "severity": "error",
        })
        assert event.system == "zigbee2mqtt"
        assert event.event_type == "bridge_log"
        assert event.severity == Severity.ERROR

    def test_device_availability_online(self) -> None:
        event = _ingest("zigbee2mqtt/kitchen_sensor/availability", {
            "state": "online",
            "entity_id": "kitchen_sensor",
        })
        assert event.system == "zigbee2mqtt"
        assert event.event_type == "device_availability"
        assert event.entity_id == "kitchen_sensor"

    def test_device_availability_offline(self) -> None:
        event = _ingest("zigbee2mqtt/motion_sensor_01/availability", {
            "state": "offline",
            "entity_id": "motion_sensor_01",
            "severity": "warning",
        })
        assert event.system == "zigbee2mqtt"
        assert event.event_type == "device_availability"
        assert event.entity_id == "motion_sensor_01"
        assert event.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Frigate
# ---------------------------------------------------------------------------


class TestFrigate:
    def test_service_available(self) -> None:
        event = _ingest("frigate/available", {
            "status": "online",
        })
        assert event.system == "frigate"
        assert event.event_type == "service_availability"

    def test_detection_event(self) -> None:
        event = _ingest("frigate/events", {
            "type": "new",
            "before": {},
            "after": {"label": "person", "camera": "front_door"},
            "entity_id": "front_door",
        })
        assert event.system == "frigate"
        assert event.event_type == "detection_event"
        assert event.severity == Severity.INFO
        assert event.entity_id == "front_door"

    def test_stats(self) -> None:
        event = _ingest("frigate/stats", {
            "cameras": {"front": {"camera_fps": 15, "process_fps": 10}},
            "detectors": {"coral": {"inference_speed": 8.5}},
            "severity": "info",
        })
        assert event.system == "frigate"
        assert event.event_type == "stats"

    def test_stats_with_zero_fps(self) -> None:
        event = _ingest("frigate/stats", {
            "cameras": {"front": {"camera_fps": 0, "process_fps": 0}},
            "severity": "warning",
        })
        assert event.system == "frigate"
        assert event.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# ESPresense
# ---------------------------------------------------------------------------


class TestEspresense:
    def test_room_status(self) -> None:
        event = _ingest("espresense/rooms/office/status", {
            "status": "online",
            "entity_id": "office",
        })
        assert event.system == "espresense"
        assert event.event_type == "room_status"
        assert event.entity_id == "office"

    def test_room_telemetry(self) -> None:
        event = _ingest("espresense/rooms/living_room/telemetry", {
            "ip": "192.168.1.50",
            "uptime": 86400,
            "free_heap": 120000,
            "entity_id": "living_room",
        })
        assert event.system == "espresense"
        assert event.event_type == "room_telemetry"
        assert event.severity == Severity.INFO
        assert event.entity_id == "living_room"


# ---------------------------------------------------------------------------
# Valetudo
# ---------------------------------------------------------------------------


class TestValetudo:
    def test_vacuum_status(self) -> None:
        event = _ingest("valetudo/roborock_s7/StatusStateAttribute/status", {
            "state": "cleaning",
            "entity_id": "roborock_s7",
        })
        assert event.system == "valetudo"
        assert event.event_type == "vacuum_status"
        assert event.entity_id == "roborock_s7"

    def test_vacuum_error_state(self) -> None:
        event = _ingest("valetudo/roborock_s7/StatusStateAttribute/status", {
            "state": "error",
            "detail": "stuck_at_obstacle",
            "entity_id": "roborock_s7",
            "severity": "error",
        })
        assert event.system == "valetudo"
        assert event.event_type == "vacuum_status"
        assert event.severity == Severity.ERROR

    def test_vacuum_battery(self) -> None:
        event = _ingest("valetudo/roborock_s7/BatteryStateAttribute/level", {
            "level": 85,
            "entity_id": "roborock_s7",
        })
        assert event.system == "valetudo"
        assert event.event_type == "vacuum_battery"
        assert event.severity == Severity.INFO

    def test_vacuum_event(self) -> None:
        event = _ingest("valetudo/roborock_s7/events", {
            "type": "consumable_depleted",
            "subtype": "main_brush",
            "entity_id": "roborock_s7",
            "severity": "warning",
        })
        assert event.system == "valetudo"
        assert event.event_type == "vacuum_event"
        assert event.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# WLED
# ---------------------------------------------------------------------------


class TestWled:
    def test_device_status(self) -> None:
        event = _ingest("wled/desk_light/status", {
            "state": "on",
            "brightness": 128,
            "freeheap": 45000,
            "wifi_signal": -55,
            "entity_id": "desk_light",
        })
        assert event.system == "wled"
        assert event.event_type == "device_status"
        assert event.entity_id == "desk_light"

    def test_device_status_low_heap(self) -> None:
        event = _ingest("wled/led_strip_01/status", {
            "freeheap": 8000,
            "entity_id": "led_strip_01",
            "severity": "warning",
        })
        assert event.system == "wled"
        assert event.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# BirdNET
# ---------------------------------------------------------------------------


class TestBirdnet:
    def test_sighting(self) -> None:
        event = _ingest("birdnet/sightings", {
            "species": "Turdus merula",
            "common_name": "Eurasian Blackbird",
            "confidence": 0.92,
            "entity_id": "birdnet",
        })
        assert event.system == "birdnet"
        assert event.event_type == "sighting"
        assert event.severity == Severity.INFO
        assert event.payload["species"] == "Turdus merula"


# ---------------------------------------------------------------------------
# Known fixes YAML validation
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_iot_yaml_loads_and_has_expected_fixes(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "iot.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fix_ids = {fix["id"] for fix in data["fixes"]}

        expected_ids = {
            "z2m-bridge-offline",
            "z2m-device-offline",
            "frigate-service-offline",
            "frigate-camera-fps-zero",
            "frigate-detector-overload",
            "wled-low-heap",
            "wled-wifi-weak",
            "valetudo-error-state",
            "valetudo-consumable-expired",
            "espresense-node-offline",
        }
        assert expected_ids == fix_ids

    def test_all_fixes_have_required_fields(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "iot.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        for fix in data["fixes"]:
            assert "id" in fix, f"Fix missing 'id': {fix}"
            assert "match" in fix, f"Fix {fix['id']} missing 'match'"
            assert "diagnosis" in fix, f"Fix {fix['id']} missing 'diagnosis'"
            assert "action" in fix, f"Fix {fix['id']} missing 'action'"
            assert "risk_tier" in fix, f"Fix {fix['id']} missing 'risk_tier'"

    def test_all_fixes_use_valid_risk_tiers(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "iot.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        valid_tiers = {"auto_fix", "recommend", "escalate"}
        for fix in data["fixes"]:
            assert fix["risk_tier"] in valid_tiers, (
                f"Fix {fix['id']} has invalid risk_tier: {fix['risk_tier']}"
            )
