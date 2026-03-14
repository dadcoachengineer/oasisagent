"""qBittorrent polling ingestion adapter.

Polls the qBittorrent Web API v2 for errored and stalled torrents,
and monitors connection status.

Events emitted on state transitions:
- ``qbt_torrent_error`` (WARNING) when a torrent enters an error state
- ``qbt_torrent_stalled`` (INFO) when a torrent is stalled downloading
- ``qbt_connection_lost`` (ERROR) when qBittorrent reports no connectivity
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
    from oasisagent.config import QBittorrentAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class QBittorrentAdapter(IngestAdapter):
    """Polls qBittorrent for torrent errors, stalls, and connectivity issues.

    Authenticates via cookie-based session. Re-authenticates on 403/401.
    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: QBittorrentAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._session: aiohttp.ClientSession | None = None

        # State trackers for dedup
        self._errored_hashes: set[str] = set()
        self._stalled_hashes: set[str] = set()
        self._connection_ok: bool | None = None  # None = unknown

    @property
    def name(self) -> str:
        return "qbittorrent"

    async def start(self) -> None:
        """Start the polling loop."""
        self._task = asyncio.create_task(
            self._poll_loop(), name="qbittorrent-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return an authenticated session, creating/refreshing as needed."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._config.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)

        await self._login()
        return self._session

    async def _login(self) -> None:
        """Authenticate with qBittorrent Web API."""
        if self._session is None:
            return

        url = f"{self._config.url}/api/v2/auth/login"
        data = {
            "username": self._config.username,
            "password": self._config.password,
        }
        async with self._session.post(url, data=data) as resp:
            body = await resp.text()
            if resp.status != 200 or body.strip().upper() != "OK.":
                msg = f"qBittorrent auth failed: {resp.status} {body}"
                raise aiohttp.ClientError(msg)

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        backoff = self._config.poll_interval
        max_backoff = 300
        while not self._stopping:
            try:
                session = await self._ensure_session()
                await self._poll_transfer_info(session)
                await self._poll_errored_torrents(session)
                await self._poll_stalled_torrents(session)
                self._connected = True
                backoff = self._config.poll_interval  # reset on success
            except asyncio.CancelledError:
                return
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._connected:
                    logger.warning("qBittorrent: connection error: %s", exc)
                self._connected = False
                # Force re-auth next cycle
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                backoff = min(backoff * 2, max_backoff)
            except Exception:
                self._connected = False
                logger.exception("qBittorrent: unexpected error")
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                backoff = min(backoff * 2, max_backoff)

            wait = self._config.poll_interval if self._connected else backoff
            for _ in range(wait):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Transfer info (connectivity)
    # -----------------------------------------------------------------

    async def _poll_transfer_info(self, session: aiohttp.ClientSession) -> None:
        """Poll /api/v2/transfer/info for connection status."""
        url = f"{self._config.url}/api/v2/transfer/info"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        status = data.get("connection_status", "connected")
        is_connected = status != "disconnected"
        was_connected = self._connection_ok

        self._connection_ok = is_connected

        if was_connected is not None and was_connected and not is_connected:
            self._enqueue(Event(
                source=self.name,
                system="qbittorrent",
                event_type="qbt_connection_lost",
                entity_id="qbittorrent",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "connection_status": status,
                    "dl_speed": data.get("dl_info_speed", 0),
                    "up_speed": data.get("up_info_speed", 0),
                },
                metadata=EventMetadata(
                    dedup_key="qbittorrent:connection",
                ),
            ))
        elif was_connected is not None and not was_connected and is_connected:
            self._enqueue(Event(
                source=self.name,
                system="qbittorrent",
                event_type="qbt_connection_recovered",
                entity_id="qbittorrent",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"connection_status": status},
                metadata=EventMetadata(
                    dedup_key="qbittorrent:connection",
                ),
            ))
        elif was_connected is None and not is_connected:
            self._enqueue(Event(
                source=self.name,
                system="qbittorrent",
                event_type="qbt_connection_lost",
                entity_id="qbittorrent",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={"connection_status": status},
                metadata=EventMetadata(
                    dedup_key="qbittorrent:connection",
                ),
            ))

    # -----------------------------------------------------------------
    # Errored torrents
    # -----------------------------------------------------------------

    async def _poll_errored_torrents(self, session: aiohttp.ClientSession) -> None:
        """Poll for torrents in error state."""
        url = f"{self._config.url}/api/v2/torrents/info"
        params = {"filter": "errored"}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            torrents: list[dict[str, Any]] = await resp.json(content_type=None)

        current_errored: set[str] = set()
        for torrent in torrents:
            torrent_hash = torrent.get("hash", "")
            if not torrent_hash:
                continue

            current_errored.add(torrent_hash)
            if torrent_hash not in self._errored_hashes:
                self._enqueue(Event(
                    source=self.name,
                    system="qbittorrent",
                    event_type="qbt_torrent_error",
                    entity_id=torrent.get("name", torrent_hash),
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "hash": torrent_hash,
                        "name": torrent.get("name", ""),
                        "state": torrent.get("state", ""),
                        "size": torrent.get("size", 0),
                        "progress": torrent.get("progress", 0),
                        "category": torrent.get("category", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"qbittorrent:error:{torrent_hash}",
                    ),
                ))

        self._errored_hashes = current_errored

    # -----------------------------------------------------------------
    # Stalled torrents
    # -----------------------------------------------------------------

    async def _poll_stalled_torrents(self, session: aiohttp.ClientSession) -> None:
        """Poll for stalled downloading torrents."""
        url = f"{self._config.url}/api/v2/torrents/info"
        params = {"filter": "stalled_downloading"}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            torrents: list[dict[str, Any]] = await resp.json(content_type=None)

        current_stalled: set[str] = set()
        for torrent in torrents:
            torrent_hash = torrent.get("hash", "")
            if not torrent_hash:
                continue

            current_stalled.add(torrent_hash)
            if torrent_hash not in self._stalled_hashes:
                self._enqueue(Event(
                    source=self.name,
                    system="qbittorrent",
                    event_type="qbt_torrent_stalled",
                    entity_id=torrent.get("name", torrent_hash),
                    severity=Severity.INFO,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "hash": torrent_hash,
                        "name": torrent.get("name", ""),
                        "state": torrent.get("state", ""),
                        "size": torrent.get("size", 0),
                        "progress": torrent.get("progress", 0),
                        "num_seeds": torrent.get("num_seeds", 0),
                        "num_leechs": torrent.get("num_leechs", 0),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"qbittorrent:stalled:{torrent_hash}",
                    ),
                ))

        self._stalled_hashes = current_stalled

