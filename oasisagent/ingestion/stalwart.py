"""Stalwart Mail Server health-check ingestion adapter.

Polls Stalwart's ``/healthz/ready`` endpoint to detect service outages.
When an ``api_key`` is configured, also polls ``/api/queue/messages`` to
detect mail queue buildup.

Events emitted on state transitions:
- ``stalwart_unreachable`` (ERROR) when the health check fails
- ``stalwart_recovered`` (INFO) when the service comes back online
- ``stalwart_queue_high`` (WARNING) when the mail queue exceeds threshold
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
    from oasisagent.config import StalwartAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class StalwartAdapter(IngestAdapter):
    """Polls Stalwart Mail Server for health and queue status.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: StalwartAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._service_ok: bool | None = None
        self._queue_high: bool | None = None

    @property
    def name(self) -> str:
        return "stalwart"

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="stalwart-poller",
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
                    logger.exception("Stalwart: unexpected error")
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
        """Poll ``/healthz/ready`` — a 200 response means healthy."""
        base = self._config.url.rstrip("/")
        url = f"{base}/healthz/ready"
        async with session.get(url) as resp:
            resp.raise_for_status()

        was_ok = self._service_ok
        self._service_ok = True

        # Transition from down -> up
        if was_ok is not None and not was_ok:
            self._enqueue(Event(
                source=self.name,
                system="stalwart",
                event_type="stalwart_recovered",
                entity_id="stalwart",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"url": self._config.url},
                metadata=EventMetadata(
                    dedup_key="stalwart:health",
                ),
            ))

        # Queue check (requires api_key)
        if self._config.api_key:
            await self._poll_queue(session)

    async def _poll_queue(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/api/queue/messages`` for mail queue size."""
        base = self._config.url.rstrip("/")
        url = f"{base}/api/queue/messages"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}

        try:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()

            queue_size = len(data) if isinstance(data, list) else 0
            was_high = self._queue_high
            is_high = queue_size >= self._config.queue_threshold
            self._queue_high = is_high

            if is_high and (was_high is None or not was_high):
                self._enqueue(Event(
                    source=self.name,
                    system="stalwart",
                    event_type="stalwart_queue_high",
                    entity_id="stalwart",
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "url": self._config.url,
                        "queue_size": queue_size,
                        "threshold": self._config.queue_threshold,
                    },
                    metadata=EventMetadata(
                        dedup_key="stalwart:queue",
                    ),
                ))
        except (TimeoutError, aiohttp.ClientError):
            logger.warning("Stalwart: failed to poll queue endpoint")

    def _handle_failure(self, reason: str) -> None:
        """Handle a failed health check — emit event on transition."""
        was_ok = self._service_ok
        self._service_ok = False
        self._queue_high = None

        if was_ok is None or was_ok:
            if was_ok is not None:
                logger.warning("Stalwart: health check failed: %s", reason)
            self._enqueue(Event(
                source=self.name,
                system="stalwart",
                event_type="stalwart_unreachable",
                entity_id="stalwart",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "url": self._config.url,
                    "reason": reason,
                },
                metadata=EventMetadata(
                    dedup_key="stalwart:health",
                ),
            ))

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "Stalwart: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )

