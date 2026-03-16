"""EMQX MQTT broker monitoring ingestion adapter.

Polls EMQX's REST API (v5) to monitor broker health, active alarms,
and node/listener status.

.. warning::

    This is **meta-monitoring** — the agent monitors its own MQTT broker.
    If EMQX goes down, events still flow through the internal queue to
    non-MQTT notification channels (Telegram, Discord, email, Web UI).
    All EMQX known fixes use ``risk_tier: recommend`` minimum to prevent
    auto-remediation from disrupting the agent's own MQTT connection.

Events emitted on state transitions:
- ``emqx_unreachable`` (ERROR) when the API is unreachable
- ``emqx_recovered`` (INFO) when the API comes back online
- ``emqx_alarm_active`` (WARNING) when an alarm is active
- ``emqx_listener_down`` (WARNING) when a listener stops running
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
    from oasisagent.config import EmqxAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class EmqxAdapter(IngestAdapter):
    """Polls EMQX REST API v5 for broker health, alarms, and listeners.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: EmqxAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._service_ok: bool | None = None
        self._active_alarms: set[str] = set()
        self._down_listeners: set[str] = set()

    @property
    def name(self) -> str:
        return "emqx"

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="emqx-poller",
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
                    logger.exception("EMQX: unexpected error")
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

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(self._config.api_key, self._config.api_secret)

    async def _poll_health(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/api/v5/stats`` to verify broker is responding."""
        base = self._config.url.rstrip("/")
        async with session.get(f"{base}/api/v5/stats", auth=self._auth()) as resp:
            resp.raise_for_status()

        was_ok = self._service_ok
        self._service_ok = True

        if was_ok is not None and not was_ok:
            self._enqueue(Event(
                source=self.name,
                system="emqx",
                event_type="emqx_recovered",
                entity_id="emqx",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"url": self._config.url},
                metadata=EventMetadata(dedup_key="emqx:health"),
            ))

        # Check alarms and listeners
        await self._poll_alarms(session)
        await self._poll_listeners(session)

    async def _poll_alarms(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/api/v5/alarms`` for active alarms."""
        base = self._config.url.rstrip("/")

        try:
            async with session.get(
                f"{base}/api/v5/alarms", auth=self._auth(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            alarms = data.get("data", data) if isinstance(data, dict) else data
            current_names: set[str] = set()
            if isinstance(alarms, list):
                for alarm in alarms:
                    name = alarm.get("name", str(alarm))
                    if alarm.get("activated", True):
                        current_names.add(name)

            # New alarms
            for alarm_name in current_names - self._active_alarms:
                self._enqueue(Event(
                    source=self.name,
                    system="emqx",
                    event_type="emqx_alarm_active",
                    entity_id=f"emqx:alarm:{alarm_name}",
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "url": self._config.url,
                        "alarm_name": alarm_name,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"emqx:alarm:{alarm_name}",
                    ),
                ))

            self._active_alarms = current_names
        except (TimeoutError, aiohttp.ClientError):
            logger.warning("EMQX: failed to poll alarms endpoint")

    async def _poll_listeners(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/api/v5/listeners`` for listener status."""
        base = self._config.url.rstrip("/")

        try:
            async with session.get(
                f"{base}/api/v5/listeners", auth=self._auth(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            listeners = data if isinstance(data, list) else []
            current_down: set[str] = set()
            for listener in listeners:
                lid = listener.get("id", str(listener))
                running = listener.get("running", True)
                if not running:
                    current_down.add(lid)

            # Newly down listeners
            for lid in current_down - self._down_listeners:
                self._enqueue(Event(
                    source=self.name,
                    system="emqx",
                    event_type="emqx_listener_down",
                    entity_id=f"emqx:listener:{lid}",
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "url": self._config.url,
                        "listener_id": lid,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"emqx:listener:{lid}",
                    ),
                ))

            self._down_listeners = current_down
        except (TimeoutError, aiohttp.ClientError):
            logger.warning("EMQX: failed to poll listeners endpoint")

    def _handle_failure(self, reason: str) -> None:
        """Handle a failed health check — emit event on transition."""
        was_ok = self._service_ok
        self._service_ok = False
        self._active_alarms = set()
        self._down_listeners = set()

        if was_ok is None or was_ok:
            if was_ok is not None:
                logger.warning("EMQX: health check failed: %s", reason)
            self._enqueue(Event(
                source=self.name,
                system="emqx",
                event_type="emqx_unreachable",
                entity_id="emqx",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "url": self._config.url,
                    "reason": reason,
                },
                metadata=EventMetadata(dedup_key="emqx:health"),
            ))

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "EMQX: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
