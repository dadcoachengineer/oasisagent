"""Vaultwarden (Bitwarden) health-check ingestion adapter.

Polls the Vaultwarden ``/alive`` endpoint to detect service outages.
Vaultwarden is a lightweight Bitwarden-compatible server — its ``/alive``
endpoint returns HTTP 200 when the service is healthy.

Events emitted on state transitions:
- ``vaultwarden_unreachable`` (ERROR) when the health check fails
- ``vaultwarden_recovered`` (INFO) when the service comes back online
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
    from oasisagent.config import VaultwardenAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class VaultwardenAdapter(IngestAdapter):
    """Polls Vaultwarden's ``/alive`` endpoint for service health.

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

        while not self._stopping:
            try:
                async with aiohttp.ClientSession(
                    timeout=timeout, connector=connector,
                    connector_owner=False,
                ) as session:
                    await self._poll_health(session)
                    self._connected = True
                    backoff = self._config.poll_interval  # reset on success
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

    # -----------------------------------------------------------------
    # Health check
    # -----------------------------------------------------------------

    async def _poll_health(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/alive`` — a 200 response means healthy."""
        url = f"{self._config.url.rstrip('/')}/alive"
        async with session.get(url) as resp:
            resp.raise_for_status()

        is_ok = True
        was_ok = self._service_ok
        self._service_ok = is_ok

        # Transition from down -> up
        if was_ok is not None and not was_ok and is_ok:
            self._enqueue(Event(
                source=self.name,
                system="vaultwarden",
                event_type="vaultwarden_recovered",
                entity_id="vaultwarden",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"url": self._config.url},
                metadata=EventMetadata(
                    dedup_key="vaultwarden:health",
                ),
            ))

    def _handle_failure(self, reason: str) -> None:
        """Handle a failed health check — emit event on transition."""
        was_ok = self._service_ok
        self._service_ok = False

        # Transition from up -> down, or first poll is down
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
                metadata=EventMetadata(
                    dedup_key="vaultwarden:health",
                ),
            ))

