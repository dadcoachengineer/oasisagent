"""Plex Media Server polling ingestion adapter.

Polls the Plex HTTP API for library scan status and server reachability.

Events emitted on state transitions:
- ``plex_server_unreachable`` (ERROR) when Plex stops responding
- ``plex_server_recovered`` (INFO) when Plex starts responding again
- ``plex_library_scan_failed`` (WARNING) when a library section reports scan issues
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
    from oasisagent.config import PlexAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class PlexAdapter(IngestAdapter):
    """Polls Plex Media Server for library status and server health.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: PlexAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._server_reachable: bool | None = None
        self._library_errors: set[str] = set()  # section keys with issues

    @property
    def name(self) -> str:
        return "plex"

    def _headers(self) -> dict[str, str]:
        return {
            "X-Plex-Token": self._config.token,
            "Accept": "application/json",
        }

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="plex-poller",
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
                    reachable = await self._check_server(session)
                    if reachable:
                        await self._poll_libraries(session)
                    self._connected = reachable
            except asyncio.CancelledError:
                return
            except (TimeoutError, aiohttp.ClientError) as exc:
                self._handle_unreachable(str(exc))
                self._connected = False
            except Exception:
                self._connected = False
                logger.exception("Plex: unexpected error")

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Server reachability
    # -----------------------------------------------------------------

    async def _check_server(self, session: aiohttp.ClientSession) -> bool:
        """Check if Plex server is reachable via /identity endpoint."""
        url = f"{self._config.url}/identity"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    self._handle_reachable()
                    return True
                self._handle_unreachable(f"HTTP {resp.status}")
                return False
        except (TimeoutError, aiohttp.ClientError) as exc:
            self._handle_unreachable(str(exc))
            return False

    def _handle_unreachable(self, error: str) -> None:
        """Emit server_unreachable on transition from reachable to unreachable."""
        was_reachable = self._server_reachable
        self._server_reachable = False

        if was_reachable is None or was_reachable:
            self._enqueue(Event(
                source=self.name,
                system="plex",
                event_type="plex_server_unreachable",
                entity_id="plex",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={"error": error},
                metadata=EventMetadata(
                    dedup_key="plex:server:reachable",
                ),
            ))

    def _handle_reachable(self) -> None:
        """Emit server_recovered on transition from unreachable to reachable."""
        was_reachable = self._server_reachable
        self._server_reachable = True

        if was_reachable is not None and not was_reachable:
            self._enqueue(Event(
                source=self.name,
                system="plex",
                event_type="plex_server_recovered",
                entity_id="plex",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={},
                metadata=EventMetadata(
                    dedup_key="plex:server:reachable",
                ),
            ))

    # -----------------------------------------------------------------
    # Library sections
    # -----------------------------------------------------------------

    async def _poll_libraries(self, session: aiohttp.ClientSession) -> None:
        """Poll /library/sections for library scan issues."""
        url = f"{self._config.url}/library/sections"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        container = data.get("MediaContainer", {})
        sections: list[dict[str, Any]] = container.get("Directory", [])

        current_errors: set[str] = set()
        for section in sections:
            key = section.get("key", "")
            title = section.get("title", key)

            # Check for refresh errors - Plex uses "refreshing" status
            # and we can detect scan failures by checking for error states
            scanning = section.get("refreshing", False)
            if scanning:
                continue  # Actively scanning, not an error

            # Check for sections with content but reported as empty
            # (indicates a scan failure or mount issue)
            content_count = section.get("count", -1)
            if content_count == 0:
                current_errors.add(key)
                if key not in self._library_errors:
                    self._enqueue(Event(
                        source=self.name,
                        system="plex",
                        event_type="plex_library_scan_failed",
                        entity_id=title,
                        severity=Severity.WARNING,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "section_key": key,
                            "title": title,
                            "type": section.get("type", ""),
                            "content_count": content_count,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"plex:library:{key}:scan",
                        ),
                    ))

        self._library_errors = current_errors

