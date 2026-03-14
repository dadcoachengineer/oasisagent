"""Overseerr polling ingestion adapter.

Polls the Overseerr API v1 for server status and pending request counts.

Events emitted on state transitions:
- ``overseerr_server_unreachable`` (ERROR) when Overseerr can't reach Plex/media servers
- ``overseerr_server_recovered`` (INFO) when connectivity is restored
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import OverseerrAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class OverseerrAdapter(IngestAdapter):
    """Polls Overseerr for server connectivity status.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: OverseerrAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State tracker
        self._server_ok: bool | None = None

    @property
    def name(self) -> str:
        return "overseerr"

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._config.api_key}

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="overseerr-poller",
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

        while not self._stopping:
            try:
                async with aiohttp.ClientSession(
                    headers=self._headers(), timeout=timeout,
                ) as session:
                    await self._poll_status(session)
                    self._connected = True
            except asyncio.CancelledError:
                return
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._connected:
                    logger.warning("Overseerr: connection error: %s", exc)
                self._connected = False
            except Exception:
                self._connected = False
                logger.exception("Overseerr: unexpected error")

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Status endpoint
    # -----------------------------------------------------------------

    async def _poll_status(self, session: aiohttp.ClientSession) -> None:
        """Poll /api/v1/status for server connectivity."""
        url = f"{self._config.url}/api/v1/status"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        version = data.get("version", "")
        is_ok = resp.status == 200
        was_ok = self._server_ok
        self._server_ok = is_ok

        if was_ok is not None and was_ok and not is_ok:
            self._enqueue(Event(
                source=self.name,
                system="overseerr",
                event_type="overseerr_server_unreachable",
                entity_id="overseerr",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={"version": version},
                metadata=EventMetadata(
                    dedup_key="overseerr:server:status",
                ),
            ))
        elif was_ok is not None and not was_ok and is_ok:
            self._enqueue(Event(
                source=self.name,
                system="overseerr",
                event_type="overseerr_server_recovered",
                entity_id="overseerr",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"version": version},
                metadata=EventMetadata(
                    dedup_key="overseerr:server:status",
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
                "Overseerr: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
