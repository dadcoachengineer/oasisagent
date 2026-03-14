"""Tautulli polling ingestion adapter.

Polls the Tautulli API v2 for Plex server status and bandwidth monitoring.
Tautulli provides richer monitoring data than the Plex API directly.

Events emitted on state transitions:
- ``tautulli_plex_down`` (ERROR) when Tautulli reports Plex is down
- ``tautulli_plex_recovered`` (INFO) when Plex comes back up
- ``tautulli_high_bandwidth`` (WARNING) when total bandwidth exceeds threshold
- ``tautulli_bandwidth_recovered`` (INFO) when bandwidth drops below threshold
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
    from oasisagent.config import TautulliAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Default bandwidth threshold in kbps (100 Mbps)
_DEFAULT_BANDWIDTH_THRESHOLD_KBPS = 100_000


class TautulliAdapter(IngestAdapter):
    """Polls Tautulli for Plex server status and bandwidth metrics.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: TautulliAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._plex_connected: bool | None = None
        self._bandwidth_high: bool = False

    @property
    def name(self) -> str:
        return "tautulli"

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="tautulli-poller",
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
        backoff = self._config.poll_interval
        max_backoff = 300

        while not self._stopping:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    await self._poll_server_status(session)
                    await self._poll_activity(session)
                    self._connected = True
                    backoff = self._config.poll_interval  # reset on success
            except asyncio.CancelledError:
                return
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._connected:
                    logger.warning("Tautulli: connection error: %s", exc)
                self._connected = False
                backoff = min(backoff * 2, max_backoff)
            except Exception:
                self._connected = False
                logger.exception("Tautulli: unexpected error")
                backoff = min(backoff * 2, max_backoff)

            wait = self._config.poll_interval if self._connected else backoff
            for _ in range(wait):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Server status
    # -----------------------------------------------------------------

    async def _poll_server_status(self, session: aiohttp.ClientSession) -> None:
        """Poll Tautulli's server_status command to check Plex connectivity."""
        url = self._api_url("server_status")
        async with session.get(url) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        response = data.get("response", {})
        result = response.get("result", "error")
        status_data = response.get("data", {})

        if result != "success":
            return

        is_connected = status_data.get("connected", False)
        was_connected = self._plex_connected
        self._plex_connected = is_connected

        if was_connected is not None and was_connected and not is_connected:
            self._enqueue(Event(
                source=self.name,
                system="tautulli",
                event_type="tautulli_plex_down",
                entity_id="plex",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "plex_version": status_data.get("pms_version", ""),
                    "plex_platform": status_data.get("pms_platform", ""),
                },
                metadata=EventMetadata(
                    dedup_key="tautulli:plex:status",
                ),
            ))
        elif was_connected is not None and not was_connected and is_connected:
            self._enqueue(Event(
                source=self.name,
                system="tautulli",
                event_type="tautulli_plex_recovered",
                entity_id="plex",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "plex_version": status_data.get("pms_version", ""),
                },
                metadata=EventMetadata(
                    dedup_key="tautulli:plex:status",
                ),
            ))
        elif was_connected is None and not is_connected:
            # First poll, already down
            self._enqueue(Event(
                source=self.name,
                system="tautulli",
                event_type="tautulli_plex_down",
                entity_id="plex",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "plex_version": status_data.get("pms_version", ""),
                    "plex_platform": status_data.get("pms_platform", ""),
                },
                metadata=EventMetadata(
                    dedup_key="tautulli:plex:status",
                ),
            ))

    # -----------------------------------------------------------------
    # Activity / bandwidth
    # -----------------------------------------------------------------

    async def _poll_activity(self, session: aiohttp.ClientSession) -> None:
        """Poll Tautulli's get_activity command for bandwidth monitoring."""
        url = self._api_url("get_activity")
        async with session.get(url) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        response = data.get("response", {})
        result = response.get("result", "error")
        activity = response.get("data", {})

        if result != "success":
            return

        total_bandwidth = activity.get("total_bandwidth", 0)
        stream_count = activity.get("stream_count", 0)
        threshold = self._config.bandwidth_threshold_kbps

        was_high = self._bandwidth_high
        is_high = total_bandwidth > threshold

        self._bandwidth_high = is_high

        if not was_high and is_high:
            self._enqueue(Event(
                source=self.name,
                system="tautulli",
                event_type="tautulli_high_bandwidth",
                entity_id="plex",
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "total_bandwidth_kbps": total_bandwidth,
                    "threshold_kbps": threshold,
                    "stream_count": stream_count,
                    "stream_count_direct_play": activity.get("stream_count_direct_play", 0),
                    "stream_count_transcode": activity.get("stream_count_transcode", 0),
                },
                metadata=EventMetadata(
                    dedup_key="tautulli:bandwidth",
                ),
            ))
        elif was_high and not is_high:
            self._enqueue(Event(
                source=self.name,
                system="tautulli",
                event_type="tautulli_bandwidth_recovered",
                entity_id="plex",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "total_bandwidth_kbps": total_bandwidth,
                    "stream_count": stream_count,
                },
                metadata=EventMetadata(
                    dedup_key="tautulli:bandwidth",
                ),
            ))

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _api_url(self, cmd: str) -> str:
        """Build Tautulli API URL with command and API key."""
        return f"{self._config.url}/api/v2?apikey={self._config.api_key}&cmd={cmd}"

