"""Nextcloud server health-check ingestion adapter.

Polls Nextcloud's server info API (OCS) to detect service outages,
stale cron jobs, and maintenance mode.

Events emitted on state transitions:
- ``nextcloud_unreachable`` (ERROR) when the API is unreachable
- ``nextcloud_recovered`` (INFO) when the service comes back online
- ``nextcloud_cron_stale`` (WARNING) when the last cron run is too old
- ``nextcloud_maintenance_mode`` (WARNING) when maintenance mode is active
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import NextcloudAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Cron is considered stale if last run was more than 1 hour ago
_CRON_STALE_SECONDS = 3600


class NextcloudAdapter(IngestAdapter):
    """Polls Nextcloud's OCS server info API for health status.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: NextcloudAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._service_ok: bool | None = None
        self._cron_stale: bool | None = None
        self._maintenance: bool | None = None

    @property
    def name(self) -> str:
        return "nextcloud"

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="nextcloud-poller",
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
                    logger.exception("Nextcloud: unexpected error")
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
        """Poll Nextcloud server info API."""
        base = self._config.url.rstrip("/")
        url = f"{base}/ocs/v2.php/apps/serverinfo/api/v1/info?format=json"
        headers = {"OCS-APIREQUEST": "true"}
        auth = aiohttp.BasicAuth(self._config.username, self._config.password)

        async with session.get(url, headers=headers, auth=auth) as resp:
            resp.raise_for_status()
            data = await resp.json()

        was_ok = self._service_ok
        self._service_ok = True

        if was_ok is not None and not was_ok:
            self._enqueue(Event(
                source=self.name,
                system="nextcloud",
                event_type="nextcloud_recovered",
                entity_id="nextcloud",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"url": self._config.url},
                metadata=EventMetadata(dedup_key="nextcloud:health"),
            ))

        # Extract server info
        ocs_data = data.get("ocs", {}).get("data", {})
        nc_system = ocs_data.get("nextcloud", {}).get("system", {})

        # Check maintenance mode
        self._check_maintenance(nc_system)

        # Check cron freshness
        last_cron_ts = nc_system.get("last_cron", 0)
        if last_cron_ts:
            self._check_cron(last_cron_ts)

    def _check_maintenance(self, nc_system: dict) -> None:
        """Check if maintenance mode is active."""
        is_maintenance = nc_system.get("maintenance", False)
        was_maintenance = self._maintenance
        self._maintenance = is_maintenance

        if is_maintenance and (was_maintenance is None or not was_maintenance):
            self._enqueue(Event(
                source=self.name,
                system="nextcloud",
                event_type="nextcloud_maintenance_mode",
                entity_id="nextcloud",
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={"url": self._config.url},
                metadata=EventMetadata(dedup_key="nextcloud:maintenance"),
            ))

    def _check_cron(self, last_cron_ts: int) -> None:
        """Check if cron has run recently."""
        now = int(datetime.now(tz=UTC).timestamp())
        age = now - last_cron_ts
        is_stale = age > _CRON_STALE_SECONDS
        was_stale = self._cron_stale
        self._cron_stale = is_stale

        if is_stale and (was_stale is None or not was_stale):
            self._enqueue(Event(
                source=self.name,
                system="nextcloud",
                event_type="nextcloud_cron_stale",
                entity_id="nextcloud",
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "url": self._config.url,
                    "last_cron_ts": last_cron_ts,
                    "age_seconds": age,
                },
                metadata=EventMetadata(dedup_key="nextcloud:cron"),
            ))

    def _handle_failure(self, reason: str) -> None:
        """Handle a failed health check — emit event on transition."""
        was_ok = self._service_ok
        self._service_ok = False
        self._cron_stale = None
        self._maintenance = None

        if was_ok is None or was_ok:
            if was_ok is not None:
                logger.warning("Nextcloud: health check failed: %s", reason)
            self._enqueue(Event(
                source=self.name,
                system="nextcloud",
                event_type="nextcloud_unreachable",
                entity_id="nextcloud",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "url": self._config.url,
                    "reason": reason,
                },
                metadata=EventMetadata(dedup_key="nextcloud:health"),
            ))

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "Nextcloud: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
