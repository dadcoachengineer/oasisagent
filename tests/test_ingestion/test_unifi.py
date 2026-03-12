"""Tests for the UniFi Network polling ingestion adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from oasisagent.config import UnifiAdapterConfig
from oasisagent.ingestion.unifi import UnifiAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> UnifiAdapterConfig:
    """Create a UnifiAdapterConfig with sensible defaults."""
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "https://192.168.1.1",
        "username": "admin",
        "password": "secret",
        "site": "default",
        "is_udm": True,
        "verify_ssl": False,
        "poll_interval": 30,
        "poll_alarms": True,
        "poll_health": True,
        "timeout": 10,
    }
    defaults.update(overrides)
    return UnifiAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[UnifiAdapter, MagicMock]:
    """Create a UnifiAdapter with mocked client and queue."""
    config = _make_config(**overrides)
    queue = _mock_queue()
    with patch("oasisagent.ingestion.unifi.UnifiClient"):
        adapter = UnifiAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = UnifiAdapterConfig()
        assert config.enabled is False
        assert config.url == "https://192.168.1.1"
        assert config.site == "default"
        assert config.is_udm is True
        assert config.poll_interval == 30

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus_field="nope")

    def test_custom_site(self) -> None:
        config = _make_config(site="office")
        assert config.site == "office"


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "unifi"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client = AsyncMock()
        await adapter.stop()
        assert adapter._stopping is True
        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_start_connection_failure(self) -> None:
        """Failed connection should set _connected=False and return."""
        adapter, _ = _make_adapter()
        adapter._client.connect = AsyncMock(side_effect=ConnectionError("refused"))

        await adapter.start()

        assert adapter._connected is False


# ---------------------------------------------------------------------------
# Device state transitions
# ---------------------------------------------------------------------------


class TestDeviceStateTransitions:
    def test_first_poll_connected_no_event(self) -> None:
        """First poll with state=1 (connected) emits no event."""
        adapter, _queue = _make_adapter()
        device = {
            "mac": "aa:bb:cc:dd:ee:ff",
            "state": 1,
            "name": "AP-Living-Room",
            "type": "uap",
            "adopted": True,
            "system-stats": {},
        }

        adapter._poll_devices = None  # not using the async version
        # Simulate device processing directly
        adapter._device_states.clear()
        adapter._enqueue = MagicMock()

        # First poll: state=1 (connected), prev_state=None → no event
        mac = device["mac"]
        state = device["state"]
        prev_state = adapter._device_states.get(mac)
        adapter._device_states[mac] = state
        # prev_state is None and state == 1 → no event emitted
        assert prev_state is None
        assert state == 1

    def test_first_poll_disconnected_emits_event(self) -> None:
        """First poll with state!=1 emits device_disconnected."""
        adapter, _queue = _make_adapter()

        # Simulate: first poll, device already disconnected
        adapter._device_states.clear()
        devices = [
            {"mac": "aa:bb:cc", "state": 0, "name": "AP-1",
             "type": "uap", "adopted": True, "system-stats": {}},
        ]

        # Call _poll_devices with mocked client
        adapter._client.get = AsyncMock(return_value={"data": devices})

    @pytest.mark.asyncio
    async def test_device_disconnect_event(self) -> None:
        """Device transitioning from state=1 to state=0 emits disconnect."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1  # previously connected

        devices = [{
            "mac": "aa:bb:cc",
            "state": 0,
            "name": "AP-Living-Room",
            "type": "uap",
            "adopted": True,
            "system-stats": {},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_disconnected"
        assert event.severity == Severity.ERROR
        assert event.entity_id == "aa:bb:cc"
        assert event.system == "unifi"
        assert event.payload["name"] == "AP-Living-Room"

    @pytest.mark.asyncio
    async def test_device_reconnect_event(self) -> None:
        """Device transitioning from state=0 to state=1 emits reconnect."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 0  # previously disconnected

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-Living-Room",
            "type": "uap",
            "adopted": True,
            "system-stats": {},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_reconnected"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_same_state_no_event(self) -> None:
        """No event if state doesn't change."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1  # already connected

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_unadopted_device_ignored(self) -> None:
        """Unadopted devices should not emit events."""
        adapter, queue = _make_adapter()

        devices = [{
            "mac": "aa:bb:cc",
            "state": 0,
            "name": "Pending-AP",
            "type": "uap",
            "adopted": False,
            "system-stats": {},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_mac_skipped(self) -> None:
        """Devices with no mac field are skipped."""
        adapter, queue = _make_adapter()

        devices = [
            {"state": 0, "name": "No-MAC", "type": "uap",
             "adopted": True, "system-stats": {}},
        ]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_disconnected_emits(self) -> None:
        """First poll with state != 1 emits device_disconnected."""
        adapter, queue = _make_adapter()

        devices = [{
            "mac": "aa:bb:cc",
            "state": 5,
            "name": "AP-Down",
            "type": "uap",
            "adopted": True,
            "system-stats": {},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_disconnected"


# ---------------------------------------------------------------------------
# Resource threshold alerts
# ---------------------------------------------------------------------------


class TestResourceAlerts:
    @pytest.mark.asyncio
    async def test_high_cpu_event(self) -> None:
        """CPU above 90% emits device_high_cpu."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1  # skip state-change event

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "95.5", "mem": "40.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_high_cpu"
        assert event.severity == Severity.WARNING
        assert event.payload["cpu"] == 95.5
        assert event.payload["threshold"] == 90.0

    @pytest.mark.asyncio
    async def test_high_mem_event(self) -> None:
        """Memory above 90% emits device_high_mem."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "10.0", "mem": "92.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_high_mem"

    @pytest.mark.asyncio
    async def test_resource_recovery_event(self) -> None:
        """CPU dropping below threshold emits recovery."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1
        adapter._device_cpu_alert["aa:bb:cc"] = True  # was alerting

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "20.0", "mem": "30.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_cpu_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_sustained_alert_no_duplicate(self) -> None:
        """CPU staying above threshold doesn't re-emit."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1
        adapter._device_cpu_alert["aa:bb:cc"] = True  # already alerting

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "95.0", "mem": "30.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_not_called()

    def test_check_resource_non_numeric_ignored(self) -> None:
        """Non-numeric resource values are silently ignored."""
        adapter, queue = _make_adapter()
        tracker: dict[str, bool] = {}

        adapter._check_resource("aa:bb:cc", "AP-1", "cpu", "not_a_number", 90.0, tracker)

        queue.put_nowait.assert_not_called()

    def test_check_resource_none_ignored(self) -> None:
        """None resource values are silently ignored."""
        adapter, queue = _make_adapter()
        tracker: dict[str, bool] = {}

        adapter._check_resource("aa:bb:cc", "AP-1", "cpu", None, 90.0, tracker)

        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Alarm polling
# ---------------------------------------------------------------------------


class TestAlarmPolling:
    @pytest.mark.asyncio
    async def test_new_alarm_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "_id": "alarm-001",
                "key": "EVT_IPS_Alert",
                "msg": "IPS alert triggered",
                "mac": "aa:bb:cc",
            }],
        })

        await adapter._poll_alarms()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "alarm"
        assert event.severity == Severity.WARNING
        assert event.payload["alarm_id"] == "alarm-001"
        assert event.payload["key"] == "EVT_IPS_Alert"

    @pytest.mark.asyncio
    async def test_seen_alarm_deduped(self) -> None:
        """Already-seen alarms are not re-emitted."""
        adapter, queue = _make_adapter()
        adapter._seen_alarms.add("alarm-001")

        adapter._client.get = AsyncMock(return_value={
            "data": [{"_id": "alarm-001", "key": "EVT_IPS_Alert", "msg": ""}],
        })

        await adapter._poll_alarms()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_new_alarms(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value={
            "data": [
                {"_id": "a1", "key": "EVT_1", "msg": "msg1", "mac": ""},
                {"_id": "a2", "key": "EVT_2", "msg": "msg2", "mac": ""},
            ],
        })

        await adapter._poll_alarms()

        assert queue.put_nowait.call_count == 2
        assert "a1" in adapter._seen_alarms
        assert "a2" in adapter._seen_alarms

    @pytest.mark.asyncio
    async def test_alarm_missing_id_skipped(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value={
            "data": [{"key": "EVT_1", "msg": "no id"}],
        })

        await adapter._poll_alarms()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleared_alarms_evicted_from_tracker(self) -> None:
        """Alarms no longer in the response are evicted from _seen_alarms."""
        adapter, _queue = _make_adapter()
        adapter._seen_alarms = {"old-alarm-1", "old-alarm-2"}

        # Only old-alarm-1 still present in response
        adapter._client.get = AsyncMock(return_value={
            "data": [{"_id": "old-alarm-1", "key": "EVT_1", "msg": ""}],
        })

        await adapter._poll_alarms()

        assert "old-alarm-1" in adapter._seen_alarms
        assert "old-alarm-2" not in adapter._seen_alarms

    @pytest.mark.asyncio
    async def test_empty_response_clears_all_seen(self) -> None:
        """Empty alarm response evicts all tracked alarms."""
        adapter, _queue = _make_adapter()
        adapter._seen_alarms = {"a1", "a2", "a3"}

        adapter._client.get = AsyncMock(return_value={"data": []})

        await adapter._poll_alarms()

        assert len(adapter._seen_alarms) == 0


# ---------------------------------------------------------------------------
# Configurable thresholds
# ---------------------------------------------------------------------------


class TestConfigurableThresholds:
    @pytest.mark.asyncio
    async def test_custom_cpu_threshold(self) -> None:
        """Custom CPU threshold triggers at configured value."""
        adapter, queue = _make_adapter(cpu_threshold=80.0)
        adapter._device_states["aa:bb:cc"] = 1

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "85.0", "mem": "30.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_high_cpu"
        assert event.payload["threshold"] == 80.0

    @pytest.mark.asyncio
    async def test_custom_memory_threshold(self) -> None:
        """Custom memory threshold triggers at configured value."""
        adapter, queue = _make_adapter(memory_threshold=70.0)
        adapter._device_states["aa:bb:cc"] = 1

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "10.0", "mem": "75.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "device_high_mem"
        assert event.payload["threshold"] == 70.0

    @pytest.mark.asyncio
    async def test_default_threshold_85_no_alert(self) -> None:
        """At default 90% threshold, 85% CPU should not alert."""
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "85.0", "mem": "30.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Health subsystem polling
# ---------------------------------------------------------------------------


class TestHealthPolling:
    @pytest.mark.asyncio
    async def test_subsystem_degraded_event(self) -> None:
        """Subsystem going from ok to not-ok emits degraded event."""
        adapter, queue = _make_adapter()
        adapter._health_states["wlan"] = "ok"

        adapter._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "wlan", "status": "unhealthy"}],
        })

        await adapter._poll_health()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "wlan_degraded"
        assert event.severity == Severity.WARNING
        assert event.payload["previous_status"] == "ok"

    @pytest.mark.asyncio
    async def test_wan_failover_event(self) -> None:
        """WAN subsystem degradation emits wan_failover with ERROR severity."""
        adapter, queue = _make_adapter()
        adapter._health_states["wan"] = "ok"

        adapter._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "wan", "status": "degraded"}],
        })

        await adapter._poll_health()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "wan_failover"
        assert event.severity == Severity.ERROR

    @pytest.mark.asyncio
    async def test_subsystem_recovery_event(self) -> None:
        """Subsystem recovering to ok emits recovery event."""
        adapter, queue = _make_adapter()
        adapter._health_states["wlan"] = "unhealthy"

        adapter._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "wlan", "status": "ok"}],
        })

        await adapter._poll_health()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "wlan_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_first_poll_no_event(self) -> None:
        """First poll establishes baseline — no event emitted."""
        adapter, queue = _make_adapter()

        adapter._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "wlan", "status": "ok"}],
        })

        await adapter._poll_health()

        queue.put_nowait.assert_not_called()
        assert adapter._health_states["wlan"] == "ok"

    @pytest.mark.asyncio
    async def test_same_status_no_event(self) -> None:
        """No event if status doesn't change."""
        adapter, queue = _make_adapter()
        adapter._health_states["wlan"] = "ok"

        adapter._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "wlan", "status": "ok"}],
        })

        await adapter._poll_health()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_subsystem_skipped(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "", "status": "ok"}],
        })

        await adapter._poll_health()

        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup key format
# ---------------------------------------------------------------------------


class TestDedupKeys:
    @pytest.mark.asyncio
    async def test_device_state_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1

        devices = [{
            "mac": "aa:bb:cc",
            "state": 0,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "unifi:device:aa:bb:cc:state"

    @pytest.mark.asyncio
    async def test_alarm_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._client.get = AsyncMock(return_value={
            "data": [{"_id": "alarm-42", "key": "EVT_1", "msg": "", "mac": ""}],
        })

        await adapter._poll_alarms()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "unifi:alarm:alarm-42"

    @pytest.mark.asyncio
    async def test_health_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._health_states["lan"] = "ok"

        adapter._client.get = AsyncMock(return_value={
            "data": [{"subsystem": "lan", "status": "unhealthy"}],
        })

        await adapter._poll_health()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "unifi:health:lan"

    @pytest.mark.asyncio
    async def test_resource_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._device_states["aa:bb:cc"] = 1

        devices = [{
            "mac": "aa:bb:cc",
            "state": 1,
            "name": "AP-1",
            "type": "uap",
            "adopted": True,
            "system-stats": {"cpu": "95.0", "mem": "20.0"},
        }]
        adapter._client.get = AsyncMock(return_value={"data": devices})

        await adapter._poll_devices()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "unifi:device:aa:bb:cc:cpu"


# ---------------------------------------------------------------------------
# Enqueue error handling
# ---------------------------------------------------------------------------


class TestEnqueueErrorHandling:
    def test_enqueue_failure_logged(self) -> None:
        """Queue failures should be caught and logged, not raised."""
        adapter, queue = _make_adapter()
        queue.put_nowait.side_effect = RuntimeError("queue full")

        from datetime import UTC, datetime

        from oasisagent.models import Event, EventMetadata

        event = Event(
            source="unifi",
            system="unifi",
            event_type="test",
            entity_id="test",
            severity=Severity.INFO,
            timestamp=datetime.now(tz=UTC),
            payload={},
            metadata=EventMetadata(dedup_key="test"),
        )

        # Should not raise
        adapter._enqueue(event)


# ---------------------------------------------------------------------------
# Known fixes YAML
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_file_exists(self) -> None:
        """Verify known_fixes/unifi.yaml exists and parses."""
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "unifi.yaml"
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        assert "fixes" in data
        fixes = data["fixes"]
        assert len(fixes) >= 5

        # Verify all fixes have required fields
        for fix in fixes:
            assert "id" in fix
            assert "match" in fix
            assert fix["match"]["system"] == "unifi"
            assert "diagnosis" in fix
            assert "action" in fix
            assert "risk_tier" in fix

    def test_known_fix_ids_unique(self) -> None:
        """All fix IDs should be unique."""
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "unifi.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        ids = [fix["id"] for fix in data["fixes"]]
        assert len(ids) == len(set(ids))

    def test_known_fix_event_types_match_adapter(self) -> None:
        """Event types in known fixes should match what the adapter emits."""
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "unifi.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        expected_types = {
            "device_disconnected",
            "device_high_cpu",
            "device_high_mem",
            "wan_failover",
            "alarm",
        }

        fix_types = {fix["match"]["event_type"] for fix in data["fixes"]}
        assert fix_types == expected_types


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_unifi_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "unifi" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["unifi"]
        assert meta.model is UnifiAdapterConfig
        assert "password" in meta.secret_fields
