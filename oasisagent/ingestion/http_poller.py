"""HTTP polling ingestion adapter with JMESPath extraction.

Periodically queries configured HTTP/REST endpoints and converts
responses to canonical Events. Supports three modes:

- ``health_check``: HTTP 2xx = ok, anything else emits an event
- ``extract``: Apply JMESPath expressions to extract Event fields
- ``threshold``: Extract numeric value, emit when crossing thresholds
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp
import jmespath

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import HttpPollerTargetConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

_SEVERITY_MAP: dict[str, Severity] = {
    "info": Severity.INFO,
    "warning": Severity.WARNING,
    "error": Severity.ERROR,
    "critical": Severity.CRITICAL,
}


class HttpPollerAdapter(IngestAdapter):
    """Ingestion adapter that polls HTTP endpoints on configurable intervals.

    Each target gets its own polling loop as a concurrent asyncio task.
    State-based dedup ensures events are only emitted on state transitions
    (healthy→unhealthy, below→above threshold).
    """

    def __init__(
        self, targets: list[HttpPollerTargetConfig], queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._targets = targets
        self._stopping = False
        self._tasks: list[asyncio.Task[None]] = []
        self._healthy_targets: set[str] = set()
        # State tracking for dedup: target name → last known state
        self._health_states: dict[str, bool] = {}  # True = healthy
        self._threshold_states: dict[str, str] = {}  # "ok" | "warning" | "critical"

    @property
    def name(self) -> str:
        return "http_poller"

    async def start(self) -> None:
        """Spawn a polling task per target and wait for all to complete."""
        for target in self._targets:
            task = asyncio.create_task(
                self._poll_loop(target), name=f"http_poller:{target.system}",
            )
            self._tasks.append(task)

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def healthy(self) -> bool:
        return len(self._healthy_targets) > 0

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self, target: HttpPollerTargetConfig) -> None:
        """Poll a single target on its configured interval."""
        target_name = target.system

        auth = None
        headers: dict[str, str] = {}
        if target.auth_mode == "basic":
            auth = aiohttp.BasicAuth(target.auth_username, target.auth_password)
        elif target.auth_mode == "token":
            headers[target.auth_header] = target.auth_value

        timeout = aiohttp.ClientTimeout(total=target.timeout)

        while not self._stopping:
            try:
                async with (
                    aiohttp.ClientSession(
                        auth=auth, headers=headers, timeout=timeout,
                    ) as session,
                    session.get(target.url) as resp,
                ):
                    self._healthy_targets.add(target_name)

                    if target.mode == "health_check":
                        self._handle_health_check(target, resp.status)
                    elif target.mode == "extract":
                        body = await resp.json(content_type=None)
                        self._handle_extract(target, body)
                    elif target.mode == "threshold":
                        body = await resp.json(content_type=None)
                        self._handle_threshold(target, body)

            except (TimeoutError, aiohttp.ClientError) as exc:
                self._healthy_targets.discard(target_name)
                self._emit_connection_error(target, exc)
            except asyncio.CancelledError:
                return
            except Exception:
                self._healthy_targets.discard(target_name)
                logger.exception("HTTP poller: unexpected error polling %s", target.url)

            # Sleep in 1-second increments for responsive shutdown
            for _ in range(target.interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Mode handlers
    # ------------------------------------------------------------------

    def _handle_health_check(
        self, target: HttpPollerTargetConfig, status: int,
    ) -> None:
        """Emit events only on state transitions (healthy ↔ unhealthy)."""
        target_name = target.system
        is_healthy = 200 <= status < 300
        was_healthy = self._health_states.get(target_name)

        self._health_states[target_name] = is_healthy

        if was_healthy is None:
            # First poll — only emit if unhealthy
            if not is_healthy:
                self._enqueue(Event(
                    source=self.name,
                    system=target.system,
                    event_type="health_check_failed",
                    entity_id=target_name,
                    severity=Severity.ERROR,
                    timestamp=datetime.now(tz=UTC),
                    payload={"url": target.url, "status_code": status},
                    metadata=EventMetadata(
                        dedup_key=f"http_poller:{target_name}:health_check",
                    ),
                ))
            return

        if was_healthy and not is_healthy:
            self._enqueue(Event(
                source=self.name,
                system=target.system,
                event_type="health_check_failed",
                entity_id=target_name,
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={"url": target.url, "status_code": status},
                metadata=EventMetadata(
                    dedup_key=f"http_poller:{target_name}:health_check",
                ),
            ))
        elif not was_healthy and is_healthy:
            self._enqueue(Event(
                source=self.name,
                system=target.system,
                event_type="health_check_recovered",
                entity_id=target_name,
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"url": target.url, "status_code": status},
                metadata=EventMetadata(
                    dedup_key=f"http_poller:{target_name}:health_check",
                ),
            ))

    def _handle_extract(
        self, target: HttpPollerTargetConfig, body: dict[str, Any],
    ) -> None:
        """Apply JMESPath expressions to build an Event from the response.

        Unlike health_check and threshold modes, extract emits on every poll
        (no dedup). This is intentional — extract targets typically serve
        metrics or status endpoints where each response is a distinct data point.
        """
        extract = target.extract
        if extract is None:
            return

        entity_id = jmespath.search(extract.entity_id, body)
        if entity_id is None:
            entity_id = target.system

        severity = Severity.WARNING
        if extract.severity_expr:
            sev_str = jmespath.search(extract.severity_expr, body)
            if isinstance(sev_str, str):
                severity = _SEVERITY_MAP.get(sev_str.lower(), Severity.WARNING)

        payload = body
        if extract.payload_expr:
            extracted = jmespath.search(extract.payload_expr, body)
            if extracted is not None:
                payload = extracted if isinstance(extracted, dict) else {"value": extracted}

        self._enqueue(Event(
            source=self.name,
            system=target.system,
            event_type=extract.event_type,
            entity_id=str(entity_id),
            severity=severity,
            timestamp=datetime.now(tz=UTC),
            payload=payload if isinstance(payload, dict) else {"raw": payload},
            metadata=EventMetadata(
                dedup_key=f"http_poller:{target.system}:{extract.event_type}:{entity_id}",
            ),
        ))

    def _handle_threshold(
        self, target: HttpPollerTargetConfig, body: dict[str, Any],
    ) -> None:
        """Emit events when a numeric value crosses configured thresholds."""
        threshold = target.threshold
        if threshold is None:
            return

        value = jmespath.search(threshold.value_expr, body)
        if not isinstance(value, (int, float)):
            logger.warning(
                "HTTP poller: threshold value_expr returned non-numeric for %s: %r",
                target.system, value,
            )
            return

        target_name = target.system
        entity_id = threshold.entity_id

        if value >= threshold.critical:
            new_state = "critical"
        elif value >= threshold.warning:
            new_state = "warning"
        else:
            new_state = "ok"

        old_state = self._threshold_states.get(target_name, "ok")
        self._threshold_states[target_name] = new_state

        # Only emit on state change
        if new_state == old_state:
            return

        if new_state == "critical":
            self._enqueue(Event(
                source=self.name,
                system=target.system,
                event_type=threshold.event_type,
                entity_id=entity_id,
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={"value": value, "threshold": threshold.critical, "level": "critical"},
                metadata=EventMetadata(
                    dedup_key=f"http_poller:{target_name}:{threshold.event_type}",
                ),
            ))
        elif new_state == "warning":
            self._enqueue(Event(
                source=self.name,
                system=target.system,
                event_type=threshold.event_type,
                entity_id=entity_id,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={"value": value, "threshold": threshold.warning, "level": "warning"},
                metadata=EventMetadata(
                    dedup_key=f"http_poller:{target_name}:{threshold.event_type}",
                ),
            ))
        elif new_state == "ok" and old_state != "ok":
            self._enqueue(Event(
                source=self.name,
                system=target.system,
                event_type=f"{threshold.event_type}_recovered",
                entity_id=entity_id,
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={"value": value, "level": "ok"},
                metadata=EventMetadata(
                    dedup_key=f"http_poller:{target_name}:{threshold.event_type}",
                ),
            ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_connection_error(
        self, target: HttpPollerTargetConfig, exc: Exception,
    ) -> None:
        """Emit an error event for a connection failure."""
        target_name = target.system
        was_healthy = self._health_states.get(target_name, True)

        if was_healthy:
            self._health_states[target_name] = False
            self._enqueue(Event(
                source=self.name,
                system=target.system,
                event_type="connection_error",
                entity_id=target_name,
                severity=Severity.ERROR,
                timestamp=datetime.now(tz=UTC),
                payload={"url": target.url, "error": str(exc)},
                metadata=EventMetadata(
                    dedup_key=f"http_poller:{target_name}:connection_error",
                ),
            ))

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "HTTP poller: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
