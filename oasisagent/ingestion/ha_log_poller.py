"""Home Assistant system log poller ingestion adapter.

Connects to HA's WebSocket API and periodically sends the
``system_log/list`` command to fetch structured log entries.
Matches entries against configured regex patterns and transforms
matches into canonical Event objects.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.backoff import ExponentialBackoff
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import HaLogPollerConfig, LogPattern
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# HA system_log/list level → Severity mapping
_LEVEL_MAP: dict[str, Severity] = {
    "CRITICAL": Severity.CRITICAL,
    "ERROR": Severity.ERROR,
    "WARNING": Severity.WARNING,
    "INFO": Severity.INFO,
    "DEBUG": Severity.INFO,
}


class HaLogPollerAdapter(IngestAdapter):
    """Ingestion adapter that polls Home Assistant's system log.

    Connects to HA via WebSocket, authenticates, and periodically sends
    ``system_log/list`` to retrieve structured log entries. Matches entries
    against configured regex patterns and emits Events for new matches.

    Deduplicates internally based on log entry fingerprint within a
    configurable window (separate from the queue-level dedup).
    """

    def __init__(self, config: HaLogPollerConfig, queue: EventQueue) -> None:
        super().__init__(queue)
        self._config = config
        self._connected = False
        self._stopping = False
        self._backoff = ExponentialBackoff(name="ha_log_poller")
        self._msg_id = 0
        self._seen: dict[str, float] = {}  # fingerprint -> timestamp
        self._compiled_patterns: list[tuple[re.Pattern[str], LogPattern]] = []

        for pattern in config.patterns:
            try:
                compiled = re.compile(pattern.regex)
                self._compiled_patterns.append((compiled, pattern))
            except re.error as exc:
                logger.error(
                    "HA log poller: invalid regex pattern '%s': %s",
                    pattern.regex,
                    exc,
                )

    @property
    def name(self) -> str:
        return "ha_log_poller"

    async def start(self) -> None:
        """Connect to HA WebSocket and poll system log on an interval."""
        while not self._stopping:
            try:
                await self._connect_and_poll()
            except aiohttp.WSServerHandshakeError as exc:
                if self._stopping:
                    break
                logger.error(
                    "HA log poller: WebSocket handshake failed (status=%s)",
                    exc.status,
                )
                self._connected = False
                await self._backoff.wait()
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._stopping:
                    break
                logger.warning(
                    "HA log poller: connection error (%s), reconnecting",
                    exc,
                )
                self._connected = False
                await self._backoff.wait()
            except _AuthError:
                logger.error(
                    "HA log poller: authentication failed. "
                    "Check HA_TOKEN in your .env file.",
                )
                self._stopping = True
                self._connected = False
                return
            except Exception:
                if self._stopping:
                    break
                logger.exception("Unexpected error in HA log poller")
                self._connected = False
                await self._backoff.wait()

    async def stop(self) -> None:
        self._stopping = True
        self._connected = False

    async def healthy(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # WebSocket connection + polling loop
    # ------------------------------------------------------------------

    def _ws_url(self) -> str:
        """Derive WebSocket URL from the configured HTTP URL."""
        url = self._config.url.rstrip("/")
        if url.startswith("https://"):
            return url.replace("https://", "wss://", 1) + "/api/websocket"
        if url.startswith("http://"):
            return url.replace("http://", "ws://", 1) + "/api/websocket"
        # Already a ws(s) URL
        if not url.endswith("/api/websocket"):
            return url + "/api/websocket"
        return url

    async def _connect_and_poll(self) -> None:
        """Connect, authenticate, then poll in a loop."""
        ws_url = self._ws_url()
        async with aiohttp.ClientSession() as session, session.ws_connect(ws_url) as ws:
            await self._authenticate(ws)
            self._connected = True
            self._backoff.reset()
            logger.info("HA log poller connected via WebSocket")

            self._msg_id = 0
            while not self._stopping:
                try:
                    entries = await self._fetch_system_log(ws)
                    self._prune_seen()
                    self._process_entries(entries)
                except _AuthError:
                    raise
                except Exception:
                    logger.exception("HA log poller: error fetching system log")

                for _ in range(self._config.poll_interval):
                    if self._stopping:
                        return
                    await asyncio.sleep(1)

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Perform HA WebSocket authentication handshake."""
        auth_required = await ws.receive_json()
        if auth_required.get("type") != "auth_required":
            msg = f"Expected auth_required, got {auth_required.get('type')}"
            raise ConnectionError(msg)

        await ws.send_json({
            "type": "auth",
            "access_token": self._config.token,
        })

        auth_result = await ws.receive_json()
        if auth_result.get("type") == "auth_invalid":
            raise _AuthError(auth_result.get("message", "invalid token"))
        if auth_result.get("type") != "auth_ok":
            msg = f"Expected auth_ok, got {auth_result.get('type')}"
            raise ConnectionError(msg)

    async def _fetch_system_log(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> list[dict[str, Any]]:
        """Send system_log/list and return the structured entries."""
        self._msg_id += 1
        msg_id = self._msg_id
        await ws.send_json({
            "id": msg_id,
            "type": "system_log/list",
        })

        # Read messages until we get the response for our ID
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("id") == msg_id:
                    if not data.get("success", False):
                        logger.warning(
                            "HA log poller: system_log/list failed: %s",
                            data.get("error", {}).get("message", "unknown"),
                        )
                        return []
                    return data.get("result", [])
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                self._connected = False
                return []

        return []

    # ------------------------------------------------------------------
    # Entry processing (structured JSON from system_log/list)
    # ------------------------------------------------------------------

    def _process_entries(self, entries: list[dict[str, Any]]) -> None:
        """Match structured log entries against patterns and emit events."""
        for entry in entries:
            name = entry.get("name", "")
            # message can be a list of strings or a single string
            raw_message = entry.get("message", "")
            if isinstance(raw_message, list):
                message = " ".join(str(m) for m in raw_message)
            else:
                message = str(raw_message)

            # Build a matchable line: "component: message"
            match_text = f"{name}: {message}" if name else message

            for compiled, pattern in self._compiled_patterns:
                match = compiled.search(match_text)
                if not match:
                    continue

                fingerprint = self._fingerprint(entry, pattern)
                if self._is_seen(fingerprint):
                    continue

                self._mark_seen(fingerprint)
                entity_id = (
                    match.group(1) if match.lastindex and match.lastindex >= 1 else ""
                )

                # Use HA's log level directly, fall back to pattern severity
                ha_level = entry.get("level", "").upper()
                severity = _LEVEL_MAP.get(ha_level, Severity.WARNING)

                source_info = entry.get("source", [])
                timestamp_unix = entry.get("timestamp")
                ts = (
                    datetime.fromtimestamp(timestamp_unix, tz=UTC)
                    if timestamp_unix
                    else datetime.now(UTC)
                )

                event = Event(
                    source=self.name,
                    system="homeassistant",
                    event_type=pattern.event_type,
                    entity_id=entity_id,
                    severity=severity,
                    timestamp=ts,
                    payload={
                        "component": name,
                        "message": message,
                        "source": source_info,
                        "count": entry.get("count", 1),
                        "first_occurred": entry.get("first_occurred"),
                        "matched_pattern": pattern.regex,
                        "match_groups": list(match.groups()),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"ha_log:{entity_id}:{pattern.event_type}",
                    ),
                )

                try:
                    self._queue.put_nowait(event)
                except Exception:
                    logger.warning(
                        "HA log poller: failed to enqueue event: %s",
                        match_text[:100],
                    )

                # First matching pattern wins per entry
                break

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(entry: dict[str, Any], pattern: LogPattern) -> str:
        """Generate a dedup fingerprint from a log entry and its pattern."""
        name = entry.get("name", "")
        raw_message = entry.get("message", "")
        if isinstance(raw_message, list):
            message = " ".join(str(m) for m in raw_message)
        else:
            message = str(raw_message)
        content = f"{pattern.event_type}:{name}:{message}"
        return hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()

    def _is_seen(self, fingerprint: str) -> bool:
        """Check if this fingerprint was seen within the dedup window."""
        seen_at = self._seen.get(fingerprint)
        if seen_at is None:
            return False
        return (time.monotonic() - seen_at) < self._config.dedup_window

    def _mark_seen(self, fingerprint: str) -> None:
        """Record a fingerprint with the current timestamp."""
        self._seen[fingerprint] = time.monotonic()

    def _prune_seen(self) -> None:
        """Remove expired entries from the seen cache."""
        now = time.monotonic()
        cutoff = now - self._config.dedup_window
        expired = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in expired:
            del self._seen[k]


class _AuthError(Exception):
    """Raised when HA WebSocket authentication fails."""
