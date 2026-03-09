"""Prometheus metrics endpoint — exposes /metrics for scraping.

Uses a custom CollectorRegistry to isolate OasisAgent metrics from
any global metrics registered by dependencies (e.g. LiteLLM).

ARCHITECTURE.md §16.9 defines the metrics specification.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:
    from oasisagent.approval.pending import PendingQueue
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom registry — isolated from global default
# ---------------------------------------------------------------------------

REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

EVENTS_TOTAL = Counter(
    "oasis_events_total",
    "Total ingested events",
    labelnames=["source", "severity"],
    registry=REGISTRY,
)

DECISIONS_TOTAL = Counter(
    "oasis_decisions_total",
    "Total decisions made",
    labelnames=["tier", "disposition", "risk_tier"],
    registry=REGISTRY,
)

ACTIONS_TOTAL = Counter(
    "oasis_actions_total",
    "Total handler actions executed",
    labelnames=["handler", "operation", "result"],
    registry=REGISTRY,
)

CIRCUIT_BREAKER_TRIPS_TOTAL = Counter(
    "oasis_circuit_breaker_trips_total",
    "Total circuit breaker trips",
    labelnames=["trigger_type"],
    registry=REGISTRY,
)

EVENT_PROCESSING_SECONDS = Histogram(
    "oasis_event_processing_seconds",
    "Event processing duration in seconds",
    labelnames=["tier"],
    registry=REGISTRY,
)

QUEUE_DEPTH = Gauge(
    "oasis_queue_depth",
    "Current event queue depth",
    registry=REGISTRY,
)

PENDING_ACTIONS = Gauge(
    "oasis_pending_actions",
    "Current number of pending approval actions",
    registry=REGISTRY,
)

UPTIME_SECONDS = Gauge(
    "oasis_uptime_seconds",
    "Agent uptime in seconds",
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Instrumentation API — called from the orchestrator
# ---------------------------------------------------------------------------


def inc_events(source: str, severity: str) -> None:
    """Increment the events counter."""
    EVENTS_TOTAL.labels(source=source, severity=severity).inc()


def inc_decisions(tier: str, disposition: str, risk_tier: str = "") -> None:
    """Increment the decisions counter."""
    DECISIONS_TOTAL.labels(tier=tier, disposition=disposition, risk_tier=risk_tier).inc()


def inc_actions(handler: str, operation: str, result: str) -> None:
    """Increment the actions counter."""
    ACTIONS_TOTAL.labels(handler=handler, operation=operation, result=result).inc()


def inc_circuit_breaker_trips(trigger_type: str) -> None:
    """Increment the circuit breaker trips counter."""
    CIRCUIT_BREAKER_TRIPS_TOTAL.labels(trigger_type=trigger_type).inc()


def observe_processing_time(tier: str, seconds: float) -> None:
    """Record an event processing duration."""
    EVENT_PROCESSING_SECONDS.labels(tier=tier).observe(seconds)


# ---------------------------------------------------------------------------
# Metrics HTTP server
# ---------------------------------------------------------------------------

_start_time: float = 0.0
_event_queue: EventQueue | None = None
_pending_queue: PendingQueue | None = None


def set_callback_sources(
    event_queue: EventQueue,
    pending_queue: PendingQueue,
) -> None:
    """Register queue references for callback gauges."""
    global _event_queue, _pending_queue
    _event_queue = event_queue
    _pending_queue = pending_queue


def _update_callback_gauges() -> None:
    """Update callback gauges with current values. Called at scrape time."""
    if _event_queue is not None:
        QUEUE_DEPTH.set(_event_queue.size)
    if _pending_queue is not None:
        PENDING_ACTIONS.set(_pending_queue.pending_count)
    if _start_time > 0:
        UPTIME_SECONDS.set(time.monotonic() - _start_time)


async def _metrics_handler(request: web.Request) -> web.Response:
    """Handle GET /metrics requests."""
    _update_callback_gauges()
    body = generate_latest(REGISTRY)
    return web.Response(
        body=body,
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


class MetricsServer:
    """Lightweight aiohttp server exposing /metrics for Prometheus scraping."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Start the HTTP server."""
        global _start_time
        _start_time = time.monotonic()

        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("Metrics server started on port %d", self._port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Metrics server stopped")
