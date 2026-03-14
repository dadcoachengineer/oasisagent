"""UniFi Network polling ingestion adapter.

Polls the UniFi controller API for device status, alarms, subsystem
health, IDS/IPS events, rogue APs, client counts, anomalies,
controller events, and DPI stats. Emits events on state transitions,
time-based lookback, or threshold crossings.

Separate state trackers handle the different endpoint semantics:
- Device states: (mac, state_code) — state-based dedup
- Alarms: set of alarm _id — point-in-time dedup
- Health subsystems: (subsystem, status) — state-based dedup
- IDS/IPS events: time-based lookback window
- Rogue APs: state-based dedup by BSSID
- Client count: spike detection (percent change)
- Anomalies: time-based lookback window
- Controller events: time-based lookback window
- DPI stats: threshold-based (bandwidth per category)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.clients.unifi import UnifiClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import UnifiAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Number of consecutive 404 errors before auto-disabling an endpoint.
# Endpoints returning 404 are typically unsupported on the controller's
# firmware version — disabling avoids log spam every poll cycle.
_MAX_404_FAILURES = 3

# Controller event keys considered actionable (skip informational noise)
_ACTIONABLE_EVENT_KEYS = frozenset({
    "EVT_AP_Lost_Contact",
    "EVT_AP_Restarted",
    "EVT_AP_Adopted",
    "EVT_AP_Upgrade_Failed",
    "EVT_AP_Channel_Changed",
    "EVT_SW_Lost_Contact",
    "EVT_SW_Restarted",
    "EVT_SW_PoeOverload",
    "EVT_GW_Lost_Contact",
    "EVT_GW_Restarted",
    "EVT_GW_WANTransition",
    "EVT_IPS_Alert",
    "EVT_LAN_ClientBlocked",
    "EVT_WU_Connected",
    "EVT_WU_Disconnected",
    "EVT_DG_Overheating",
    "EVT_DG_FanFailed",
})


class UnifiAdapter(IngestAdapter):
    """Polls UniFi controller for device status, alarms, health, and more.

    State-based dedup ensures events fire only on state transitions.
    Each endpoint has its own tracker with appropriate semantics and
    independent health tracking.
    """

    def __init__(self, config: UnifiAdapterConfig, queue: EventQueue) -> None:
        super().__init__(queue)
        self._config = config
        self._client = UnifiClient(
            url=config.url,
            username=config.username,
            password=config.password,
            site=config.site,
            is_udm=config.is_udm,
            verify_ssl=config.verify_ssl,
            timeout=config.timeout,
        )
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

        # Per-endpoint health tracking (replaces single _connected bool)
        self._endpoint_health: dict[str, bool] = {"devices": False}
        if config.poll_alarms:
            self._endpoint_health["alarms"] = False
        if config.poll_health:
            self._endpoint_health["health"] = False
        if config.poll_ips:
            self._endpoint_health["ips"] = False
        if config.poll_rogue_ap:
            self._endpoint_health["rogue_ap"] = False
        if config.poll_clients:
            self._endpoint_health["clients"] = False
        if config.poll_anomalies:
            self._endpoint_health["anomalies"] = False
        if config.poll_events:
            self._endpoint_health["events"] = False
        if config.poll_dpi:
            self._endpoint_health["dpi"] = False

        # Auto-disable endpoints that persistently return 404
        self._404_counts: dict[str, int] = {}
        self._disabled_endpoints: set[str] = set()

        # Dedup trackers (separate semantics per endpoint)
        self._device_states: dict[str, int] = {}          # mac → state code
        self._device_cpu_alert: dict[str, bool] = {}      # mac → alert active
        self._device_mem_alert: dict[str, bool] = {}      # mac → alert active
        self._seen_alarms: set[str] = set()               # alarm _id
        self._health_states: dict[str, str] = {}          # subsystem → status

        # Time-based lookback trackers
        self._last_ips_poll: datetime | None = None
        self._last_anomaly_poll: datetime | None = None
        self._last_events_poll: datetime | None = None

        # State-based dedup trackers
        self._seen_rogue_aps: set[str] = set()            # BSSID
        self._last_client_count: int | None = None         # previous client count

    @property
    def name(self) -> str:
        return "unifi"

    async def start(self) -> None:
        """Connect to controller and start the polling loop."""
        try:
            await self._client.connect()
        except Exception as exc:
            logger.error("UniFi adapter: connection failed: %s", exc)
            for key in self._endpoint_health:
                self._endpoint_health[key] = False
            return

        self._task = asyncio.create_task(
            self._poll_loop(), name="unifi-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
        await self._client.close()
        for key in self._endpoint_health:
            self._endpoint_health[key] = False

    async def healthy(self) -> bool:
        if not self._endpoint_health:
            return False
        return any(self._endpoint_health.values())

    def health_detail(self) -> dict[str, str]:
        """Per-endpoint health for dashboard display."""
        return {
            endpoint: ("connected" if ok else "disconnected")
            for endpoint, ok in self._endpoint_health.items()
        }

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main polling loop — polls all endpoints each interval.

        Each endpoint is wrapped in its own try/except so a single failing
        endpoint doesn't block the others. 404 responses are tracked per
        endpoint; after ``_MAX_404_FAILURES`` consecutive 404s the endpoint
        is auto-disabled (likely unsupported on this firmware version).
        """
        while not self._stopping:
            # Devices always polled (no auto-disable — core endpoint)
            try:
                await self._poll_devices()
                self._endpoint_health["devices"] = True
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("UniFi devices poll error: %s", exc)
                self._endpoint_health["devices"] = False

            # Optional endpoints with auto-disable on persistent 404
            optional_endpoints: list[tuple[str, bool, Any]] = [
                ("alarms", self._config.poll_alarms, self._poll_alarms),
                ("health", self._config.poll_health, self._poll_health),
                ("ips", self._config.poll_ips, self._poll_ips),
                ("rogue_ap", self._config.poll_rogue_ap, self._poll_rogue_ap),
                ("clients", self._config.poll_clients, self._poll_clients),
                ("anomalies", self._config.poll_anomalies, self._poll_anomalies),
                ("events", self._config.poll_events, self._poll_events),
                ("dpi", self._config.poll_dpi, self._poll_dpi),
            ]

            for ep_name, enabled, poll_fn in optional_endpoints:
                if not enabled or ep_name in self._disabled_endpoints:
                    continue
                try:
                    await poll_fn()
                    self._endpoint_health[ep_name] = True
                    self._404_counts.pop(ep_name, None)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    if self._is_404(exc):
                        count = self._404_counts.get(ep_name, 0) + 1
                        self._404_counts[ep_name] = count
                        if count >= _MAX_404_FAILURES:
                            self._disabled_endpoints.add(ep_name)
                            self._endpoint_health[ep_name] = False
                            logger.warning(
                                "Auto-disabling %s endpoint "
                                "(not supported on this firmware)",
                                ep_name,
                            )
                    else:
                        logger.warning("UniFi %s poll error: %s", ep_name, exc)
                        self._endpoint_health[ep_name] = False

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Device polling
    # -----------------------------------------------------------------

    async def _poll_devices(self) -> None:
        """Poll stat/device for device state transitions and resource alerts."""
        data = await self._client.get("stat/device")
        devices: list[dict[str, Any]] = data.get("data", [])

        for device in devices:
            mac = device.get("mac", "")
            if not mac:
                continue

            state = device.get("state", 0)
            device_name = device.get("name", mac)
            device_type = device.get("type", "unknown")
            adopted = device.get("adopted", False)

            if not adopted:
                continue

            prev_state = self._device_states.get(mac)
            self._device_states[mac] = state

            if prev_state is not None and prev_state != state:
                if state != 1:
                    self._enqueue(Event(
                        source=self.name,
                        system="unifi",
                        event_type="device_disconnected",
                        entity_id=mac,
                        severity=Severity.ERROR,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "name": device_name,
                            "type": device_type,
                            "state": state,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"unifi:device:{mac}:state",
                        ),
                    ))
                elif prev_state != 1:
                    self._enqueue(Event(
                        source=self.name,
                        system="unifi",
                        event_type="device_reconnected",
                        entity_id=mac,
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "name": device_name,
                            "type": device_type,
                            "state": state,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"unifi:device:{mac}:state",
                        ),
                    ))
            elif prev_state is None and state != 1:
                # First poll, device already disconnected
                self._enqueue(Event(
                    source=self.name,
                    system="unifi",
                    event_type="device_disconnected",
                    entity_id=mac,
                    severity=Severity.ERROR,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "name": device_name,
                        "type": device_type,
                        "state": state,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"unifi:device:{mac}:state",
                    ),
                ))

            # Resource alerts
            sys_stats = device.get("system-stats", {})
            self._check_resource(
                mac, device_name, "cpu",
                sys_stats.get("cpu"), self._config.cpu_threshold,
                self._device_cpu_alert,
            )
            self._check_resource(
                mac, device_name, "mem",
                sys_stats.get("mem"), self._config.memory_threshold,
                self._device_mem_alert,
            )

    def _check_resource(
        self,
        mac: str,
        device_name: str,
        resource: str,
        value: object,
        threshold: float,
        alert_tracker: dict[str, bool],
    ) -> None:
        """Emit events on resource threshold transitions."""
        if value is None:
            return

        try:
            val = float(value)
        except (TypeError, ValueError):
            return

        was_alert = alert_tracker.get(mac, False)
        is_alert = val >= threshold
        alert_tracker[mac] = is_alert

        if is_alert and not was_alert:
            self._enqueue(Event(
                source=self.name,
                system="unifi",
                event_type=f"device_high_{resource}",
                entity_id=mac,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "name": device_name,
                    resource: val,
                    "threshold": threshold,
                },
                metadata=EventMetadata(
                    dedup_key=f"unifi:device:{mac}:{resource}",
                ),
            ))
        elif not is_alert and was_alert:
            self._enqueue(Event(
                source=self.name,
                system="unifi",
                event_type=f"device_{resource}_recovered",
                entity_id=mac,
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "name": device_name,
                    resource: val,
                    "threshold": threshold,
                },
                metadata=EventMetadata(
                    dedup_key=f"unifi:device:{mac}:{resource}",
                ),
            ))

    # -----------------------------------------------------------------
    # Alarm polling
    # -----------------------------------------------------------------

    async def _poll_alarms(self) -> None:
        """Poll rest/alarm for new unarchived alarms."""
        data = await self._client.get("rest/alarm")
        alarms: list[dict[str, Any]] = data.get("data", [])

        for alarm in alarms:
            alarm_id = alarm.get("_id", "")
            if not alarm_id or alarm_id in self._seen_alarms:
                continue

            self._seen_alarms.add(alarm_id)
            alarm_key = alarm.get("key", "unknown_alarm")
            alarm_msg = alarm.get("msg", "")

            self._enqueue(Event(
                source=self.name,
                system="unifi",
                event_type="alarm",
                entity_id=alarm_key,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "alarm_id": alarm_id,
                    "key": alarm_key,
                    "message": alarm_msg,
                    "mac": alarm.get("mac", ""),
                },
                metadata=EventMetadata(
                    dedup_key=f"unifi:alarm:{alarm_id}",
                ),
            ))

        # Evict cleared/archived alarms to prevent unbounded growth
        current_ids = {a.get("_id", "") for a in alarms} - {""}
        self._seen_alarms &= current_ids

    # -----------------------------------------------------------------
    # Health polling
    # -----------------------------------------------------------------

    async def _poll_health(self) -> None:
        """Poll stat/health for subsystem status transitions."""
        data = await self._client.get("stat/health")
        subsystems: list[dict[str, Any]] = data.get("data", [])

        for sub in subsystems:
            subsystem = sub.get("subsystem", "")
            status = sub.get("status", "unknown")
            if not subsystem:
                continue

            prev_status = self._health_states.get(subsystem)
            self._health_states[subsystem] = status

            if prev_status is not None and prev_status != status:
                if status != "ok":
                    severity = Severity.ERROR if subsystem == "wan" else Severity.WARNING
                    event_type = (
                        "wan_failover" if subsystem == "wan"
                        else f"{subsystem}_degraded"
                    )
                    self._enqueue(Event(
                        source=self.name,
                        system="unifi",
                        event_type=event_type,
                        entity_id=subsystem,
                        severity=severity,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "subsystem": subsystem,
                            "status": status,
                            "previous_status": prev_status,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"unifi:health:{subsystem}",
                        ),
                    ))
                elif prev_status != "ok":
                    self._enqueue(Event(
                        source=self.name,
                        system="unifi",
                        event_type=f"{subsystem}_recovered",
                        entity_id=subsystem,
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "subsystem": subsystem,
                            "status": status,
                            "previous_status": prev_status,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"unifi:health:{subsystem}",
                        ),
                    ))

    # -----------------------------------------------------------------
    # IDS/IPS event polling
    # -----------------------------------------------------------------

    async def _poll_ips(self) -> None:
        """Poll stat/ips/event for IDS/IPS detections using time-based lookback."""
        now = datetime.now(tz=UTC)
        lookback = timedelta(minutes=max(self._config.poll_interval // 60 + 1, 2))

        since = self._last_ips_poll if self._last_ips_poll is not None else now - lookback
        self._last_ips_poll = now

        # Convert to epoch ms for UniFi API filtering
        since_epoch_ms = int(since.timestamp() * 1000)

        data = await self._client.get("stat/ips/event")
        events: list[dict[str, Any]] = data.get("data", [])

        for ips_event in events:
            # Filter by timestamp — only process events after our lookback
            event_ts = ips_event.get("timestamp", 0)
            if event_ts and event_ts < since_epoch_ms:
                continue

            signature = ips_event.get("signature", "")
            if not signature:
                continue

            action = ips_event.get("action", "alert")
            severity = Severity.ERROR if action == "block" else Severity.WARNING

            self._enqueue(Event(
                source=self.name,
                system="unifi",
                event_type="ids_alert",
                entity_id=signature,
                severity=severity,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "src_ip": ips_event.get("src_ip", ""),
                    "dest_ip": ips_event.get("dest_ip", ""),
                    "signature": signature,
                    "action": action,
                    "category": ips_event.get("category", ""),
                },
                metadata=EventMetadata(
                    dedup_key=f"unifi:ips:{signature}:{ips_event.get('src_ip', '')}",
                ),
            ))

    # -----------------------------------------------------------------
    # Rogue AP detection
    # -----------------------------------------------------------------

    async def _poll_rogue_ap(self) -> None:
        """Poll stat/rogueap for new rogue AP detections (state-based dedup)."""
        data = await self._client.get("stat/rogueap")
        rogue_aps: list[dict[str, Any]] = data.get("data", [])

        current_bssids: set[str] = set()

        for ap in rogue_aps:
            bssid = ap.get("bssid", "")
            if not bssid:
                continue

            current_bssids.add(bssid)

            if bssid in self._seen_rogue_aps:
                continue

            self._seen_rogue_aps.add(bssid)

            self._enqueue(Event(
                source=self.name,
                system="unifi",
                event_type="rogue_ap_detected",
                entity_id=bssid,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "bssid": bssid,
                    "ssid": ap.get("ssid", ""),
                    "channel": ap.get("channel", 0),
                    "rssi": ap.get("rssi", 0),
                    "is_rogue": ap.get("is_rogue", True),
                },
                metadata=EventMetadata(
                    dedup_key=f"unifi:rogue_ap:{bssid}",
                ),
            ))

        # Evict rogue APs no longer seen to prevent unbounded growth
        self._seen_rogue_aps &= current_bssids

    # -----------------------------------------------------------------
    # Client tracking (spike detection)
    # -----------------------------------------------------------------

    async def _poll_clients(self) -> None:
        """Poll stat/sta for client count spike detection.

        Does not emit per-client events (too high volume). Instead tracks
        total count and emits client_count_spike if count changes by more
        than the configured threshold percentage.
        """
        data = await self._client.get("stat/sta")
        clients: list[dict[str, Any]] = data.get("data", [])
        current_count = len(clients)

        if self._last_client_count is not None and self._last_client_count > 0:
            change = abs(current_count - self._last_client_count)
            pct_change = change / self._last_client_count * 100

            if pct_change >= self._config.client_spike_threshold:
                direction = "increase" if current_count > self._last_client_count else "decrease"
                self._enqueue(Event(
                    source=self.name,
                    system="unifi",
                    event_type="client_count_spike",
                    entity_id="clients",
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "current_count": current_count,
                        "previous_count": self._last_client_count,
                        "change_pct": round(pct_change, 1),
                        "direction": direction,
                        "threshold_pct": self._config.client_spike_threshold,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"unifi:clients:spike:{direction}",
                    ),
                ))

        self._last_client_count = current_count

    # -----------------------------------------------------------------
    # Anomaly detection
    # -----------------------------------------------------------------

    async def _poll_anomalies(self) -> None:
        """Poll stat/anomalies for network anomalies using time-based lookback."""
        now = datetime.now(tz=UTC)
        lookback = timedelta(minutes=max(self._config.poll_interval // 60 + 1, 2))

        since = self._last_anomaly_poll if self._last_anomaly_poll is not None else now - lookback
        self._last_anomaly_poll = now

        since_epoch_ms = int(since.timestamp() * 1000)

        data = await self._client.get("stat/anomalies")
        anomalies: list[dict[str, Any]] = data.get("data", [])

        for anomaly in anomalies:
            anomaly_ts = anomaly.get("timestamp", 0)
            if anomaly_ts and anomaly_ts < since_epoch_ms:
                continue

            anomaly_type = anomaly.get("anomaly", "")
            if not anomaly_type:
                continue

            self._enqueue(Event(
                source=self.name,
                system="unifi",
                event_type="network_anomaly",
                entity_id=anomaly_type,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "anomaly": anomaly_type,
                    "value": anomaly.get("value", 0),
                    "threshold": anomaly.get("threshold", 0),
                },
                metadata=EventMetadata(
                    dedup_key=f"unifi:anomaly:{anomaly_type}:{anomaly_ts}",
                ),
            ))

    # -----------------------------------------------------------------
    # Controller events
    # -----------------------------------------------------------------

    async def _poll_events(self) -> None:
        """Poll stat/event for actionable controller events using time-based lookback.

        The UniFi stat/event endpoint requires POST (not GET) even though
        it retrieves data. The POST body accepts ``_limit``, ``_start``,
        and ``within`` parameters to control the result window.
        """
        now = datetime.now(tz=UTC)
        lookback = timedelta(minutes=max(self._config.poll_interval // 60 + 1, 2))

        since = self._last_events_poll if self._last_events_poll is not None else now - lookback
        self._last_events_poll = now

        since_epoch_ms = int(since.timestamp() * 1000)

        # within = lookback in hours (minimum 1)
        within_hours = max(int(lookback.total_seconds() / 3600), 1)
        data = await self._client.post(
            "stat/event",
            {"_limit": 100, "within": within_hours},
        )
        events: list[dict[str, Any]] = data.get("data", [])

        for ctrl_event in events:
            event_ts = ctrl_event.get("timestamp", 0)
            if event_ts and event_ts < since_epoch_ms:
                continue

            event_key = ctrl_event.get("key", "")
            if not event_key or event_key not in _ACTIONABLE_EVENT_KEYS:
                continue

            event_id = ctrl_event.get("_id", "")
            if not event_id:
                continue

            self._enqueue(Event(
                source=self.name,
                system="unifi",
                event_type="controller_event",
                entity_id=event_key,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "event_id": event_id,
                    "key": event_key,
                    "message": ctrl_event.get("msg", ""),
                    "mac": ctrl_event.get("mac", ""),
                },
                metadata=EventMetadata(
                    dedup_key=f"unifi:event:{event_id}",
                ),
            ))

    # -----------------------------------------------------------------
    # DPI stats (bandwidth threshold)
    # -----------------------------------------------------------------

    async def _poll_dpi(self) -> None:
        """Poll stat/dpi for bandwidth threshold crossings by app category."""
        data = await self._client.get("stat/dpi")
        entries: list[dict[str, Any]] = data.get("data", [])

        threshold_bytes = self._config.dpi_bandwidth_threshold_mbps * 1_000_000 / 8

        for entry in entries:
            category = entry.get("cat_name", "") or entry.get("cat", "")
            if not category:
                continue

            # rx_bytes + tx_bytes for total bandwidth
            rx_bytes = entry.get("rx_bytes", 0)
            tx_bytes = entry.get("tx_bytes", 0)
            total_bytes = rx_bytes + tx_bytes

            if total_bytes >= threshold_bytes:
                total_mbps = round(total_bytes * 8 / 1_000_000, 2)
                self._enqueue(Event(
                    source=self.name,
                    system="unifi",
                    event_type="dpi_threshold",
                    entity_id=str(category),
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "category": str(category),
                        "total_mbps": total_mbps,
                        "rx_bytes": rx_bytes,
                        "tx_bytes": tx_bytes,
                        "threshold_mbps": self._config.dpi_bandwidth_threshold_mbps,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"unifi:dpi:{category}",
                    ),
                ))

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _is_404(exc: Exception) -> bool:
        """Return True if the exception represents an HTTP 404 response."""
        return isinstance(exc, aiohttp.ClientResponseError) and exc.status == 404

