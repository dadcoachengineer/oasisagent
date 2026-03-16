"""InfluxDB audit reader — queries events, decisions, actions for the Event Explorer.

Separate from AuditWriter (different concerns: Flux queries + pagination
vs. write-only points). Uses the same InfluxDbConfig, owns its own client.

ARCHITECTURE.md §9 defines the measurement schemas queried here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from influxdb_client.client.flux_table import FluxRecord, TableList
    from influxdb_client.client.query_api_async import QueryApiAsync

    from oasisagent.config import AuditConfig

logger = logging.getLogger(__name__)

# Tag value validation — allows dots, underscores, colons, hyphens, slashes
_SAFE_TAG_RE = re.compile(r"^[a-zA-Z0-9_.:\-/]+$")

# Duration strings accepted for time range filters
_DURATION_RE = re.compile(r"^[1-9]\d*[smhd]$")

# Cache TTL for filter options (seconds)
_FILTER_CACHE_TTL = 60.0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class EventRow(BaseModel):
    """Single row in the event list table."""

    event_id: str
    timestamp: datetime
    source: str
    system: str
    event_type: str
    entity_id: str
    severity: str
    disposition: str | None = None
    tier: str | None = None


class EventPage(BaseModel):
    """Paginated event list."""

    rows: list[EventRow]
    offset: int
    limit: int
    has_next: bool
    has_prev: bool


class DecisionRecord(BaseModel):
    """Decision engine result for an event."""

    tier: str
    disposition: str
    risk_tier: str
    diagnosis: str
    matched_fix_id: str
    timestamp: datetime
    details: dict[str, Any] = {}


class ActionRecord(BaseModel):
    """Handler action result for an event."""

    handler: str
    operation: str
    status: str
    duration_ms: float
    error_message: str
    timestamp: datetime


class VerifyRecord(BaseModel):
    """Verification result for an event action."""

    handler: str
    operation: str
    verified: bool
    message: str
    timestamp: datetime


class EventTimeline(BaseModel):
    """Full correlated view for one event_id."""

    event: EventRow
    payload: dict[str, Any]
    correlation_id: str
    decision: DecisionRecord | None = None
    actions: list[ActionRecord] = []
    verifications: list[VerifyRecord] = []


class FilterOptions(BaseModel):
    """Available distinct tag values for filter dropdowns."""

    severities: list[str]
    sources: list[str]
    systems: list[str]
    event_types: list[str]
    dispositions: list[str]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_tag(value: str) -> bool:
    """Check a filter value is safe for Flux query interpolation."""
    return bool(_SAFE_TAG_RE.match(value))


def _validate_duration(value: str) -> bool:
    """Check a duration string is safe (e.g. '1h', '24h', '7d')."""
    return bool(_DURATION_RE.match(value))


# ---------------------------------------------------------------------------
# AuditReader
# ---------------------------------------------------------------------------


class AuditReaderNotStartedError(Exception):
    """Raised when reader methods are called before start()."""


class AuditReader:
    """Queries InfluxDB audit data for the Event Explorer UI.

    Follows the same start()/stop() lifecycle as AuditWriter.
    """

    def __init__(self, config: AuditConfig) -> None:
        self._config = config
        self._client: Any | None = None
        self._query_api: QueryApiAsync | None = None
        self._filter_cache: dict[str, Any] | None = None
        self._filter_cache_ts: float = 0.0

    @property
    def _enabled(self) -> bool:
        return self._config.influxdb.enabled

    @property
    def _bucket(self) -> str:
        return self._config.influxdb.bucket

    @property
    def _org(self) -> str:
        return self._config.influxdb.org

    async def start(self) -> None:
        """Create InfluxDB client and query API. No-op if disabled."""
        if not self._enabled:
            logger.info("Audit reader disabled — skipping InfluxDB connection")
            return

        from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

        influx = self._config.influxdb
        self._client = InfluxDBClientAsync(
            url=influx.url,
            token=influx.token,
            org=influx.org,
        )
        self._query_api = self._client.query_api()
        logger.info("Audit reader started (url=%s, bucket=%s)", influx.url, influx.bucket)

    async def stop(self) -> None:
        """Close InfluxDB client. No-op if disabled or not started."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._query_api = None
            logger.info("Audit reader stopped")

    def _ensure_started(self) -> None:
        if self._query_api is None:
            raise AuditReaderNotStartedError(
                "AuditReader.start() must be called before querying"
            )

    async def _query(self, flux: str) -> TableList:
        """Execute a Flux query. Wraps errors with logging."""
        self._ensure_started()
        assert self._query_api is not None
        try:
            return await self._query_api.query(flux, org=self._org)
        except Exception as exc:
            logger.warning("Audit query failed: %s", exc)
            raise

    # -------------------------------------------------------------------
    # Public query methods
    # -------------------------------------------------------------------

    async def list_events(
        self,
        *,
        offset: int = 0,
        limit: int = 25,
        duration: str = "24h",
        severity: str | None = None,
        source: str | None = None,
        system: str | None = None,
        event_type: str | None = None,
        disposition: str | None = None,
    ) -> EventPage:
        """Query paginated event list with optional filters.

        Uses limit+1 pattern to detect has_next without a count query.
        Then batch-fetches decisions for the page's event_ids.
        """
        if not _validate_duration(duration):
            duration = "24h"

        # Build tag filters
        filters: list[str] = []
        for tag, val in [
            ("severity", severity),
            ("source", source),
            ("system", system),
            ("event_type", event_type),
        ]:
            if val and _validate_tag(val):
                filters.append(f'  |> filter(fn: (r) => r["{tag}"] == "{val}")')

        filter_str = "\n".join(filters)

        # Fetch limit+1 to detect has_next
        fetch_limit = limit + 1

        flux = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -{duration})
  |> filter(fn: (r) => r["_measurement"] == "oasis_event")
{filter_str}
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: {fetch_limit}, offset: {offset})"""

        tables = await self._query(flux)
        records = [r for table in tables for r in table.records]

        has_next = len(records) > limit
        if has_next:
            records = records[:limit]

        rows = [self._record_to_event_row(r) for r in records]

        # Batch-fetch decisions for disposition column
        if rows:
            event_ids = [r.event_id for r in rows]
            dispositions_map = await self._batch_decisions(event_ids, duration)
            for row in rows:
                info = dispositions_map.get(row.event_id)
                if info:
                    row.disposition = info["disposition"]
                    row.tier = info["tier"]

        # Post-filter by disposition (comes from decision, not event measurement)
        if disposition and _validate_tag(disposition):
            rows = [r for r in rows if r.disposition == disposition]

        return EventPage(
            rows=rows,
            offset=offset,
            limit=limit,
            has_next=has_next,
            has_prev=offset > 0,
        )

    async def get_event_timeline(self, event_id: str) -> EventTimeline | None:
        """Get full correlated view for a single event."""
        if not _validate_tag(event_id):
            return None

        # Fetch the event itself
        flux_event = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -90d)
  |> filter(fn: (r) => r["_measurement"] == "oasis_event")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => r["event_id"] == "{event_id}")
  |> limit(n: 1)"""

        tables = await self._query(flux_event)
        records = [r for table in tables for r in table.records]
        if not records:
            return None

        rec = records[0]
        event_row = self._record_to_event_row(rec)

        payload_raw = rec.values.get("payload", "{}")
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}

        correlation_id = rec.values.get("correlation_id", "")

        # Parallel fetch: decision, actions, verifications
        decision_coro = self._fetch_decision(event_id)
        actions_coro = self._fetch_actions(event_id)
        verifications_coro = self._fetch_verifications(event_id)

        decision, actions, verifications = await asyncio.gather(
            decision_coro, actions_coro, verifications_coro
        )

        return EventTimeline(
            event=event_row,
            payload=payload,
            correlation_id=correlation_id,
            decision=decision,
            actions=actions,
            verifications=verifications,
        )

    async def get_filter_options(self) -> FilterOptions:
        """Get distinct tag values for filter dropdowns. Cached with 60s TTL."""
        now = time.monotonic()
        if (
            self._filter_cache is not None
            and (now - self._filter_cache_ts) < _FILTER_CACHE_TTL
        ):
            return FilterOptions(**self._filter_cache)

        tag_queries = {
            "severities": ("oasis_event", "severity"),
            "sources": ("oasis_event", "source"),
            "systems": ("oasis_event", "system"),
            "event_types": ("oasis_event", "event_type"),
            "dispositions": ("oasis_decision", "disposition"),
        }

        results: dict[str, list[str]] = {}
        for key, (measurement, tag) in tag_queries.items():
            flux = f"""\
import "influxdata/influxdb/schema"
schema.tagValues(
  bucket: "{self._bucket}",
  tag: "{tag}",
  predicate: (r) => r["_measurement"] == "{measurement}",
  start: -90d,
)"""
            try:
                tables = await self._query(flux)
                values = [r.get_value() for table in tables for r in table.records]
                results[key] = sorted(v for v in values if v)
            except Exception:
                logger.warning("Failed to fetch tag values for %s.%s", measurement, tag)
                results[key] = []

        self._filter_cache = results
        self._filter_cache_ts = now
        return FilterOptions(**results)

    async def get_event_density(
        self,
        *,
        duration: str = "24h",
        window: str = "15m",
    ) -> list[dict[str, Any]]:
        """Get event counts per time bucket for the density chart.

        Returns a list of {"time": datetime, "count": int} dicts.
        Uses aggregateWindow for efficient server-side bucketing.
        """
        if not _validate_duration(duration):
            duration = "24h"
        if not _validate_duration(window):
            window = "15m"

        flux = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -{duration})
  |> filter(fn: (r) => r["_measurement"] == "oasis_event")
  |> filter(fn: (r) => r["_field"] == "event_id")
  |> group()
  |> aggregateWindow(every: {window}, fn: count, column: "_value", createEmpty: true)
  |> yield(name: "density")"""

        try:
            tables = await self._query(flux)
        except Exception:
            return []

        result: list[dict[str, Any]] = []
        for table in tables:
            for rec in table.records:
                ts = rec.get_time()
                count = rec.get_value()
                if ts is not None:
                    result.append({
                        "time": ts.isoformat(),
                        "count": int(count) if count else 0,
                    })
        return result

    async def get_suppressions(
        self,
        *,
        duration: str = "24h",
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get suppression records for the timeline.

        Returns a list of suppression events with counts.
        """
        if not _validate_duration(duration):
            duration = "24h"

        filters = ""
        if entity_id and _validate_tag(entity_id):
            filters = f'\n  |> filter(fn: (r) => r["entity_id"] == "{entity_id}")'

        flux = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -{duration})
  |> filter(fn: (r) => r["_measurement"] == "oasis_suppression")
{filters}
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 100)"""

        try:
            tables = await self._query(flux)
        except Exception:
            return []

        result: list[dict[str, Any]] = []
        for table in tables:
            for rec in table.records:
                v = rec.values
                ts = rec.get_time()
                result.append({
                    "timestamp": ts.isoformat() if ts else "",
                    "source": v.get("source", ""),
                    "system": v.get("system", ""),
                    "event_type": v.get("event_type", ""),
                    "entity_id": v.get("entity_id", ""),
                    "severity": v.get("severity", ""),
                    "suppressed_count": int(v.get("suppressed_count", 0)),
                })
        return result

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _record_to_event_row(rec: FluxRecord) -> EventRow:
        """Convert an InfluxDB record to an EventRow."""
        values = rec.values
        ts = rec.get_time()
        if ts is None:
            ts = datetime.now(UTC)
        return EventRow(
            event_id=values.get("event_id", ""),
            timestamp=ts,
            source=values.get("source", ""),
            system=values.get("system", ""),
            event_type=values.get("event_type", ""),
            entity_id=values.get("entity_id", ""),
            severity=values.get("severity", ""),
        )

    async def _batch_decisions(
        self, event_ids: list[str], duration: str
    ) -> dict[str, dict[str, str]]:
        """Fetch disposition + tier for a list of event_ids."""
        # Build regex filter for event_ids
        escaped = [re.escape(eid) for eid in event_ids]
        id_pattern = "|".join(escaped)

        flux = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -{duration})
  |> filter(fn: (r) => r["_measurement"] == "oasis_decision")
  |> filter(fn: (r) => r["event_id"] =~ /^({id_pattern})$/)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")"""

        try:
            tables = await self._query(flux)
        except Exception:
            return {}

        result: dict[str, dict[str, str]] = {}
        for table in tables:
            for rec in table.records:
                eid = rec.values.get("event_id", "")
                if eid:
                    result[eid] = {
                        "disposition": rec.values.get("disposition", ""),
                        "tier": rec.values.get("tier", ""),
                    }
        return result

    async def _fetch_decision(self, event_id: str) -> DecisionRecord | None:
        """Fetch the decision record for an event."""
        flux = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -90d)
  |> filter(fn: (r) => r["_measurement"] == "oasis_decision")
  |> filter(fn: (r) => r["event_id"] == "{event_id}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> limit(n: 1)"""

        try:
            tables = await self._query(flux)
        except Exception:
            return None

        records = [r for table in tables for r in table.records]
        if not records:
            return None

        rec = records[0]
        v = rec.values
        ts = rec.get_time()
        if ts is None:
            ts = datetime.now(UTC)

        # Parse the JSON details blob (DecisionDetails TypedDict)
        details_raw = v.get("details", "{}")
        try:
            details = json.loads(details_raw) if isinstance(details_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            details = {}

        return DecisionRecord(
            tier=v.get("tier", ""),
            disposition=v.get("disposition", ""),
            risk_tier=v.get("risk_tier", ""),
            diagnosis=v.get("diagnosis", ""),
            matched_fix_id=v.get("matched_fix_id", ""),
            timestamp=ts,
            details=details,
        )

    async def _fetch_actions(self, event_id: str) -> list[ActionRecord]:
        """Fetch action records for an event."""
        flux = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -90d)
  |> filter(fn: (r) => r["_measurement"] == "oasis_action")
  |> filter(fn: (r) => r["event_id"] == "{event_id}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])"""

        try:
            tables = await self._query(flux)
        except Exception:
            return []

        results: list[ActionRecord] = []
        for table in tables:
            for rec in table.records:
                v = rec.values
                ts = rec.get_time()
                if ts is None:
                    ts = datetime.now(UTC)
                results.append(
                    ActionRecord(
                        handler=v.get("handler", ""),
                        operation=v.get("operation", ""),
                        status=v.get("action_status", ""),
                        duration_ms=float(v.get("duration_ms", 0.0)),
                        error_message=v.get("error_message", ""),
                        timestamp=ts,
                    )
                )
        return results

    async def _fetch_verifications(self, event_id: str) -> list[VerifyRecord]:
        """Fetch verification records for an event."""
        flux = f"""\
from(bucket: "{self._bucket}")
  |> range(start: -90d)
  |> filter(fn: (r) => r["_measurement"] == "oasis_verify")
  |> filter(fn: (r) => r["event_id"] == "{event_id}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])"""

        try:
            tables = await self._query(flux)
        except Exception:
            return []

        results: list[VerifyRecord] = []
        for table in tables:
            for rec in table.records:
                v = rec.values
                ts = rec.get_time()
                if ts is None:
                    ts = datetime.now(UTC)
                results.append(
                    VerifyRecord(
                        handler=v.get("handler", ""),
                        operation=v.get("operation", ""),
                        verified=v.get("verified", "false") == "true",
                        message=v.get("message", ""),
                        timestamp=ts,
                    )
                )
        return results
