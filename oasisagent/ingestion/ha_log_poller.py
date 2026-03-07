"""Home Assistant log poller ingestion adapter.

Polls HA's error log endpoint, pattern-matches log entries against
configured regexes, and transforms matches into canonical Event objects.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import HaLogPollerConfig, LogPattern
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

_SEVERITY_MAP: dict[str, Severity] = {
    "info": Severity.INFO,
    "warning": Severity.WARNING,
    "error": Severity.ERROR,
    "critical": Severity.CRITICAL,
}


class HaLogPollerAdapter(IngestAdapter):
    """Ingestion adapter that polls Home Assistant's error log.

    Periodically fetches HA's error log via the REST API, matches entries
    against configured regex patterns, and emits Events for new matches.

    Deduplicates internally based on error fingerprint within a configurable
    window (separate from the queue-level dedup).
    """

    def __init__(self, config: HaLogPollerConfig, queue: EventQueue) -> None:
        super().__init__(queue)
        self._config = config
        self._connected = False
        self._stopping = False
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
        """Poll HA error log on the configured interval."""
        while not self._stopping:
            try:
                await self._poll()
                self._connected = True
            except aiohttp.ClientResponseError as exc:
                self._connected = False
                if exc.status in (401, 403):
                    logger.error(
                        "HA log poller: authentication failed (HTTP %d). "
                        "Check HA_TOKEN in your .env file.",
                        exc.status,
                    )
                    return
                logger.warning(
                    "HA log poller: HTTP error %d, will retry next interval",
                    exc.status,
                )
            except aiohttp.ClientError as exc:
                self._connected = False
                logger.warning(
                    "HA log poller: connection error (%s), will retry next interval",
                    exc,
                )
            except Exception:
                self._connected = False
                logger.exception("Unexpected error in HA log poller")

            # Wait for next poll interval (or until stopped)
            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stopping = True
        self._connected = False

    async def healthy(self) -> bool:
        return self._connected

    async def _poll(self) -> None:
        """Fetch the error log and process entries."""
        url = f"{self._config.url.rstrip('/')}/api/error/all"
        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "Content-Type": "application/json",
        }

        async with (
            aiohttp.ClientSession() as session,
            session.get(url, headers=headers) as response,
        ):
            response.raise_for_status()
            log_text = await response.text()

        self._prune_seen()
        self._process_log(log_text)

    def _process_log(self, log_text: str) -> None:
        """Match log lines against patterns and emit events."""
        for line in log_text.splitlines():
            line = line.strip()
            if not line:
                continue

            for compiled, pattern in self._compiled_patterns:
                match = compiled.search(line)
                if not match:
                    continue

                fingerprint = self._fingerprint(line, pattern)
                if self._is_seen(fingerprint):
                    continue

                self._mark_seen(fingerprint)
                entity_id = match.group(1) if match.lastindex and match.lastindex >= 1 else ""
                severity = _SEVERITY_MAP.get(pattern.severity, Severity.WARNING)

                event = Event(
                    source=self.name,
                    system="homeassistant",
                    event_type=pattern.event_type,
                    entity_id=entity_id,
                    severity=severity,
                    timestamp=datetime.now(UTC),
                    payload={
                        "log_line": line,
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
                        "HA log poller: failed to enqueue event for line: %s",
                        line[:100],
                    )

                # First matching pattern wins per line
                break

    @staticmethod
    def _fingerprint(line: str, pattern: LogPattern) -> str:
        """Generate a dedup fingerprint from a log line and its pattern."""
        content = f"{pattern.event_type}:{line}"
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
