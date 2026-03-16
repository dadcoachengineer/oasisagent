"""Uptime Kuma ingestion adapter.

Polls Uptime Kuma's Prometheus ``/metrics`` endpoint and emits events on
state transitions. Not a scanner — it polls an external monitoring service,
same pattern as the Cloudflare and UniFi adapters.

Events emitted on state transitions:
- ``monitor_down`` (ERROR) when status changes from up to down
- ``monitor_recovered`` (INFO) when status changes from down to up
- ``monitor_slow`` (WARNING) when response time exceeds threshold
  (with hysteresis: must drop below threshold*0.8 to clear)
- ``certificate_expiry`` (WARNING/ERROR) when cert days remaining crosses
  threshold (uses same threshold pattern as scanner.cert_expiry)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from oasisagent.clients.uptime_kuma import MonitorMetrics, UptimeKumaClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity, TopologyEdge, TopologyNode
from oasisagent.util.dedup import normalize_cert_dedup_key

if TYPE_CHECKING:
    from oasisagent.config import UptimeKumaAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class UptimeKumaAdapter(IngestAdapter):
    """Polls Uptime Kuma for monitor status, response time, and cert data.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: UptimeKumaAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._client = UptimeKumaClient(
            url=config.url,
            api_key=config.api_key,
            timeout=config.timeout,
        )
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers for dedup
        self._status_states: dict[str, bool] = {}  # monitor_name -> is_up
        self._cert_states: dict[str, str] = {}  # monitor_name -> "ok"|"warning"|"critical"
        self._slow_states: dict[str, bool] = {}  # monitor_name -> is_slow

    @property
    def name(self) -> str:
        return "uptime_kuma"

    async def start(self) -> None:
        """Connect to Uptime Kuma and start polling.

        Retries initial connection with back-off so a transient failure at
        startup doesn't permanently kill the adapter.
        """
        backoff = 5
        max_backoff = 300
        while not self._stopping:
            try:
                await self._client.start()
                self._connected = True
                break
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    "Uptime Kuma adapter: connection failed: %s "
                    "(retrying in %ds)", exc, backoff,
                )
                self._connected = False
                for _ in range(backoff):
                    if self._stopping:
                        return
                    await asyncio.sleep(1)
                backoff = min(backoff * 2, max_backoff)

        self._task = asyncio.create_task(
            self._poll_loop(), name="uptime-kuma-poller",
        )
        await self._task

    async def stop(self) -> None:
        """Stop polling and close the client."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self._client.close()

    async def healthy(self) -> bool:
        return self._connected

    async def _poll_loop(self) -> None:
        """Poll at configured interval, emitting events on state transitions."""
        while not self._stopping:
            try:
                monitors = await self._client.fetch_metrics()
                self._connected = True
                events = self._process_monitors(monitors)
                for event in events:
                    self._enqueue(event)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Uptime Kuma poll error: %s", exc)
                self._connected = False

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    def _process_monitors(self, monitors: list[MonitorMetrics]) -> list[Event]:
        """Process all monitors and return events for state transitions."""
        events: list[Event] = []
        for monitor in monitors:
            events.extend(self._check_status(monitor))
            events.extend(self._check_response_time(monitor))
            events.extend(self._check_cert(monitor))
        return events

    def _check_status(self, monitor: MonitorMetrics) -> list[Event]:
        """Emit monitor_down/monitor_recovered on status transitions."""
        is_up = monitor.status == 1
        was_up = self._status_states.get(monitor.name)
        self._status_states[monitor.name] = is_up

        if was_up is None:
            # First poll — only emit if down
            if not is_up:
                return [self._make_event(
                    monitor, "monitor_down", Severity.ERROR,
                    payload={"status": monitor.status},
                )]
            return []

        if was_up and not is_up:
            return [self._make_event(
                monitor, "monitor_down", Severity.ERROR,
                payload={"status": monitor.status},
            )]

        if not was_up and is_up:
            return [self._make_event(
                monitor, "monitor_recovered", Severity.INFO,
                payload={"status": monitor.status},
            )]

        return []

    def _check_response_time(self, monitor: MonitorMetrics) -> list[Event]:
        """Emit monitor_slow on threshold crossing with hysteresis."""
        if monitor.response_time_ms is None:
            return []

        threshold = self._config.response_time_threshold_ms
        was_slow = self._slow_states.get(monitor.name, False)
        is_slow = monitor.response_time_ms > threshold

        # Hysteresis: must drop below 80% of threshold to clear
        if was_slow and monitor.response_time_ms <= threshold * 0.8:
            is_slow = False
        elif was_slow:
            # Still above hysteresis band — stay in slow state
            is_slow = True

        self._slow_states[monitor.name] = is_slow

        if not was_slow and is_slow:
            return [self._make_event(
                monitor, "monitor_slow", Severity.WARNING,
                payload={
                    "response_time_ms": monitor.response_time_ms,
                    "threshold_ms": threshold,
                },
            )]

        if was_slow and not is_slow:
            return [self._make_event(
                monitor, "monitor_slow_recovered", Severity.INFO,
                payload={
                    "response_time_ms": monitor.response_time_ms,
                    "threshold_ms": threshold,
                },
            )]

        return []

    def _check_cert(self, monitor: MonitorMetrics) -> list[Event]:
        """Emit certificate_expiry on threshold transitions.

        Uses normalized dedup keys so that certificate events from both
        Uptime Kuma and the cert scanner share the same key for the same
        hostname, allowing the EventQueue to deduplicate across sources.
        """
        if monitor.cert_days_remaining is None:
            return []

        days = monitor.cert_days_remaining
        if days <= self._config.cert_critical_days:
            new_state = "critical"
        elif days <= self._config.cert_warning_days:
            new_state = "warning"
        else:
            new_state = "ok"

        old_state = self._cert_states.get(monitor.name)
        self._cert_states[monitor.name] = new_state

        if old_state == new_state:
            return []

        # Use monitor URL for normalized dedup key (cross-source correlation)
        cert_dedup_key = normalize_cert_dedup_key(monitor.url) if monitor.url else None

        # Recovery
        if new_state == "ok" and old_state in ("warning", "critical"):
            return [self._make_event(
                monitor, "certificate_renewed", Severity.INFO,
                payload={
                    "cert_days_remaining": days,
                    "previous_state": old_state,
                },
                dedup_key_override=cert_dedup_key,
            )]

        # Warning or critical
        if new_state in ("warning", "critical"):
            severity = Severity.ERROR if new_state == "critical" else Severity.WARNING
            return [self._make_event(
                monitor, "certificate_expiry", severity,
                payload={
                    "cert_days_remaining": days,
                    "state": new_state,
                    "cert_is_valid": monitor.cert_is_valid,
                },
                dedup_key_override=cert_dedup_key,
            )]

        return []

    def _make_event(
        self,
        monitor: MonitorMetrics,
        event_type: str,
        severity: Severity,
        *,
        payload: dict[str, object] | None = None,
        dedup_suffix: str = "",
        dedup_key_override: str | None = None,
    ) -> Event:
        """Build an Event from monitor data."""
        dedup_key = dedup_key_override or f"uptime_kuma:{monitor.name}{dedup_suffix}"
        return Event(
            source=self.name,
            system="uptime_kuma",
            event_type=event_type,
            entity_id=monitor.name,
            severity=severity,
            timestamp=datetime.now(tz=UTC),
            payload={
                "monitor_type": monitor.monitor_type,
                "url": monitor.url,
                **(payload or {}),
            },
            metadata=EventMetadata(
                dedup_key=dedup_key,
            ),
        )

    # -----------------------------------------------------------------
    # Topology discovery
    # -----------------------------------------------------------------

    async def discover_topology(
        self,
    ) -> tuple[list[TopologyNode], list[TopologyEdge]]:
        """Discover monitored services from Uptime Kuma."""
        from urllib.parse import urlparse

        nodes: list[TopologyNode] = []
        edges: list[TopologyEdge] = []
        source = f"auto:{self.name}"
        now = datetime.now(UTC)

        try:
            monitors = await self._client.get_metrics()
        except Exception:
            logger.debug("Uptime Kuma topology discovery failed")
            return [], []

        for monitor in monitors:
            host_ip: str | None = None
            if monitor.url:
                parsed = urlparse(monitor.url)
                host_ip = parsed.hostname

            nodes.append(TopologyNode(
                entity_id=f"uptime_kuma:{monitor.name}",
                entity_type="monitor",
                display_name=monitor.name,
                host_ip=host_ip,
                source=source,
                last_seen=now,
                metadata={
                    "url": monitor.url,
                    "monitor_type": monitor.monitor_type,
                },
            ))

        return nodes, edges

