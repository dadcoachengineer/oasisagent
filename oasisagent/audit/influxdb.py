"""InfluxDB audit writer — records events, decisions, and actions.

Every event, decision, and action that passes through OasisAgent is
recorded to InfluxDB for full audit trail. This is best-effort: write
failures are logged but never crash the pipeline.

When InfluxDB is temporarily unreachable, failed points are buffered
in-memory (bounded to ``_BUFFER_MAX`` entries) and retried on the
next successful write. This prevents data loss during transient
outages without risking unbounded memory growth.

ARCHITECTURE.md §9 defines the measurement schemas.

Tag vs. field split (InfluxDB performance):
  Tags (indexed, low cardinality):
    source, system, event_type, severity, entity_id, tier,
    disposition, risk_tier, handler, operation, action_status,
    trigger_type
  Fields (not indexed, high cardinality or variable):
    payload (JSON), details (JSON), error_message, duration_ms,
    confidence, diagnosis, reasoning, correlation_id, summary,
    attempts, window_minutes, message, event_timestamp
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client.rest import ApiException

if TYPE_CHECKING:
    from influxdb_client.client.write_api_async import WriteApiAsync

    from oasisagent.config import AuditConfig
    from oasisagent.engine.circuit_breaker import CircuitBreakerResult
    from oasisagent.engine.decision import DecisionResult
    from oasisagent.models import ActionResult, Event, RecommendedAction, VerifyResult

logger = logging.getLogger(__name__)

# Maximum number of failed points to buffer in memory.  Oldest entries
# are evicted when the buffer is full to prevent unbounded growth
# during extended outages.
_BUFFER_MAX = 1000


class AuditNotStartedError(Exception):
    """Raised when audit methods are called before start()."""


class AuditWriter:
    """Records audit data to InfluxDB v2.

    Follows the same lifecycle pattern as Handler: start() creates
    async resources, stop() tears them down. If InfluxDB is disabled
    in config, all methods are no-ops.

    Failed writes are buffered in-memory (up to ``_BUFFER_MAX`` points)
    and retried on the next successful write, preventing data loss
    during transient InfluxDB outages.
    """

    def __init__(self, config: AuditConfig) -> None:
        self._config = config
        self._client: InfluxDBClientAsync | None = None
        self._write_api: WriteApiAsync | None = None
        self._buffer: collections.deque[tuple[Point, str]] = collections.deque(
            maxlen=_BUFFER_MAX,
        )

    @property
    def _enabled(self) -> bool:
        return self._config.influxdb.enabled

    @property
    def _bucket(self) -> str:
        return self._config.influxdb.bucket

    async def start(self) -> None:
        """Create InfluxDB client. No-op if disabled."""
        if not self._enabled:
            logger.info("Audit writer disabled — skipping InfluxDB connection")
            return

        influx = self._config.influxdb
        self._client = InfluxDBClientAsync(
            url=influx.url,
            token=influx.token,
            org=influx.org,
        )
        self._write_api = self._client.write_api()
        logger.info("Audit writer started (url=%s, bucket=%s)", influx.url, influx.bucket)

    async def stop(self) -> None:
        """Close InfluxDB client. No-op if disabled or not started."""
        if self._client is not None:
            if self._buffer:
                logger.warning(
                    "Audit writer stopping with %d buffered points "
                    "(these will be lost)",
                    len(self._buffer),
                )
            await self._client.close()
            self._client = None
            self._write_api = None
            self._buffer.clear()
            logger.info("Audit writer stopped")

    async def write_event(self, event: Event) -> None:
        """Record an ingested event (oasis_event measurement)."""
        if not self._enabled:
            return
        self._ensure_started()

        point = (
            Point("oasis_event")
            .tag("source", event.source)
            .tag("system", event.system)
            .tag("event_type", event.event_type)
            .tag("entity_id", event.entity_id)
            .tag("severity", event.severity.value)
            .field("payload", json.dumps(event.payload, default=str))
            .field("correlation_id", event.metadata.correlation_id or "")
            .field("event_id", event.id)
            .time(event.timestamp)
        )

        await self._write(point, measurement="oasis_event")

    async def write_decision(
        self, event: Event, result: DecisionResult
    ) -> None:
        """Record a decision engine result (oasis_decision measurement)."""
        if not self._enabled:
            return
        self._ensure_started()

        risk_tier = ""
        if result.guardrail_result is not None:
            risk_tier = result.guardrail_result.risk_tier.value

        point = (
            Point("oasis_decision")
            .tag("event_id", result.event_id)
            .tag("tier", result.tier.value)
            .tag("disposition", result.disposition.value)
            .tag("risk_tier", risk_tier)
            .field("diagnosis", result.diagnosis)
            .field("matched_fix_id", result.matched_fix_id or "")
            .field("details", json.dumps(result.details, default=str))
            .field("event_timestamp", event.timestamp.isoformat())
            .time(datetime.now(UTC))
        )

        await self._write(point, measurement="oasis_decision")

    async def write_action(
        self,
        event: Event,
        action: RecommendedAction,
        result: ActionResult,
    ) -> None:
        """Record a handler action result (oasis_action measurement)."""
        if not self._enabled:
            return
        self._ensure_started()

        point = (
            Point("oasis_action")
            .tag("event_id", event.id)
            .tag("handler", action.handler)
            .tag("operation", action.operation)
            .tag("action_status", result.status.value)
            .field("details", json.dumps(result.details, default=str))
            .field("duration_ms", result.duration_ms or 0.0)
            .field("error_message", result.error_message or "")
            .field("event_timestamp", event.timestamp.isoformat())
            .time(datetime.now(UTC))
        )

        await self._write(point, measurement="oasis_action")

    async def write_circuit_breaker(
        self,
        entity_id: str,
        result: CircuitBreakerResult,
    ) -> None:
        """Record a circuit breaker trip (oasis_circuit_breaker measurement)."""
        if not self._enabled:
            return
        self._ensure_started()

        if result.entity_tripped:
            trigger_type = "entity"
        elif result.global_tripped:
            trigger_type = "global"
        elif result.entity_cooldown:
            trigger_type = "cooldown"
        else:
            trigger_type = "unknown"

        point = (
            Point("oasis_circuit_breaker")
            .tag("entity_id", entity_id)
            .tag("trigger_type", trigger_type)
            .field("reason", result.reason)
            .field("allowed", result.allowed)
            .time(datetime.now(UTC))
        )

        await self._write(point, measurement="oasis_circuit_breaker")

    async def write_verify(
        self,
        event: Event,
        action: RecommendedAction,
        verify_result: VerifyResult,
    ) -> None:
        """Record a verification result (oasis_verify measurement)."""
        if not self._enabled:
            return
        self._ensure_started()

        point = (
            Point("oasis_verify")
            .tag("event_id", event.id)
            .tag("handler", action.handler)
            .tag("operation", action.operation)
            .tag("verified", str(verify_result.verified).lower())
            .field("message", verify_result.message)
            .field("checked_at", verify_result.checked_at.isoformat())
            .field("event_timestamp", event.timestamp.isoformat())
            .time(verify_result.checked_at)
        )

        await self._write(point, measurement="oasis_verify")

    async def write_suppression(
        self,
        event: Event,
        suppressed_count: int,
    ) -> None:
        """Record a suppressed event (oasis_suppression measurement).

        Called when the repeated-event suppression tracker drops an event.
        Uses the same batching/retry as other write methods.
        """
        if not self._enabled:
            return
        self._ensure_started()

        point = (
            Point("oasis_suppression")
            .tag("source", event.source)
            .tag("system", event.system)
            .tag("event_type", event.event_type)
            .tag("entity_id", event.entity_id)
            .tag("severity", event.severity.value)
            .field("event_id", event.id)
            .field("suppressed_count", suppressed_count)
            .field("event_timestamp", event.timestamp.isoformat())
            .time(datetime.now(UTC))
        )

        await self._write(point, measurement="oasis_suppression")

    async def write_notification_archive(
        self, row: dict[str, Any],
    ) -> None:
        """Archive a pruned notification to InfluxDB (oasis_notification_archive)."""
        if not self._enabled:
            return
        self._ensure_started()

        # Use the original notification timestamp so InfluxDB time series
        # reflects when the notification was created, not when it was pruned.
        ts_str = row.get("timestamp")
        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(UTC)

        point = (
            Point("oasis_notification_archive")
            .tag("severity", row.get("severity", "info"))
            .tag("event_id", row.get("event_id", ""))
            .field("title", row.get("title", ""))
            .field("message", row.get("message", ""))
            .field("metadata", json.dumps(row.get("metadata", {}), default=str))
            .field("notification_id", row.get("id", ""))
            .time(ts)
        )

        await self._write(point, measurement="oasis_notification_archive")

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the writer hasn't been started."""
        if self._write_api is None:
            raise AuditNotStartedError(
                "AuditWriter.start() must be called before writing"
            )

    @property
    def buffer_size(self) -> int:
        """Number of points waiting in the retry buffer."""
        return len(self._buffer)

    async def _write(self, point: Point, *, measurement: str = "") -> None:
        """Write a point to InfluxDB. Best-effort with transient retry.

        On success the method also drains any previously buffered points.
        On final failure the point is added to a bounded in-memory buffer
        so it can be retried on the next successful write.
        """
        if self._write_api is None:
            logger.warning(
                "Audit write skipped for %s: write API not initialized",
                measurement,
            )
            return

        if await self._try_write(point, measurement=measurement):
            # Current point succeeded — drain buffered points.
            await self._drain_buffer()
        else:
            # All retries exhausted — buffer for later.
            self._enqueue(point, measurement)

    async def _try_write(
        self, point: Point, *, measurement: str = ""
    ) -> bool:
        """Attempt to write a single point with retry. Return True on success."""
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                assert self._write_api is not None
                await self._write_api.write(bucket=self._bucket, record=point)
                return True
            except Exception as exc:
                if attempt < max_retries and self._is_retryable(exc):
                    delay = 0.5 * (2**attempt)
                    logger.warning(
                        "Audit write failed for %s (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        measurement,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "Audit write failed for %s (attempt %d/%d): %s",
                        measurement,
                        attempt + 1,
                        max_retries + 1,
                        exc,
                    )
                    return False
        return False  # pragma: no cover — unreachable, satisfies type checker

    def _enqueue(self, point: Point, measurement: str) -> None:
        """Add a failed point to the retry buffer.

        The buffer is a bounded deque — oldest entries are evicted
        automatically when ``_BUFFER_MAX`` is reached.
        """
        was_full = len(self._buffer) == self._buffer.maxlen
        self._buffer.append((point, measurement))
        if was_full:
            logger.warning(
                "Audit retry buffer full (%d); oldest point evicted",
                _BUFFER_MAX,
            )
        else:
            logger.info(
                "Audit point buffered for retry (%s); buffer size: %d",
                measurement,
                len(self._buffer),
            )

    async def _drain_buffer(self) -> None:
        """Attempt to flush buffered points after a successful write.

        Stops at the first failure so we don't spin through the entire
        buffer when InfluxDB goes down again mid-drain.
        """
        if not self._buffer:
            return

        drained = 0
        while self._buffer:
            point, meas = self._buffer[0]  # peek
            try:
                assert self._write_api is not None
                await self._write_api.write(bucket=self._bucket, record=point)
            except Exception as exc:
                logger.warning(
                    "Audit buffer drain failed after %d points (%s): %s",
                    drained,
                    meas,
                    exc,
                )
                break
            self._buffer.popleft()
            drained += 1

        if drained:
            logger.info(
                "Audit buffer drained %d points; %d remaining",
                drained,
                len(self._buffer),
            )

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Determine if an exception is transient and worth retrying."""
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True
        if isinstance(exc, ApiException):
            return exc.status is not None and (
                exc.status >= 500 or exc.status == 429
            )
        return False
