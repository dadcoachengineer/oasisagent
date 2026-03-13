"""UniFi Network polling ingestion adapter.

Polls the UniFi controller API for device status, alarms, and
subsystem health. Emits events on state transitions only (dedup).

Three separate state trackers handle the different endpoint semantics:
- Device states: (mac, state_code) — state-based dedup
- Alarms: set of alarm _id — point-in-time dedup
- Health subsystems: (subsystem, status) — state-based dedup
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.clients.unifi import UnifiClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import UnifiAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)



class UnifiAdapter(IngestAdapter):
    """Polls UniFi controller for device status, alarms, and health.

    State-based dedup ensures events fire only on state transitions.
    Each endpoint has its own tracker with appropriate semantics.
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
        self._connected = False

        # Dedup trackers (separate semantics per endpoint)
        self._device_states: dict[str, int] = {}          # mac → state code
        self._device_cpu_alert: dict[str, bool] = {}      # mac → alert active
        self._device_mem_alert: dict[str, bool] = {}      # mac → alert active
        self._seen_alarms: set[str] = set()               # alarm _id
        self._health_states: dict[str, str] = {}          # subsystem → status

    @property
    def name(self) -> str:
        return "unifi"

    async def start(self) -> None:
        """Connect to controller and start the polling loop."""
        try:
            await self._client.connect()
            self._connected = True
        except Exception as exc:
            logger.error("UniFi adapter: connection failed: %s", exc)
            self._connected = False
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
        self._connected = False

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main polling loop — polls all endpoints each interval."""
        consecutive_failures = 0
        _MAX_BACKOFF = 300  # 5 minutes cap

        while not self._stopping:
            try:
                await self._poll_devices()

                if self._config.poll_alarms:
                    await self._poll_alarms()

                if self._config.poll_health:
                    await self._poll_health()

                self._connected = True
                consecutive_failures = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                consecutive_failures += 1
                backoff = min(
                    self._config.poll_interval * (2 ** (consecutive_failures - 1)),
                    _MAX_BACKOFF,
                )
                logger.warning(
                    "UniFi poll error (attempt %d, next retry in %ds): %s",
                    consecutive_failures, backoff, exc,
                )
                self._connected = False
                for _ in range(backoff):
                    if self._stopping:
                        return
                    await asyncio.sleep(1)
                continue

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
    # Helpers
    # -----------------------------------------------------------------

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "UniFi adapter: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
