"""Home Assistant integration health scanner.

Proactively checks the health of HA config entries (integrations) by
calling the HA REST API. Detects integrations in error states:
``setup_error``, ``config_entry_not_ready``, ``not_loaded``.

Requires the HA handler to be configured (uses its URL and token for
API access). If the HA handler is not enabled, the scanner is not
instantiated.

Backlog: The Handler ABC's ``get_context(event)`` requires a specific
Event, but scanners need a broad listing of all integrations. This
scanner calls the HA API directly using the handler config's credentials.
A future refactoring could add a ``get_status()`` method to the Handler
ABC that returns system-wide health data without requiring an Event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.models import Event, EventMetadata, Severity
from oasisagent.scanner.base import ScannerIngestAdapter

if TYPE_CHECKING:
    from oasisagent.config import HaHandlerConfig, HaHealthCheckConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Integration states that indicate a problem
_ERROR_STATES = frozenset({
    "setup_error",
    "config_entry_not_ready",
    "not_loaded",
    "setup_retry",
    "setup_in_progress",  # not an error per se, but noteworthy if stuck
})

# Only these states trigger events; setup_in_progress is excluded from
# event emission to avoid noise during HA startup
_ALERT_STATES = frozenset({
    "setup_error",
    "config_entry_not_ready",
    "not_loaded",
})


class HaHealthScannerAdapter(ScannerIngestAdapter):
    """Scanner that checks HA integration health via the REST API.

    Calls ``GET /api/config/config_entries/entry`` to list all config
    entries and their states. Emits events on state transitions (healthy
    -> error state, error state -> healthy).
    """

    def __init__(
        self,
        config: HaHealthCheckConfig,
        queue: EventQueue,
        interval: int,
        ha_config: HaHandlerConfig,
    ) -> None:
        super().__init__(queue, interval)
        self._config = config
        self._ha_url = ha_config.url.rstrip("/")
        self._ha_token = ha_config.token
        self._session: aiohttp.ClientSession | None = None
        # State tracking: integration_domain -> "ok" | error_state
        self._states: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "scanner.ha_health"

    async def start(self) -> None:
        """Create HTTP session then start the poll loop."""
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        self._session = aiohttp.ClientSession(
            base_url=self._ha_url, headers=headers, timeout=timeout,
        )
        await super().start()

    async def stop(self) -> None:
        """Close HTTP session and stop the poll loop."""
        await super().stop()
        if self._session:
            await self._session.close()
            self._session = None

    async def _scan(self) -> list[Event]:
        """Fetch config entries and check for error states."""
        assert self._session is not None
        entries = await self._fetch_config_entries()
        return self._evaluate_entries(entries)

    async def _fetch_config_entries(self) -> list[dict[str, Any]]:
        """Fetch all config entries from HA REST API."""
        assert self._session is not None
        async with self._session.get("/api/config/config_entries/entry") as resp:
            resp.raise_for_status()
            return await resp.json()  # type: ignore[no-any-return]

    def _evaluate_entries(self, entries: list[dict[str, Any]]) -> list[Event]:
        """Check each entry's state and emit events on transitions."""
        events: list[Event] = []
        seen_domains: set[str] = set()

        for entry in entries:
            domain = entry.get("domain", "unknown")
            state = entry.get("state", "loaded")
            entry_id = entry.get("entry_id", "")
            title = entry.get("title", domain)

            # Use domain as the tracking key (multiple entries per domain
            # are possible but we track the worst state per domain)
            seen_domains.add(domain)

            new_state = state if state in _ALERT_STATES else "ok"

            old_state = self._states.get(domain)
            self._states[domain] = new_state

            if old_state == new_state:
                continue

            now = datetime.now(tz=UTC)

            # Transition to error
            if new_state != "ok" and old_state != new_state:
                events.append(Event(
                    source=self.name,
                    system="homeassistant",
                    event_type="integration_unhealthy",
                    entity_id=domain,
                    severity=Severity.ERROR,
                    timestamp=now,
                    payload={
                        "domain": domain,
                        "title": title,
                        "entry_id": entry_id,
                        "state": state,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"scanner.ha_health:{domain}",
                    ),
                ))

            # Recovery: error -> ok
            elif new_state == "ok" and old_state is not None and old_state != "ok":
                events.append(Event(
                    source=self.name,
                    system="homeassistant",
                    event_type="integration_recovered",
                    entity_id=domain,
                    severity=Severity.INFO,
                    timestamp=now,
                    payload={
                        "domain": domain,
                        "title": title,
                        "entry_id": entry_id,
                        "previous_state": old_state,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"scanner.ha_health:{domain}",
                    ),
                ))

        return events
