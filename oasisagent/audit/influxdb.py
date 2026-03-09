"""InfluxDB audit writer — records events, decisions, and actions.

Every event, decision, and action that passes through OasisAgent is
recorded to InfluxDB for full audit trail. This is best-effort: write
failures are logged but never crash the pipeline.

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

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

if TYPE_CHECKING:
    from influxdb_client.client.write_api_async import WriteApiAsync

    from oasisagent.config import AuditConfig
    from oasisagent.engine.circuit_breaker import CircuitBreakerResult
    from oasisagent.engine.decision import DecisionResult
    from oasisagent.models import ActionResult, Event, RecommendedAction, VerifyResult

logger = logging.getLogger(__name__)


class AuditNotStartedError(Exception):
    """Raised when audit methods are called before start()."""


class AuditWriter:
    """Records audit data to InfluxDB v2.

    Follows the same lifecycle pattern as Handler: start() creates
    async resources, stop() tears them down. If InfluxDB is disabled
    in config, all methods are no-ops.
    """

    def __init__(self, config: AuditConfig) -> None:
        self._config = config
        self._client: InfluxDBClientAsync | None = None
        self._write_api: WriteApiAsync | None = None

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
            await self._client.close()
            self._client = None
            self._write_api = None
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

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the writer hasn't been started."""
        if self._write_api is None:
            raise AuditNotStartedError(
                "AuditWriter.start() must be called before writing"
            )

    async def _write(self, point: Point, *, measurement: str = "") -> None:
        """Write a point to InfluxDB. Best-effort — errors are logged."""
        try:
            assert self._write_api is not None
            await self._write_api.write(bucket=self._bucket, record=point)
        except Exception as exc:
            logger.warning(
                "Audit write failed for %s: %s",
                measurement,
                exc,
            )
