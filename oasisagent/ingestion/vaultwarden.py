"""Vaultwarden (Bitwarden) health-check ingestion adapter.

Polls the Vaultwarden ``/alive`` endpoint to detect service outages.
When ``deep_health`` is enabled, additionally polls ``/api/config``
to detect partial degradation and tracks response time.

Events emitted on state transitions:
- ``vaultwarden_unreachable`` (ERROR) when the health check fails
- ``vaultwarden_recovered`` (INFO) when the service comes back online
- ``vaultwarden_degraded`` (WARNING) when /alive OK but /api/config fails
- ``vaultwarden_slow`` (WARNING) when response time exceeds threshold
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity, TopologyEdge, TopologyNode

if TYPE_CHECKING:
    from oasisagent.config import VaultwardenAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class VaultwardenAdapter(IngestAdapter):
    """Polls Vaultwarden's ``/alive`` endpoint for service health.

    When ``deep_health`` is enabled, also polls ``/api/config`` for
    degraded detection and response time tracking.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: VaultwardenAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State tracker: None = first poll, True = healthy, False = down
        self._service_ok: bool | None = None
        # Deep health state
        self._api_ok: bool | None = None
        self._is_slow: bool | None = None

    @property
    def name(self) -> str:
        return "vaultwarden"

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="vaultwarden-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self._config.timeout)
        connector = aiohttp.TCPConnector(ssl=self._config.verify_ssl)
        backoff = self._config.poll_interval
        max_backoff = 300

        try:
            while not self._stopping:
                try:
                    async with aiohttp.ClientSession(
                        timeout=timeout, connector=connector,
                        connector_owner=False,
                    ) as session:
                        await self._poll_health(session)
                        self._connected = True
                        backoff = self._config.poll_interval
                except asyncio.CancelledError:
                    return
                except (TimeoutError, aiohttp.ClientError) as exc:
                    self._connected = False
                    self._handle_failure(str(exc))
                    backoff = min(backoff * 2, max_backoff)
                except Exception:
                    self._connected = False
                    logger.exception("Vaultwarden: unexpected error")
                    self._handle_failure("unexpected error")
                    backoff = min(backoff * 2, max_backoff)

                wait = self._config.poll_interval if self._connected else backoff
                for _ in range(wait):
                    if self._stopping:
                        return
                    await asyncio.sleep(1)
        finally:
            await connector.close()

    # -----------------------------------------------------------------
    # Health check
    # -----------------------------------------------------------------

    async def _poll_health(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/alive`` — a 200 response means healthy."""
        url = f"{self._config.url.rstrip('/')}/alive"
        async with session.get(url) as resp:
            resp.raise_for_status()

        was_ok = self._service_ok
        self._service_ok = True

        if was_ok is not None and not was_ok:
            self._enqueue(Event(
                source=self.name,
                system="vaultwarden",
                event_type="vaultwarden_recovered",
                entity_id="vaultwarden",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"url": self._config.url},
                metadata=EventMetadata(dedup_key="vaultwarden:health"),
            ))

        if self._config.deep_health:
            await self._poll_deep_health(session)

    # -----------------------------------------------------------------
    # Deep health checks
    # -----------------------------------------------------------------

    async def _poll_deep_health(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/api/config`` for degraded detection and response time."""
        base = self._config.url.rstrip("/")
        api_url = f"{base}/api/config"

        was_api_ok = self._api_ok
        was_slow = self._is_slow

        try:
            t0 = time.monotonic()
            async with session.get(api_url) as resp:
                resp.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000

            self._api_ok = True

            if was_api_ok is not None and not was_api_ok:
                logger.info("Vaultwarden: /api/config recovered")

            is_slow = elapsed_ms > self._config.slow_threshold_ms
            self._is_slow = is_slow

            if is_slow and (was_slow is None or not was_slow):
                self._enqueue(Event(
                    source=self.name,
                    system="vaultwarden",
                    event_type="vaultwarden_slow",
                    entity_id="vaultwarden",
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "url": self._config.url,
                        "response_time_ms": round(elapsed_ms, 1),
                        "threshold_ms": self._config.slow_threshold_ms,
                    },
                    metadata=EventMetadata(dedup_key="vaultwarden:slow"),
                ))

        except (TimeoutError, aiohttp.ClientError, Exception) as exc:
            self._api_ok = False
            self._is_slow = None

            if was_api_ok is None or was_api_ok:
                logger.warning(
                    "Vaultwarden: /api/config failed (degraded): %s", exc,
                )
                self._enqueue(Event(
                    source=self.name,
                    system="vaultwarden",
                    event_type="vaultwarden_degraded",
                    entity_id="vaultwarden",
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "url": self._config.url,
                        "reason": str(exc),
                    },
                    metadata=EventMetadata(
                        dedup_key="vaultwarden:degraded",
                    ),
                ))

    def _handle_failure(self, reason: str) -> None:
        """Handle a failed health check — emit event on transition."""
        was_ok = self._service_ok
        self._service_ok = False

        if self._config.deep_health:
            self._api_ok = None
            self._is_slow = None

        if was_ok is None or was_ok:
            if was_ok is not None:
                logger.warning("Vaultwarden: health check failed: %s", reason)
            self._enqueue(Event(
                source=self.name,
                system="vaultwarden",
                event_type="vaultwarden_unreachable",
                entity_id="vaultwarden",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "url": self._config.url,
                    "reason": reason,
                },
                metadata=EventMetadata(dedup_key="vaultwarden:health"),
            ))

    # -----------------------------------------------------------------
    # Topology discovery
    # -----------------------------------------------------------------

    async def discover_topology(
        self,
    ) -> tuple[list[TopologyNode], list[TopologyEdge]]:
        """Discover this service as a topology node."""
        from urllib.parse import urlparse

        source = f"auto:{self.name}"
        parsed = urlparse(self._config.url)
        nodes = [
            TopologyNode(
                entity_id=f"{self.name}:{self.name}",
                entity_type="service",
                display_name="Vaultwarden",
                host_ip=parsed.hostname,
                source=source,
                last_seen=datetime.now(UTC),
                metadata={"url": self._config.url},
            ),
        ]
        return nodes, []
