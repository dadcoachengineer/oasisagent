"""Ollama LLM server health-check ingestion adapter.

Polls Ollama's root ``/`` endpoint (returns "Ollama is running") for basic
health, and ``/api/ps`` for loaded model status.

Events emitted on state transitions:
- ``ollama_unreachable`` (ERROR) when the health check fails
- ``ollama_recovered`` (INFO) when the service comes back online
- ``ollama_no_models`` (WARNING) when no models are loaded
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
    from oasisagent.config import OllamaAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class OllamaAdapter(IngestAdapter):
    """Polls Ollama for service health and loaded model status.

    State-based dedup ensures events only fire on transitions.
    """

    def __init__(
        self, config: OllamaAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._service_ok: bool | None = None
        self._has_models: bool | None = None

    @property
    def name(self) -> str:
        return "ollama"

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._poll_loop(), name="ollama-poller",
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
        connector = aiohttp.TCPConnector(ssl=False)
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
                    logger.exception("Ollama: unexpected error")
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
        """Poll ``/`` — returns "Ollama is running" when healthy."""
        base = self._config.url.rstrip("/")
        async with session.get(f"{base}/") as resp:
            resp.raise_for_status()

        was_ok = self._service_ok
        self._service_ok = True

        # Transition from down -> up
        if was_ok is not None and not was_ok:
            self._enqueue(Event(
                source=self.name,
                system="ollama",
                event_type="ollama_recovered",
                entity_id="ollama",
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"url": self._config.url},
                metadata=EventMetadata(
                    dedup_key="ollama:health",
                ),
            ))

        # Check loaded models
        await self._poll_models(session)

    async def _poll_models(self, session: aiohttp.ClientSession) -> None:
        """Poll ``/api/ps`` for currently loaded models."""
        base = self._config.url.rstrip("/")

        try:
            async with session.get(f"{base}/api/ps") as resp:
                resp.raise_for_status()
                data = await resp.json()

            models = data.get("models", [])
            was_has = self._has_models
            has_models = len(models) > 0
            self._has_models = has_models

            if not has_models and (was_has is None or was_has):
                self._enqueue(Event(
                    source=self.name,
                    system="ollama",
                    event_type="ollama_no_models",
                    entity_id="ollama",
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={"url": self._config.url},
                    metadata=EventMetadata(
                        dedup_key="ollama:models",
                    ),
                ))
        except (TimeoutError, aiohttp.ClientError):
            logger.warning("Ollama: failed to poll /api/ps")

    def _handle_failure(self, reason: str) -> None:
        """Handle a failed health check — emit event on transition."""
        was_ok = self._service_ok
        self._service_ok = False
        self._has_models = None

        if was_ok is None or was_ok:
            if was_ok is not None:
                logger.warning("Ollama: health check failed: %s", reason)
            self._enqueue(Event(
                source=self.name,
                system="ollama",
                event_type="ollama_unreachable",
                entity_id="ollama",
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "url": self._config.url,
                    "reason": reason,
                },
                metadata=EventMetadata(
                    dedup_key="ollama:health",
                ),
            ))

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "Ollama: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )

