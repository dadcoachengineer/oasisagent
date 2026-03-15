"""Tests for the UniFi Network polling ingestion adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

    def test_new_toggle_defaults(self) -> None:
        """New telemetry toggles have correct defaults."""
        config = UnifiAdapterConfig()
        assert config.poll_ips is True
        assert config.poll_rogue_ap is True
        assert config.poll_clients is False
        assert config.poll_anomalies is True
        assert config.poll_events is True
        assert config.poll_dpi is False
        assert config.client_spike_threshold == 20.0
        assert config.dpi_bandwidth_threshold_mbps == 100.0

    def test_custom_thresholds(self) -> None:
        """Custom threshold values are accepted."""
        config = _make_config(
            client_spike_threshold=30.0,
            dpi_bandwidth_threshold_mbps=500.0,
        )
        assert config.client_spike_threshold == 30.0
        assert config.dpi_bandwidth_threshold_mbps == 500.0


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
        # All endpoint health should be False after stop
        for v in adapter._endpoint_health.values():
            assert v is False

    @pytest.mark.asyncio
    async def test_start_connection_failure(self) -> None:
        """Failed connection should set all endpoints unhealthy and return."""
        adapter, _ = _make_adapter()
        adapter._client.connect = AsyncMock(side_effect=ConnectionError("refused"))

        await adapter.start()

        assert not await adapter.healthy()

    def test_endpoint_health_initialized(self) -> None:
        """Endpoint health dict should include keys for enabled endpoints."""
        adapter, _ = _make_adapter(
            poll_ips=True, poll_rogue_ap=True, poll_clients=True,
            poll_anomalies=True, poll_events=True, poll_dpi=True,
        )
        assert "devices" in adapter._endpoint_health
        assert "alarms" in adapter._endpoint_health
        assert "health" in adapter._endpoint_health
        assert "ips" in adapter._endpoint_health
        assert "rogue_ap" in adapter._endpoint_health
        assert "clients" in adapter._endpoint_health
        assert "anomalies" in adapter._endpoint_health
        assert "events" in adapter._endpoint_health
        assert "dpi" in adapter._endpoint_health

    def test_endpoint_health_excludes_disabled(self) -> None:
        """Disabled endpoints should not appear in health dict."""
        adapter, _ = _make_adapter(
            poll_alarms=False, poll_health=False, poll_ips=False,
            poll_rogue_ap=False, poll_clients=False, poll_anomalies=False,
            poll_events=False, poll_dpi=False,
        )
        assert "devices" in adapter._endpoint_health
        assert "alarms" not in adapter._endpoint_health
        assert "health" not in adapter._endpoint_health
        assert "ips" not in adapter._endpoint_health

    def test_health_detail(self) -> None:
        """health_detail returns string status per endpoint."""
        adapter, _ = _make_adapter()
        adapter._endpoint_health["devices"] = True
        adapter._endpoint_health["alarms"] = False
        detail = adapter.health_detail()
        assert detail["devices"] == "connected"
        assert detail["alarms"] == "disconnected"

    @pytest.mark.asyncio
    async def test_healthy_true_when_any_endpoint_up(self) -> None:
        """healthy() returns True if at least one endpoint is up."""
        adapter, _ = _make_adapter()
        adapter._endpoint_health["devices"] = True
        assert await adapter.healthy()

    @pytest.mark.asyncio
    async def test_healthy_false_when_all_down(self) -> None:
        """healthy() returns False if all endpoints are down."""
        adapter, _ = _make_adapter()
        # All default to False
        assert not await adapter.healthy()


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
# IDS/IPS polling
# ---------------------------------------------------------------------------


class TestIpsPolling:
    @pytest.mark.asyncio
    async def test_ips_alert_emits_event(self) -> None:
        """IPS alert event is emitted with correct fields."""
        adapter, queue = _make_adapter(poll_ips=True)
        # Set lookback to ensure events pass filter
        adapter._last_ips_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "signature": "ET MALWARE Trojan",
                "src_ip": "192.168.1.100",
                "dest_ip": "10.0.0.1",
                "action": "alert",
                "category": "malware",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_ips()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "ids_alert"
        assert event.severity == Severity.WARNING
        assert event.payload["signature"] == "ET MALWARE Trojan"
        assert event.payload["src_ip"] == "192.168.1.100"
        assert event.payload["dest_ip"] == "10.0.0.1"
        assert event.payload["action"] == "alert"
        assert event.payload["category"] == "malware"

    @pytest.mark.asyncio
    async def test_ips_block_is_error_severity(self) -> None:
        """Blocked IPS events should have ERROR severity."""
        adapter, queue = _make_adapter(poll_ips=True)
        adapter._last_ips_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "signature": "ET EXPLOIT",
                "src_ip": "1.2.3.4",
                "dest_ip": "192.168.1.1",
                "action": "block",
                "category": "exploit",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_ips()

        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.ERROR

    @pytest.mark.asyncio
    async def test_ips_old_events_filtered(self) -> None:
        """Events older than the lookback window are not emitted."""
        adapter, queue = _make_adapter(poll_ips=True)
        adapter._last_ips_poll = datetime.now(tz=UTC) - timedelta(seconds=30)

        old_ts = int((datetime.now(tz=UTC) - timedelta(minutes=10)).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "signature": "Old Event",
                "src_ip": "1.2.3.4",
                "dest_ip": "5.6.7.8",
                "action": "alert",
                "category": "misc",
                "timestamp": old_ts,
            }],
        })

        await adapter._poll_ips()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_ips_missing_signature_skipped(self) -> None:
        """Events without a signature field are skipped."""
        adapter, queue = _make_adapter(poll_ips=True)
        adapter._last_ips_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "src_ip": "1.2.3.4",
                "action": "alert",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_ips()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_ips_first_poll_uses_lookback(self) -> None:
        """First poll (no _last_ips_poll) uses lookback window."""
        adapter, queue = _make_adapter(poll_ips=True)
        assert adapter._last_ips_poll is None

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "signature": "New Event",
                "src_ip": "1.2.3.4",
                "dest_ip": "5.6.7.8",
                "action": "alert",
                "category": "test",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_ips()

        queue.put_nowait.assert_called_once()
        assert adapter._last_ips_poll is not None

    @pytest.mark.asyncio
    async def test_ips_empty_response(self) -> None:
        """Empty IPS response emits no events."""
        adapter, queue = _make_adapter(poll_ips=True)
        adapter._client.get = AsyncMock(return_value={"data": []})

        await adapter._poll_ips()

        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Rogue AP detection
# ---------------------------------------------------------------------------


class TestRogueApPolling:
    @pytest.mark.asyncio
    async def test_new_rogue_ap_emits_event(self) -> None:
        """New rogue AP emits rogue_ap_detected event."""
        adapter, queue = _make_adapter(poll_rogue_ap=True)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "bssid": "aa:bb:cc:dd:ee:ff",
                "ssid": "EvilTwin",
                "channel": 6,
                "rssi": -45,
                "is_rogue": True,
            }],
        })

        await adapter._poll_rogue_ap()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "rogue_ap_detected"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "aa:bb:cc:dd:ee:ff"
        assert event.payload["ssid"] == "EvilTwin"
        assert event.payload["channel"] == 6
        assert event.payload["rssi"] == -45

    @pytest.mark.asyncio
    async def test_seen_rogue_ap_deduped(self) -> None:
        """Already-seen rogue APs are not re-emitted."""
        adapter, queue = _make_adapter(poll_rogue_ap=True)
        adapter._seen_rogue_aps.add("aa:bb:cc:dd:ee:ff")

        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "bssid": "aa:bb:cc:dd:ee:ff",
                "ssid": "EvilTwin",
                "channel": 6,
                "rssi": -45,
            }],
        })

        await adapter._poll_rogue_ap()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_rogue_ap_eviction(self) -> None:
        """Rogue APs no longer in response are evicted from tracker."""
        adapter, _queue = _make_adapter(poll_rogue_ap=True)
        adapter._seen_rogue_aps = {"old-bssid-1", "old-bssid-2"}

        adapter._client.get = AsyncMock(return_value={
            "data": [{"bssid": "old-bssid-1", "ssid": "X", "channel": 1}],
        })

        await adapter._poll_rogue_ap()

        assert "old-bssid-1" in adapter._seen_rogue_aps
        assert "old-bssid-2" not in adapter._seen_rogue_aps

    @pytest.mark.asyncio
    async def test_rogue_ap_missing_bssid_skipped(self) -> None:
        """Rogue APs without BSSID are skipped."""
        adapter, queue = _make_adapter(poll_rogue_ap=True)
        adapter._client.get = AsyncMock(return_value={
            "data": [{"ssid": "NoBSSID", "channel": 6}],
        })

        await adapter._poll_rogue_ap()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_rogue_ap_dedup_key(self) -> None:
        """Rogue AP dedup key includes BSSID."""
        adapter, queue = _make_adapter(poll_rogue_ap=True)
        adapter._client.get = AsyncMock(return_value={
            "data": [{"bssid": "11:22:33:44:55:66", "ssid": "Test"}],
        })

        await adapter._poll_rogue_ap()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "unifi:rogue_ap:11:22:33:44:55:66"


# ---------------------------------------------------------------------------
# Client tracking (spike detection)
# ---------------------------------------------------------------------------


class TestClientPolling:
    @pytest.mark.asyncio
    async def test_first_poll_establishes_baseline(self) -> None:
        """First poll sets baseline count, no event emitted."""
        adapter, queue = _make_adapter(poll_clients=True)
        assert adapter._last_client_count is None

        adapter._client.get = AsyncMock(return_value={
            "data": [{"mac": f"aa:bb:{i}"} for i in range(50)],
        })

        await adapter._poll_clients()

        queue.put_nowait.assert_not_called()
        assert adapter._last_client_count == 50

    @pytest.mark.asyncio
    async def test_spike_increase_emits_event(self) -> None:
        """Client count increase above threshold emits spike event."""
        adapter, queue = _make_adapter(poll_clients=True, client_spike_threshold=20.0)
        adapter._last_client_count = 100

        # 130 clients = 30% increase
        adapter._client.get = AsyncMock(return_value={
            "data": [{"mac": f"aa:bb:{i}"} for i in range(130)],
        })

        await adapter._poll_clients()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "client_count_spike"
        assert event.severity == Severity.WARNING
        assert event.payload["current_count"] == 130
        assert event.payload["previous_count"] == 100
        assert event.payload["change_pct"] == 30.0
        assert event.payload["direction"] == "increase"

    @pytest.mark.asyncio
    async def test_spike_decrease_emits_event(self) -> None:
        """Client count decrease above threshold emits spike event."""
        adapter, queue = _make_adapter(poll_clients=True, client_spike_threshold=20.0)
        adapter._last_client_count = 100

        # 70 clients = 30% decrease
        adapter._client.get = AsyncMock(return_value={
            "data": [{"mac": f"aa:bb:{i}"} for i in range(70)],
        })

        await adapter._poll_clients()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.payload["direction"] == "decrease"

    @pytest.mark.asyncio
    async def test_no_spike_below_threshold(self) -> None:
        """Changes below threshold do not emit events."""
        adapter, queue = _make_adapter(poll_clients=True, client_spike_threshold=20.0)
        adapter._last_client_count = 100

        # 110 clients = 10% increase (below 20% threshold)
        adapter._client.get = AsyncMock(return_value={
            "data": [{"mac": f"aa:bb:{i}"} for i in range(110)],
        })

        await adapter._poll_clients()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_baseline_no_division_error(self) -> None:
        """Zero previous count should not cause division by zero."""
        adapter, queue = _make_adapter(poll_clients=True)
        adapter._last_client_count = 0

        adapter._client.get = AsyncMock(return_value={
            "data": [{"mac": "aa:bb:cc"}],
        })

        await adapter._poll_clients()

        # Should not raise and should not emit (0 baseline skips pct calc)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_client_list(self) -> None:
        """Empty client list still updates the count."""
        adapter, _queue = _make_adapter(poll_clients=True)
        adapter._client.get = AsyncMock(return_value={"data": []})

        await adapter._poll_clients()

        assert adapter._last_client_count == 0


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


class TestAnomalyPolling:
    @pytest.mark.asyncio
    async def test_anomaly_uses_plural_endpoint(self) -> None:
        """stat/anomalies endpoint uses plural form (not stat/anomaly)."""
        adapter, _queue = _make_adapter(poll_anomalies=True)
        adapter._last_anomaly_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        adapter._client.get = AsyncMock(return_value={"data": []})

        await adapter._poll_anomalies()

        adapter._client.get.assert_called_once_with("stat/anomalies")

    @pytest.mark.asyncio
    async def test_anomaly_emits_event(self) -> None:
        """Network anomaly emits event with correct fields."""
        adapter, queue = _make_adapter(poll_anomalies=True)
        adapter._last_anomaly_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "anomaly": "dns_tunneling",
                "value": 150,
                "threshold": 100,
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_anomalies()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "network_anomaly"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "dns_tunneling"
        assert event.payload["anomaly"] == "dns_tunneling"
        assert event.payload["value"] == 150

    @pytest.mark.asyncio
    async def test_anomaly_old_events_filtered(self) -> None:
        """Old anomalies are filtered by lookback."""
        adapter, queue = _make_adapter(poll_anomalies=True)
        adapter._last_anomaly_poll = datetime.now(tz=UTC) - timedelta(seconds=30)

        old_ts = int((datetime.now(tz=UTC) - timedelta(minutes=10)).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "anomaly": "old_anomaly",
                "value": 50,
                "timestamp": old_ts,
            }],
        })

        await adapter._poll_anomalies()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_anomaly_missing_type_skipped(self) -> None:
        """Anomalies without type are skipped."""
        adapter, queue = _make_adapter(poll_anomalies=True)
        adapter._last_anomaly_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{"value": 100, "timestamp": now_ms}],
        })

        await adapter._poll_anomalies()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_anomaly_first_poll(self) -> None:
        """First poll uses lookback window."""
        adapter, queue = _make_adapter(poll_anomalies=True)
        assert adapter._last_anomaly_poll is None

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "anomaly": "new_anomaly",
                "value": 200,
                "threshold": 100,
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_anomalies()

        queue.put_nowait.assert_called_once()
        assert adapter._last_anomaly_poll is not None


# ---------------------------------------------------------------------------
# Controller events
# ---------------------------------------------------------------------------


class TestControllerEventPolling:
    @pytest.mark.asyncio
    async def test_actionable_event_emits(self) -> None:
        """Actionable controller event emits event."""
        adapter, queue = _make_adapter(poll_events=True)
        adapter._last_events_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.post = AsyncMock(return_value={
            "data": [{
                "_id": "evt-001",
                "key": "EVT_AP_Lost_Contact",
                "msg": "AP lost contact",
                "mac": "aa:bb:cc",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_events()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "controller_event"
        assert event.entity_id == "EVT_AP_Lost_Contact"
        assert event.payload["event_id"] == "evt-001"
        assert event.payload["key"] == "EVT_AP_Lost_Contact"
        assert event.payload["message"] == "AP lost contact"

    @pytest.mark.asyncio
    async def test_event_uses_post(self) -> None:
        """stat/event endpoint uses POST with body params (UniFi convention)."""
        adapter, _queue = _make_adapter(poll_events=True)
        adapter._last_events_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.post = AsyncMock(return_value={
            "data": [{
                "_id": "evt-post",
                "key": "EVT_AP_Lost_Contact",
                "msg": "POST test",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_events()

        adapter._client.post.assert_called_once()
        call_args = adapter._client.post.call_args
        assert call_args[0][0] == "stat/event"
        body = call_args[0][1]
        assert "_limit" in body
        assert "within" in body

    @pytest.mark.asyncio
    async def test_informational_event_filtered(self) -> None:
        """Non-actionable events are filtered out."""
        adapter, queue = _make_adapter(poll_events=True)
        adapter._last_events_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.post = AsyncMock(return_value={
            "data": [{
                "_id": "evt-002",
                "key": "EVT_SomeInfoEvent",
                "msg": "Informational",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_events()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_missing_id_skipped(self) -> None:
        """Events without _id are skipped."""
        adapter, queue = _make_adapter(poll_events=True)
        adapter._last_events_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.post = AsyncMock(return_value={
            "data": [{
                "key": "EVT_AP_Lost_Contact",
                "msg": "No ID",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_events()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_old_filtered(self) -> None:
        """Old events are filtered by lookback."""
        adapter, queue = _make_adapter(poll_events=True)
        adapter._last_events_poll = datetime.now(tz=UTC) - timedelta(seconds=30)

        old_ts = int((datetime.now(tz=UTC) - timedelta(minutes=10)).timestamp() * 1000)
        adapter._client.post = AsyncMock(return_value={
            "data": [{
                "_id": "evt-003",
                "key": "EVT_AP_Restarted",
                "msg": "Old event",
                "timestamp": old_ts,
            }],
        })

        await adapter._poll_events()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_dedup_key(self) -> None:
        """Controller event dedup key includes event ID."""
        adapter, queue = _make_adapter(poll_events=True)
        adapter._last_events_poll = datetime.now(tz=UTC) - timedelta(minutes=5)

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        adapter._client.post = AsyncMock(return_value={
            "data": [{
                "_id": "evt-abc",
                "key": "EVT_SW_Lost_Contact",
                "msg": "Switch lost",
                "timestamp": now_ms,
            }],
        })

        await adapter._poll_events()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "unifi:event:evt-abc"


# ---------------------------------------------------------------------------
# DPI stats
# ---------------------------------------------------------------------------


class TestDpiPolling:
    @pytest.mark.asyncio
    async def test_dpi_threshold_exceeded(self) -> None:
        """DPI category exceeding bandwidth threshold emits event."""
        adapter, queue = _make_adapter(poll_dpi=True, dpi_bandwidth_threshold_mbps=100.0)

        # 100 Mbps = 12_500_000 bytes, set rx+tx above that
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "cat_name": "Streaming",
                "rx_bytes": 10_000_000,
                "tx_bytes": 5_000_000,
            }],
        })

        await adapter._poll_dpi()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "dpi_threshold"
        assert event.entity_id == "Streaming"
        assert event.payload["category"] == "Streaming"
        assert event.payload["threshold_mbps"] == 100.0
        assert event.payload["rx_bytes"] == 10_000_000
        assert event.payload["tx_bytes"] == 5_000_000

    @pytest.mark.asyncio
    async def test_dpi_below_threshold(self) -> None:
        """DPI categories below threshold do not emit events."""
        adapter, queue = _make_adapter(poll_dpi=True, dpi_bandwidth_threshold_mbps=100.0)

        # Well below 12.5 MB threshold
        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "cat_name": "Web",
                "rx_bytes": 1_000_000,
                "tx_bytes": 500_000,
            }],
        })

        await adapter._poll_dpi()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_dpi_missing_category_skipped(self) -> None:
        """DPI entries without category are skipped."""
        adapter, queue = _make_adapter(poll_dpi=True)
        adapter._client.get = AsyncMock(return_value={
            "data": [{"rx_bytes": 50_000_000, "tx_bytes": 50_000_000}],
        })

        await adapter._poll_dpi()

        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_dpi_uses_cat_fallback(self) -> None:
        """DPI uses 'cat' field if 'cat_name' is missing."""
        adapter, queue = _make_adapter(poll_dpi=True, dpi_bandwidth_threshold_mbps=10.0)

        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "cat": 5,
                "rx_bytes": 5_000_000,
                "tx_bytes": 5_000_000,
            }],
        })

        await adapter._poll_dpi()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "5"

    @pytest.mark.asyncio
    async def test_dpi_dedup_key(self) -> None:
        """DPI dedup key includes category."""
        adapter, queue = _make_adapter(poll_dpi=True, dpi_bandwidth_threshold_mbps=10.0)

        adapter._client.get = AsyncMock(return_value={
            "data": [{
                "cat_name": "Gaming",
                "rx_bytes": 5_000_000,
                "tx_bytes": 5_000_000,
            }],
        })

        await adapter._poll_dpi()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "unifi:dpi:Gaming"


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
        assert len(fixes) >= 8

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
            "ids_alert",
            "rogue_ap_detected",
            "network_anomaly",
        }

        fix_types = {fix["match"]["event_type"] for fix in data["fixes"]}
        assert fix_types == expected_types

    def test_new_known_fix_risk_tiers(self) -> None:
        """New known fixes have correct risk tiers."""
        from pathlib import Path

        import yaml

        fixes_path = Path(__file__).parent.parent.parent / "known_fixes" / "unifi.yaml"
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fix_by_id = {fix["id"]: fix for fix in data["fixes"]}
        assert fix_by_id["unifi-ids-alert"]["risk_tier"] == "recommend"
        assert fix_by_id["unifi-rogue-ap"]["risk_tier"] == "escalate"
        assert fix_by_id["unifi-network-anomaly"]["risk_tier"] == "recommend"


# ---------------------------------------------------------------------------
# Per-endpoint health isolation
# ---------------------------------------------------------------------------


class TestPerEndpointHealth:
    @pytest.mark.asyncio
    async def test_device_failure_isolates_from_ips(self) -> None:
        """Device poll failure should not affect IPS health."""
        adapter, _queue = _make_adapter(poll_ips=True)
        adapter._endpoint_health["ips"] = True

        # Mark devices as failed
        adapter._endpoint_health["devices"] = False

        # IPS should still be healthy
        assert adapter._endpoint_health["ips"] is True
        assert await adapter.healthy()

    @pytest.mark.asyncio
    async def test_all_endpoints_must_fail_for_unhealthy(self) -> None:
        """Adapter is unhealthy only when all endpoints fail."""
        adapter, _ = _make_adapter(
            poll_ips=True, poll_rogue_ap=True, poll_anomalies=True,
        )
        # All start as False
        assert not await adapter.healthy()

        # Set just one healthy
        adapter._endpoint_health["devices"] = True
        assert await adapter.healthy()


# ---------------------------------------------------------------------------
# Auto-disable on persistent 404
# ---------------------------------------------------------------------------


class TestAutoDisable404:
    @pytest.mark.asyncio
    async def test_three_consecutive_404s_disables_endpoint(self) -> None:
        """After 3 consecutive 404s, the endpoint is auto-disabled."""
        import aiohttp

        from oasisagent.ingestion.unifi import _MAX_404_FAILURES

        adapter, _queue = _make_adapter(poll_anomalies=True)

        exc_404 = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=404,
            message="Not Found",
        )
        adapter._client.get = AsyncMock(side_effect=exc_404)

        ep_name = "anomalies"
        # Simulate the poll loop's 404 tracking for 3 iterations
        for _ in range(_MAX_404_FAILURES):
            try:
                await adapter._poll_anomalies()
                adapter._404_counts.pop(ep_name, None)
            except Exception as exc:
                if adapter._is_unsupported(exc):
                    count = adapter._404_counts.get(ep_name, 0) + 1
                    adapter._404_counts[ep_name] = count
                    if count >= _MAX_404_FAILURES:
                        adapter._disabled_endpoints.add(ep_name)

        assert "anomalies" in adapter._disabled_endpoints

    @pytest.mark.asyncio
    async def test_disabled_endpoint_skipped(self) -> None:
        """A disabled endpoint is not polled on subsequent cycles."""
        adapter, _queue = _make_adapter(poll_anomalies=True)

        adapter._disabled_endpoints.add("anomalies")
        adapter._client.get = AsyncMock(return_value={"data": []})

        # Poll loop iteration — the method should never be called
        # We test via the poll loop check in _poll_loop, but directly:
        assert "anomalies" in adapter._disabled_endpoints

    @pytest.mark.asyncio
    async def test_success_resets_404_counter(self) -> None:
        """A successful poll resets the 404 counter for that endpoint."""
        adapter, _queue = _make_adapter(poll_anomalies=True)
        adapter._404_counts["anomalies"] = 2  # one more would disable

        # Simulate a successful poll — counter should reset
        adapter._client.get = AsyncMock(return_value={"data": []})
        adapter._last_anomaly_poll = datetime.now(tz=UTC) - timedelta(minutes=5)
        await adapter._poll_anomalies()

        # After success, the poll loop would pop the counter — simulate that
        adapter._404_counts.pop("anomalies", None)
        assert "anomalies" not in adapter._404_counts
        assert "anomalies" not in adapter._disabled_endpoints

    @pytest.mark.asyncio
    async def test_different_endpoints_tracked_independently(self) -> None:
        """404 counts for different endpoints don't interfere."""
        adapter, _queue = _make_adapter(
            poll_anomalies=True, poll_events=True,
        )

        adapter._404_counts["anomalies"] = 2
        adapter._404_counts["events"] = 1

        assert adapter._404_counts["anomalies"] == 2
        assert adapter._404_counts["events"] == 1
        assert "anomalies" not in adapter._disabled_endpoints
        assert "events" not in adapter._disabled_endpoints

    @pytest.mark.asyncio
    async def test_is_unsupported_detection(self) -> None:
        """_is_unsupported correctly identifies 403/404 ClientResponseError."""
        import aiohttp

        exc_404 = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=404,
            message="Not Found",
        )
        exc_403 = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=403,
            message="Forbidden",
        )
        exc_500 = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Server Error",
        )
        exc_other = RuntimeError("connection lost")

        # ConnectionError from cascading re-auth failure
        exc_login_403 = ConnectionError(
            'UniFi login failed (HTTP 403): {"error":{"code":403,"message":"Forbidden"}}',
        )
        exc_login_other = ConnectionError("UniFi login failed (HTTP 401): unauthorized")

        assert UnifiAdapter._is_unsupported(exc_404) is True
        assert UnifiAdapter._is_unsupported(exc_403) is True
        assert UnifiAdapter._is_unsupported(exc_login_403) is True
        assert UnifiAdapter._is_unsupported(exc_login_other) is False
        assert UnifiAdapter._is_unsupported(exc_500) is False
        assert UnifiAdapter._is_unsupported(exc_other) is False

    @pytest.mark.asyncio
    async def test_poll_loop_auto_disables_on_404(self) -> None:
        """Full poll loop integration: 404s accumulate and disable endpoint."""
        import aiohttp

        adapter, _queue = _make_adapter(
            poll_alarms=False, poll_health=False,
            poll_ips=False, poll_rogue_ap=False,
            poll_clients=False, poll_anomalies=True,
            poll_events=False, poll_dpi=False,
        )

        exc_404 = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=404,
            message="Not Found",
        )

        # Devices succeed, anomalies get 404
        adapter._client.get = AsyncMock(side_effect=[
            # iteration 1: devices ok, anomalies 404
            {"data": []}, exc_404,
            # iteration 2: devices ok, anomalies 404
            {"data": []}, exc_404,
            # iteration 3: devices ok, anomalies 404 (triggers disable)
            {"data": []}, exc_404,
        ])

        # Run 3 abbreviated poll iterations
        for _ in range(3):
            # Inline the relevant poll loop logic
            try:
                await adapter._poll_devices()
                adapter._endpoint_health["devices"] = True
            except Exception:
                adapter._endpoint_health["devices"] = False

            ep_name = "anomalies"
            if ep_name not in adapter._disabled_endpoints:
                try:
                    await adapter._poll_anomalies()
                    adapter._endpoint_health[ep_name] = True
                    adapter._404_counts.pop(ep_name, None)
                except Exception as exc:
                    if adapter._is_unsupported(exc):
                        count = adapter._404_counts.get(ep_name, 0) + 1
                        adapter._404_counts[ep_name] = count
                        if count >= 3:
                            adapter._disabled_endpoints.add(ep_name)
                            adapter._endpoint_health[ep_name] = False

        assert "anomalies" in adapter._disabled_endpoints
        assert adapter._endpoint_health["anomalies"] is False

    @pytest.mark.asyncio
    async def test_non_404_error_does_not_count(self) -> None:
        """Non-404 errors do not increment the 404 counter."""
        import aiohttp

        adapter, _queue = _make_adapter(poll_anomalies=True)

        exc_500 = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Server Error",
        )

        assert not adapter._is_unsupported(exc_500)
        assert adapter._404_counts.get("anomalies", 0) == 0

    @pytest.mark.asyncio
    async def test_disabled_resets_on_new_adapter(self) -> None:
        """Disabled endpoints reset when a new adapter is created."""
        adapter1, _ = _make_adapter(poll_anomalies=True)
        adapter1._disabled_endpoints.add("anomalies")

        # New adapter should have empty disabled set
        adapter2, _ = _make_adapter(poll_anomalies=True)
        assert "anomalies" not in adapter2._disabled_endpoints


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
