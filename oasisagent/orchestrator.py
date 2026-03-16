"""Orchestrator — wires components together and runs the event loop.

The orchestrator is the application's main loop. It builds all components
from config, starts them in dependency order, pulls events from the queue,
and runs them through the full pipeline. Graceful shutdown on SIGTERM/SIGINT.

ARCHITECTURE.md §15 defines the orchestrator specification.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from oasisagent.approval.listener import ApprovalListener
from oasisagent.approval.pending import ApprovalDecision, PendingAction, PendingQueue
from oasisagent.audit.influxdb import AuditWriter
from oasisagent.db.stats_store import StatsStore
from oasisagent.db.topology_store import TopologyStore
from oasisagent.engine.circuit_breaker import CircuitBreaker
from oasisagent.engine.correlator import EventCorrelator
from oasisagent.engine.cross_correlator import CrossDomainCorrelator
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionEngine,
    DecisionResult,
    DecisionTier,
)
from oasisagent.engine.guardrails import GuardrailsEngine
from oasisagent.engine.known_fixes import KnownFixRegistry
from oasisagent.engine.queue import EventQueue
from oasisagent.engine.service_graph import ServiceGraph
from oasisagent.handlers.docker import DockerHandler
from oasisagent.handlers.homeassistant import HomeAssistantHandler
from oasisagent.ingestion.ha_log_poller import HaLogPollerAdapter
from oasisagent.ingestion.ha_websocket import HaWebSocketAdapter
from oasisagent.ingestion.http_poller import HttpPollerAdapter
from oasisagent.ingestion.mqtt import MqttAdapter
from oasisagent.llm.client import LLMClient, LLMRole
from oasisagent.llm.reasoning import ReasoningService
from oasisagent.llm.triage import TriageService
from oasisagent.metrics import MetricsServer
from oasisagent.metrics import inc_actions as _inc_actions
from oasisagent.metrics import inc_decisions as _inc_decisions
from oasisagent.metrics import inc_events as _inc_events
from oasisagent.metrics import observe_processing_time as _observe_processing_time
from oasisagent.models import (
    ActionResult,
    ActionStatus,
    Event,
    Notification,
    RecommendedAction,
    RiskTier,
    VerifyResult,
)
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.mqtt import MqttNotificationChannel
from oasisagent.notifications.web_channel import WebNotificationChannel

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import aiosqlite

    from oasisagent.config import OasisAgentConfig
    from oasisagent.db.config_store import ConfigStore
    from oasisagent.db.notification_store import NotificationStore
    from oasisagent.handlers.base import Handler
    from oasisagent.ingestion.base import IngestAdapter
    from oasisagent.models import Event
    from oasisagent.notifications.base import NotificationChannel

logger = logging.getLogger(__name__)

# Suffixes that indicate a recovery/resolution event.  When an event_type
# ends with one of these, suppression for the corresponding entity is reset.
_RECOVERY_SUFFIXES: tuple[str, ...] = ("_recovered", "_reconnected", "_renewed")


class EventSuppressionTracker:
    """Track consecutive identical events per (entity_id, event_type).

    After *threshold* consecutive identical events the tracker suppresses
    further occurrences until the entity produces a different event_type
    (typically a recovery event).

    This is a lightweight, in-memory tracker — no persistence needed.
    """

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = threshold
        # key → consecutive count
        self._counts: dict[tuple[str, str], int] = {}

    # -----------------------------------------------------------------

    def check(self, event: Event) -> int:
        """Return the suppressed count if event should be suppressed, else 0.

        A return value > 0 means the event was suppressed and the value
        indicates the consecutive count (useful for audit recording).

        Side-effects:
        * Increments the counter for (entity_id, event_type).
        * Resets *all* counters for the entity_id when a recovery event
          or a different event_type is seen.
        """
        entity_id = event.entity_id
        event_type = event.event_type
        key = (entity_id, event_type)

        # Recovery events reset suppression for the whole entity.
        if any(event_type.endswith(s) for s in _RECOVERY_SUFFIXES):
            self._reset_entity(entity_id)
            return 0

        # Different event_type for the same entity resets previous key.
        self._reset_entity(entity_id, keep=key)

        count = self._counts.get(key, 0) + 1
        self._counts[key] = count

        if count == self._threshold:
            logger.warning(
                "Suppressing repeated %s events for %s (seen %d consecutive)",
                event_type,
                entity_id,
                count,
            )

        if count > self._threshold:
            return count
        return 0

    def reset(self) -> None:
        """Clear all tracking state."""
        self._counts.clear()

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _reset_entity(
        self, entity_id: str, *, keep: tuple[str, str] | None = None
    ) -> None:
        """Remove all counters for *entity_id* except *keep*."""
        to_remove = [
            k for k in self._counts if k[0] == entity_id and k != keep
        ]
        for k in to_remove:
            del self._counts[k]


class Orchestrator:
    """Builds, starts, and runs all OasisAgent components.

    This is the application entry point. ``run()`` blocks until a shutdown
    signal is received or ``shutdown()`` is called externally.
    """

    def __init__(
        self,
        config: OasisAgentConfig,
        db: aiosqlite.Connection | None = None,
        config_store: ConfigStore | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._config_store = config_store
        self._shutting_down = False

        # Components — populated by _build_infrastructure() + _build_db_components()
        # (or _build_components_from_config() as fallback)
        self._queue: EventQueue | None = None
        self._correlator: EventCorrelator | None = None
        self._registry: KnownFixRegistry | None = None
        self._circuit_breaker: CircuitBreaker | None = None
        self._guardrails: GuardrailsEngine | None = None
        self._llm_client: LLMClient | None = None
        self._triage_service: TriageService | None = None
        self._reasoning_service: ReasoningService | None = None
        self._decision_engine: DecisionEngine | None = None
        self._handlers: dict[str, Handler] = {}
        self._audit: AuditWriter | None = None
        self._dispatcher: NotificationDispatcher | None = None
        self._pending_queue: PendingQueue | None = None
        self._approval_listener: ApprovalListener | None = None
        self._approval_listener_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._metrics_server: MetricsServer | None = None
        self._adapters: list[IngestAdapter] = []
        self._adapter_tasks: list[asyncio.Task[None]] = []
        self._stats_store: StatsStore | None = None
        self._notification_store: NotificationStore | None = None
        self._web_channel: WebNotificationChannel | None = None
        self._topology_store: TopologyStore | None = None
        self._service_graph: ServiceGraph | None = None
        self._cross_correlator: CrossDomainCorrelator | None = None

        # Repeated-event suppression (§ issue #168)
        self._suppression = EventSuppressionTracker(
            threshold=config.agent.max_consecutive_identical,
        )

        # Stats — seeded from SQLite on startup when db is available
        self._events_processed: int = 0
        self._actions_taken: int = 0
        self._errors: int = 0

    def enqueue(self, event: Event) -> None:
        """Submit an event for processing.

        Raises:
            RuntimeError: If the queue is not initialized.
            asyncio.QueueFull: If the queue is at capacity.
        """
        if self._queue is None:
            msg = "Orchestrator not started"
            raise RuntimeError(msg)
        self._queue.put_nowait(event)

    def _get_stats(self) -> tuple[int, int, int]:
        """Return current counter values for the stats flush loop."""
        return self._events_processed, self._actions_taken, self._errors

    async def run(self) -> None:
        """Start all components and enter the main event loop.

        Blocks until shutdown signal. This is the standalone entry point
        (``oasisagent run``). Under FastAPI, use ``start()`` / ``run_loop()``
        / ``stop()`` instead — the lifespan manages the lifecycle.
        """
        await self.start()
        self._install_signal_handlers()
        try:
            await self.run_loop()
        finally:
            await self.stop()

    async def start(self) -> None:
        """Build and start all components without entering the event loop.

        Call this from FastAPI lifespan startup. Does NOT install signal
        handlers — under uvicorn, signal handling belongs to uvicorn.

        Two-phase startup:
        1. ``_build_infrastructure()`` — sync singletons from config
        2. ``_build_db_components()`` — async, queries SQLite tables
           OR ``_build_components_from_config()`` — sync fallback
        """
        # Phase A: infrastructure singletons (always from config)
        self._build_infrastructure()

        # Phase B: user-configurable components (DB or config fallback)
        if self._config_store is not None:
            await self._build_db_components()
        else:
            self._build_components_from_config()

        # Upgrade pending queue, notification store, and stats from SQLite
        # when available. Must happen after components and before start.
        if self._db is not None:
            self._pending_queue = await PendingQueue.from_db(self._db)

            # Web notification channel — always enabled when db is available
            from oasisagent.db.notification_store import NotificationStore

            self._notification_store = NotificationStore(self._db)
            self._web_channel = WebNotificationChannel(self._notification_store)
            if self._dispatcher is not None:
                self._dispatcher._channels.append(self._web_channel)

            self._stats_store = await StatsStore.from_db(self._db)
            vals = self._stats_store.values
            self._events_processed = vals["events_processed"]
            self._actions_taken = vals["actions_taken"]
            self._errors = vals["errors"]
            self._stats_store.start(self._get_stats)

        await self._start_components()
        logger.info("OasisAgent started")

    async def run_loop(self) -> None:
        """Run the main event processing loop.

        Blocks until ``_shutting_down`` is set or the task is cancelled.
        Call this as a background task from FastAPI lifespan.
        """
        logger.info("Entering event loop")
        last_prune = time.monotonic()
        try:
            while not self._shutting_down:
                await self._expire_stale_actions()
                now = time.monotonic()
                if now - last_prune >= 60.0:
                    last_prune = now
                    await self._prune_notifications()

                try:
                    assert self._queue is not None
                    event = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except TimeoutError:
                    continue
                await self._process_one(event)
                self._queue.task_done()
        except asyncio.CancelledError:
            pass

    # Maps handler name() → DB service type for health lookups.
    _HANDLER_NAME_TO_TYPE: ClassVar[dict[str, str]] = {
        "homeassistant": "ha_handler",
        "docker": "docker_handler",
        "portainer": "portainer_handler",
        "proxmox": "proxmox_handler",
        "unifi": "unifi_handler",
        "cloudflare": "cloudflare_handler",
    }

    # Maps notification channel name() → DB notification type.
    # Only mqtt differs (name()="mqtt", DB type="mqtt_notification").
    # email, webhook, telegram name() values match their DB types directly
    # and fall through via the .get() fallback.
    _CHANNEL_NAME_TO_TYPE: ClassVar[dict[str, str]] = {
        "mqtt": "mqtt_notification",
    }

    # Internal service types that lack a real health check.
    _INTERNAL_SERVICE_TYPES: tuple[str, ...] = (
        "influxdb", "guardrails", "circuit_breaker", "llm_options",
    )

    # Maps LLM roles to DB service types for health lookups.
    _LLM_ROLE_TO_TYPE: ClassVar[dict[str, str]] = {
        "triage": "llm_triage",
        "reasoning": "llm_reasoning",
    }

    _HEALTH_CHECK_TIMEOUT: ClassVar[float] = 5.0  # seconds per component

    async def get_component_health(self) -> dict[str, dict[str, str]]:
        """Return live health status for all active components.

        Returns ``{"connectors": {...}, "services": {...}, "notifications": {...}}``
        where each value maps a DB type string to a health status string:
        ``"connected"``, ``"disconnected"``, ``"error"``, or ``"unknown"``.

        All health checks run in parallel with a per-component timeout so
        one slow handler (e.g., Proxmox behind VPN) cannot block the
        entire response.
        """
        connectors: dict[str, str] = {}
        services: dict[str, str] = {}
        notifications: dict[str, str] = {}
        scanner_detail = ""

        # Build a list of (key, category, component) tuples to check in
        # parallel.  Each entry becomes one _check_health coroutine.
        checks: list[tuple[str, str, object]] = []

        # --- Connectors (adapters) ---
        for adapter in self._adapters:
            checks.append((adapter.name, "connector", adapter))

        # --- Services (handlers) ---
        for handler in self._handlers.values():
            db_type = self._HANDLER_NAME_TO_TYPE.get(
                handler.name(), handler.name(),
            )
            checks.append((db_type, "service", handler))

        # --- Notifications ---
        if self._dispatcher is not None:
            for channel in self._dispatcher.channels:
                db_type = self._CHANNEL_NAME_TO_TYPE.get(
                    channel.name(), channel.name(),
                )
                checks.append((db_type, "notification", channel))

        # Fire all checks concurrently with per-component timeout.
        results = await asyncio.gather(
            *(self._check_health(comp) for _, _, comp in checks),
        )

        # Distribute results into the correct category dicts.
        scanner_results: list[tuple[str, str]] = []
        for (key, category, _), status in zip(checks, results, strict=True):
            if category == "connector":
                if key.startswith("scanner."):
                    scanner_results.append((key, status))
                else:
                    connectors[key] = status
            elif category == "service":
                services[key] = status
            else:
                notifications[key] = status

        # --- Services: LLM endpoints — infer from last call (no I/O) ---
        if self._llm_client is not None:
            for role in LLMRole:
                db_type = self._LLM_ROLE_TO_TYPE.get(role.value, role.value)
                services[db_type] = self._llm_client.get_role_health(role)

        # --- Services: internal components → "unknown" ---
        for svc_type in self._INTERNAL_SERVICE_TYPES:
            services[svc_type] = "unknown"

        # --- Services: scanner aggregate ---
        if scanner_results:
            statuses = [s for _, s in scanner_results]
            healthy_count = statuses.count("connected")
            total = len(statuses)
            scanner_detail = f"{healthy_count}/{total} scanners healthy"
            if any(s == "error" for s in statuses):
                services["scanner"] = "error"
            elif any(s == "disconnected" for s in statuses):
                services["scanner"] = "disconnected"
            else:
                services["scanner"] = "connected"

        # --- Connector detail (per-endpoint breakdown) ---
        connector_details: dict[str, dict[str, str]] = {}
        for adapter in self._adapters:
            if hasattr(adapter, "health_detail"):
                detail = adapter.health_detail()
                if detail:
                    connector_details[adapter.name] = detail

        result: dict[str, dict[str, str]] = {
            "connectors": connectors,
            "services": services,
            "notifications": notifications,
        }
        if connector_details:
            result["connector_detail"] = connector_details  # type: ignore[assignment]
        if scanner_detail:
            result["scanner_detail"] = {"detail": scanner_detail}
        return result

    @classmethod
    async def _check_health(cls, component: object) -> str:
        """Call healthy() on a component with a timeout.

        Returns ``"connected"``, ``"disconnected"``, or ``"error"``.
        A timeout is treated as ``"error"`` so one slow component never
        blocks the entire health response.
        """
        try:
            is_healthy = await asyncio.wait_for(
                component.healthy(),  # type: ignore[union-attr]
                timeout=cls._HEALTH_CHECK_TIMEOUT,
            )
            return "connected" if is_healthy else "disconnected"
        except TimeoutError:
            return "error"
        except Exception:
            return "error"

    # -------------------------------------------------------------------
    # Component restart
    # -------------------------------------------------------------------

    async def restart_connector(self, connector_id: int) -> bool:
        """Restart a single ingestion adapter by its database ID.

        Looks up the connector row in the database, stops the old adapter,
        creates a new one with fresh config, and starts it.

        Returns True on success, False if the connector was not found or
        the restart failed.
        """
        store = self._config_store
        if store is None:
            logger.error("Cannot restart connector: no config store available")
            return False

        row = await store.get_connector(connector_id)
        if row is None:
            logger.warning("Connector %d not found for restart", connector_id)
            return False

        db_type = row["type"]
        logger.info(
            "Restarting connector %d (type=%s, name=%s)",
            connector_id, db_type, row["name"],
        )

        # Find and stop the old adapter
        old_adapter: IngestAdapter | None = None
        old_idx: int | None = None
        old_task: asyncio.Task[None] | None = None

        for idx, adapter in enumerate(self._adapters):
            if adapter.name == db_type:
                old_adapter = adapter
                old_idx = idx
                break

        if old_adapter is not None and old_idx is not None:
            try:
                await old_adapter.stop()
            except Exception:
                logger.exception("Error stopping adapter %s during restart", db_type)

            # Cancel the adapter task
            if old_idx < len(self._adapter_tasks):
                old_task = self._adapter_tasks[old_idx]
                old_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await old_task

        if not row["enabled"]:
            # Connector is disabled — just remove the old one
            if old_idx is not None:
                self._adapters.pop(old_idx)
                if old_idx < len(self._adapter_tasks):
                    self._adapter_tasks.pop(old_idx)
            logger.info("Connector %d disabled, removed adapter", connector_id)
            return True

        # Build a new adapter directly from the database row config
        new_adapter = self._build_adapter_from_row(db_type, row["config"])
        if new_adapter is None:
            if old_idx is not None:
                self._adapters.pop(old_idx)
                if old_idx < len(self._adapter_tasks):
                    self._adapter_tasks.pop(old_idx)
            logger.warning(
                "Connector %d: unknown adapter type %r", connector_id, db_type,
            )
            return False

        # Replace or append
        if old_idx is not None:
            self._adapters[old_idx] = new_adapter
            new_task = asyncio.create_task(
                self._run_adapter(new_adapter), name=f"adapter-{new_adapter.name}",
            )
            if old_idx < len(self._adapter_tasks):
                self._adapter_tasks[old_idx] = new_task
            else:
                self._adapter_tasks.append(new_task)
        else:
            self._adapters.append(new_adapter)
            new_task = asyncio.create_task(
                self._run_adapter(new_adapter), name=f"adapter-{new_adapter.name}",
            )
            self._adapter_tasks.append(new_task)

        logger.info("Connector %d restarted successfully (type=%s)", connector_id, db_type)
        return True

    async def restart_service(self, service_id: int) -> bool:
        """Restart a single handler/service by its database ID.

        Looks up the service row in the database, stops the old handler,
        creates a new one with fresh config, and starts it.

        Returns True on success, False if the service was not found or
        the restart failed.
        """
        store = self._config_store
        if store is None:
            logger.error("Cannot restart service: no config store available")
            return False

        row = await store.get_service(service_id)
        if row is None:
            logger.warning("Service %d not found for restart", service_id)
            return False

        db_type = row["type"]
        logger.info("Restarting service %d (type=%s, name=%s)", service_id, db_type, row["name"])

        # Check if this is a handler type
        handler_name: str | None = None
        for hname, htype in self._HANDLER_NAME_TO_TYPE.items():
            if htype == db_type:
                handler_name = hname
                break

        if handler_name is None:
            # Not a handler — internal service types cannot be restarted individually
            logger.warning(
                "Service type %s is not a restartable handler", db_type,
            )
            return False

        # Stop the old handler
        old_handler = self._handlers.get(handler_name)
        if old_handler is not None:
            try:
                await old_handler.stop()
            except Exception:
                logger.exception("Error stopping handler %s during restart", handler_name)
            del self._handlers[handler_name]

        if not row["enabled"]:
            logger.info("Service %d disabled, removed handler", service_id)
            return True

        # Build a new handler directly from the database row config
        new_handler = self._build_handler_from_row(db_type, row["config"])
        if new_handler is None:
            logger.info("Service %d: handler type %s not buildable", service_id, db_type)
            return True

        # Start the new handler
        try:
            await new_handler.start()
            self._handlers[handler_name] = new_handler
            logger.info("Service %d restarted successfully (type=%s)", service_id, db_type)
            return True
        except Exception:
            logger.exception("Failed to start handler %s during restart", handler_name)
            return False

    async def restart_notification(self, notification_id: int) -> bool:
        """Restart a single notification channel by its database ID.

        Looks up the notification row in the database, stops the old channel,
        creates a new one with fresh config, and starts it.

        Returns True on success, False if the notification was not found or
        the restart failed.
        """
        store = self._config_store
        if store is None:
            logger.error("Cannot restart notification: no config store available")
            return False

        row = await store.get_notification(notification_id)
        if row is None:
            logger.warning("Notification %d not found for restart", notification_id)
            return False

        db_type = row["type"]
        logger.info(
            "Restarting notification %d (type=%s, name=%s)",
            notification_id, db_type, row["name"],
        )

        if self._dispatcher is None:
            logger.error("Cannot restart notification: no dispatcher")
            return False

        # Find the channel name that maps to this DB type
        channel_name: str | None = None
        for cname, ctype in self._CHANNEL_NAME_TO_TYPE.items():
            if ctype == db_type:
                channel_name = cname
                break
        # Fallback: channel name matches DB type directly
        if channel_name is None:
            channel_name = db_type

        # Stop and remove the old channel
        old_channels = self._dispatcher._channels
        old_channel = None
        old_idx = None
        for idx, ch in enumerate(old_channels):
            if ch.name() == channel_name:
                old_channel = ch
                old_idx = idx
                break

        if old_channel is not None and old_idx is not None:
            try:
                await old_channel.stop()
            except Exception:
                logger.exception("Error stopping channel %s during restart", channel_name)
            old_channels.pop(old_idx)

        if not row["enabled"]:
            logger.info("Notification %d disabled, removed channel", notification_id)
            return True

        # Build a new channel directly from the database row config
        new_channel = self._build_notification_from_row(db_type, row["config"])
        if new_channel is None:
            logger.info(
                "Notification %d: type %s not buildable", notification_id, db_type,
            )
            return True

        # Start and add the new channel
        try:
            await new_channel.start()
            self._dispatcher._channels.append(new_channel)
            logger.info(
                "Notification %d restarted successfully (type=%s)",
                notification_id, db_type,
            )
            return True
        except Exception:
            logger.exception("Failed to start channel %s during restart", channel_name)
            return False

    def _build_adapter_from_row(
        self, db_type: str, row_config: dict[str, Any],
    ) -> IngestAdapter | None:
        """Build an adapter directly from a database row's config dict.

        Uses the registry's ``module_path`` / ``class_name`` to locate the
        adapter class — no hardcoded mapping needed.  Returns ``None`` for
        types that have no adapter class (webhook_receiver, http_poller
        when handled via aggregation).
        """
        assert self._queue is not None
        import importlib

        from oasisagent.db.registry import get_type_meta

        try:
            meta = get_type_meta("connectors", db_type)
        except ValueError:
            return None

        if not meta.module_path:
            return None

        # Validate the config through the Pydantic model
        adapter_config = meta.model(**{**row_config, "enabled": True})

        module = importlib.import_module(meta.module_path)
        cls = getattr(module, meta.class_name)
        return cls(adapter_config, self._queue)

    def _build_handler_from_row(
        self, db_type: str, row_config: dict[str, Any],
    ) -> Handler | None:
        """Build a handler directly from a database row's config dict.

        Uses the registry's ``module_path`` / ``class_name``.  Returns
        ``None`` for non-handler service types (LLM, guardrails, etc.)
        which have empty ``module_path``.
        """
        import importlib

        from oasisagent.db.registry import get_type_meta

        try:
            meta = get_type_meta("core_services", db_type)
        except ValueError:
            return None

        if not meta.module_path:
            return None

        config = meta.model(**{**row_config, "enabled": True})
        module = importlib.import_module(meta.module_path)
        cls = getattr(module, meta.class_name)
        return cls(config)

    def _build_notification_from_row(
        self, db_type: str, row_config: dict[str, Any],
    ) -> NotificationChannel | None:
        """Build a notification channel directly from a database row's config.

        Uses the registry's ``module_path`` / ``class_name``.  Returns
        ``None`` for unknown types.
        """
        import importlib

        from oasisagent.db.registry import get_type_meta

        try:
            meta = get_type_meta("notification_channels", db_type)
        except ValueError:
            return None

        if not meta.module_path:
            return None

        config = meta.model(**{**row_config, "enabled": True})
        module = importlib.import_module(meta.module_path)
        cls = getattr(module, meta.class_name)
        return cls(config)

    def _build_adapter(
        self, db_type: str, config: OasisAgentConfig,
    ) -> IngestAdapter | None:
        """Build a single ingestion adapter by DB type from the given config.

        Returns None if the adapter type is not enabled in the config.
        """
        assert self._queue is not None
        cfg = config

        if db_type == "mqtt" and cfg.ingestion.mqtt.enabled:
            return MqttAdapter(cfg.ingestion.mqtt, self._queue)
        if db_type == "ha_websocket" and cfg.ingestion.ha_websocket.enabled:
            return HaWebSocketAdapter(cfg.ingestion.ha_websocket, self._queue)
        if db_type == "ha_log_poller" and cfg.ingestion.ha_log_poller.enabled:
            return HaLogPollerAdapter(cfg.ingestion.ha_log_poller, self._queue)
        if db_type == "http_poller" and cfg.ingestion.http_poller_targets:
            return HttpPollerAdapter(cfg.ingestion.http_poller_targets, self._queue)
        if db_type == "uptime_kuma" and cfg.ingestion.uptime_kuma.enabled:
            from oasisagent.ingestion.uptime_kuma import UptimeKumaAdapter
            return UptimeKumaAdapter(cfg.ingestion.uptime_kuma, self._queue)
        if db_type == "unifi" and cfg.ingestion.unifi.enabled:
            from oasisagent.ingestion.unifi import UnifiAdapter
            return UnifiAdapter(cfg.ingestion.unifi, self._queue)
        if db_type == "cloudflare" and cfg.ingestion.cloudflare.enabled:
            from oasisagent.ingestion.cloudflare import CloudflareAdapter
            return CloudflareAdapter(cfg.ingestion.cloudflare, self._queue)
        if db_type == "npm" and cfg.ingestion.npm.enabled:
            from oasisagent.ingestion.npm import NpmAdapter
            return NpmAdapter(cfg.ingestion.npm, self._queue)
        if db_type == "frigate" and cfg.ingestion.frigate.enabled:
            from oasisagent.ingestion.frigate import FrigateAdapter
            return FrigateAdapter(cfg.ingestion.frigate, self._queue)
        if db_type == "n8n" and cfg.ingestion.n8n.enabled:
            from oasisagent.ingestion.n8n import N8nAdapter
            return N8nAdapter(cfg.ingestion.n8n, self._queue)
        if db_type == "vaultwarden" and cfg.ingestion.vaultwarden.enabled:
            from oasisagent.ingestion.vaultwarden import VaultwardenAdapter
            return VaultwardenAdapter(cfg.ingestion.vaultwarden, self._queue)
        if db_type == "overseerr" and cfg.ingestion.overseerr.enabled:
            from oasisagent.ingestion.overseerr import OverseerrAdapter
            return OverseerrAdapter(cfg.ingestion.overseerr, self._queue)
        if db_type == "qbittorrent" and cfg.ingestion.qbittorrent.enabled:
            from oasisagent.ingestion.qbittorrent import QBittorrentAdapter
            return QBittorrentAdapter(cfg.ingestion.qbittorrent, self._queue)
        if db_type == "plex" and cfg.ingestion.plex.enabled:
            from oasisagent.ingestion.plex import PlexAdapter
            return PlexAdapter(cfg.ingestion.plex, self._queue)
        if db_type == "tautulli" and cfg.ingestion.tautulli.enabled:
            from oasisagent.ingestion.tautulli import TautulliAdapter
            return TautulliAdapter(cfg.ingestion.tautulli, self._queue)
        if db_type == "tdarr" and cfg.ingestion.tdarr.enabled:
            from oasisagent.ingestion.tdarr import TdarrAdapter
            return TdarrAdapter(cfg.ingestion.tdarr, self._queue)
        if db_type == "servarr":
            # Servarr uses a list config — find the enabled entry
            for servarr_cfg in cfg.ingestion.servarr:
                if servarr_cfg.enabled:
                    from oasisagent.ingestion.servarr import ServarrAdapter
                    return ServarrAdapter(servarr_cfg, self._queue)

        return None

    def _build_handler(
        self, handler_name: str, config: OasisAgentConfig,
    ) -> Handler | None:
        """Build a single handler by name from the given config.

        Returns None if the handler is not enabled in the config.
        """
        cfg = config

        if handler_name == "homeassistant" and cfg.handlers.homeassistant.enabled:
            return HomeAssistantHandler(cfg.handlers.homeassistant)
        if handler_name == "docker" and cfg.handlers.docker.enabled:
            return DockerHandler(cfg.handlers.docker)
        if handler_name == "portainer" and cfg.handlers.portainer.enabled:
            from oasisagent.handlers.portainer import PortainerHandler
            return PortainerHandler(cfg.handlers.portainer)
        if handler_name == "proxmox" and cfg.handlers.proxmox.enabled:
            from oasisagent.handlers.proxmox import ProxmoxHandler
            return ProxmoxHandler(cfg.handlers.proxmox)
        if handler_name == "unifi":
            from oasisagent.handlers.unifi import UniFiHandler
            if cfg.handlers.unifi.enabled:
                return UniFiHandler(cfg.handlers.unifi)
        if handler_name == "cloudflare":
            from oasisagent.handlers.cloudflare import CloudflareHandler
            if cfg.handlers.cloudflare.enabled:
                return CloudflareHandler(cfg.handlers.cloudflare)

        return None

    def _build_notification_channel(
        self, db_type: str, config: OasisAgentConfig,
    ) -> NotificationChannel | None:
        """Build a single notification channel by DB type from the given config.

        Returns None if the channel type is not enabled in the config.
        """
        cfg = config

        if db_type == "mqtt_notification" and cfg.notifications.mqtt.enabled:
            return MqttNotificationChannel(cfg.notifications.mqtt)
        if db_type == "email" and cfg.notifications.email.enabled:
            from oasisagent.notifications.email import EmailNotificationChannel
            return EmailNotificationChannel(cfg.notifications.email)
        if db_type == "webhook" and cfg.notifications.webhook.enabled:
            from oasisagent.notifications.webhook import WebhookNotificationChannel
            return WebhookNotificationChannel(cfg.notifications.webhook)
        if db_type == "telegram" and cfg.notifications.telegram.enabled:
            from oasisagent.notifications.telegram import TelegramNotificationChannel
            return TelegramNotificationChannel(cfg.notifications.telegram)

        return None

    async def stop(self) -> None:
        """Signal shutdown and tear down all components.

        Safe to call multiple times. Called by FastAPI lifespan shutdown
        or by ``run()`` in standalone mode.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutdown requested")
        await self._shutdown()

    # -------------------------------------------------------------------
    # Component construction
    # -------------------------------------------------------------------

    def _build_infrastructure(self) -> None:
        """Instantiate system singletons from config. No I/O — just wiring.

        These are infrastructure components that are NOT configurable
        per-component via the UI. They are always built from
        ``self._config`` (the OasisAgentConfig from ``store.load_config()``).
        """
        cfg = self._config

        # 1. Event queue
        self._queue = EventQueue(
            max_size=cfg.agent.event_queue_size,
            dedup_window_seconds=cfg.agent.dedup_window_seconds,
        )

        # 2. Event correlator
        self._correlator = EventCorrelator(
            window_seconds=cfg.agent.correlation_window,
        )

        # 3. Known fixes registry
        self._registry = KnownFixRegistry()
        fixes_dir = Path(cfg.agent.known_fixes_dir)
        if fixes_dir.exists():
            self._registry.load(fixes_dir)
        else:
            logger.warning("Known fixes directory not found: %s", fixes_dir)

        # 4. Circuit breaker
        self._circuit_breaker = CircuitBreaker(cfg.guardrails.circuit_breaker)

        # 5. Guardrails engine
        self._guardrails = GuardrailsEngine(cfg.guardrails)

        # 6. LLM client (stateless)
        self._llm_client = LLMClient(cfg.llm)

        # 7. Triage service
        self._triage_service = TriageService(self._llm_client)

        # 8. Reasoning service (T2)
        self._reasoning_service = ReasoningService(self._llm_client)

        # 9. Decision engine
        self._decision_engine = DecisionEngine(
            registry=self._registry,
            guardrails=self._guardrails,
            triage_service=self._triage_service,
            reasoning_service=self._reasoning_service,
        )

        # 10. Audit writer
        self._audit = AuditWriter(cfg.audit)

        # 11. Pending action queue (in-memory; upgraded to persistent in start())
        self._pending_queue = PendingQueue()

        # 12. Metrics server (Prometheus)
        if cfg.agent.metrics_port > 0:
            from oasisagent import metrics as metrics_mod

            self._metrics_server = MetricsServer(cfg.agent.metrics_port)
            metrics_mod.set_callback_sources(self._queue, self._pending_queue)

    def _build_components(self) -> None:
        """Build all components from config (backward-compat for tests).

        Equivalent to ``_build_infrastructure()`` +
        ``_build_components_from_config()``.
        """
        self._build_infrastructure()
        self._build_components_from_config()

    def _build_components_from_config(self) -> None:
        """Build user-configurable components from OasisAgentConfig.

        This is the sync fallback for standalone mode (no DB / tests).
        Only builds adapters, handlers, and notifications — infrastructure
        singletons must already be built by ``_build_infrastructure()``.
        """
        cfg = self._config
        assert self._queue is not None

        # --- Handlers ---
        if cfg.handlers.homeassistant.enabled:
            ha = HomeAssistantHandler(cfg.handlers.homeassistant)
            self._handlers[ha.name()] = ha
        if cfg.handlers.docker.enabled:
            docker = DockerHandler(cfg.handlers.docker)
            self._handlers[docker.name()] = docker
        if cfg.handlers.portainer.enabled:
            from oasisagent.handlers.portainer import PortainerHandler

            portainer = PortainerHandler(cfg.handlers.portainer)
            self._handlers[portainer.name()] = portainer
        if cfg.handlers.proxmox.enabled:
            from oasisagent.handlers.proxmox import ProxmoxHandler

            proxmox = ProxmoxHandler(cfg.handlers.proxmox)
            self._handlers[proxmox.name()] = proxmox
        if cfg.handlers.unifi.enabled:
            from oasisagent.handlers.unifi import UnifiHandler

            unifi_h = UnifiHandler(cfg.handlers.unifi)
            self._handlers[unifi_h.name()] = unifi_h
        if cfg.handlers.cloudflare.enabled:
            from oasisagent.handlers.cloudflare import CloudflareHandler

            cf_h = CloudflareHandler(cfg.handlers.cloudflare)
            self._handlers[cf_h.name()] = cf_h

        # --- Notification channels ---
        channels: list[NotificationChannel] = []
        if cfg.notifications.mqtt.enabled:
            channels.append(MqttNotificationChannel(cfg.notifications.mqtt))
        if cfg.notifications.email.enabled:
            from oasisagent.notifications.email import EmailNotificationChannel

            channels.append(EmailNotificationChannel(cfg.notifications.email))
        if cfg.notifications.webhook.enabled:
            from oasisagent.notifications.webhook import WebhookNotificationChannel

            channels.append(WebhookNotificationChannel(cfg.notifications.webhook))
        if cfg.notifications.telegram.enabled:
            from oasisagent.notifications.telegram import TelegramChannel

            channels.append(TelegramChannel(cfg.notifications.telegram))
        if cfg.notifications.discord.enabled:
            from oasisagent.notifications.discord import DiscordNotificationChannel

            channels.append(DiscordNotificationChannel(cfg.notifications.discord))
        if cfg.notifications.slack.enabled:
            from oasisagent.notifications.slack import SlackNotificationChannel

            channels.append(SlackNotificationChannel(cfg.notifications.slack))
        self._dispatcher = NotificationDispatcher(channels)

        # --- Approval listener ---
        if cfg.ingestion.mqtt.enabled:
            self._approval_listener = ApprovalListener(
                config=cfg.ingestion.mqtt,
                on_approve=self._handle_approval,
                on_reject=self._handle_rejection,
            )

        # --- Ingestion adapters ---
        if cfg.ingestion.mqtt.enabled:
            self._adapters.append(MqttAdapter(cfg.ingestion.mqtt, self._queue))
        if cfg.ingestion.ha_websocket.enabled:
            logger.info("Starting HA WebSocket adapter: url=%s", cfg.ingestion.ha_websocket.url)
            self._adapters.append(
                HaWebSocketAdapter(cfg.ingestion.ha_websocket, self._queue)
            )
        if cfg.ingestion.ha_log_poller.enabled:
            logger.info("Starting HA Log Poller adapter: url=%s", cfg.ingestion.ha_log_poller.url)
            self._adapters.append(
                HaLogPollerAdapter(cfg.ingestion.ha_log_poller, self._queue)
            )
        if cfg.ingestion.http_poller_targets:
            self._adapters.append(
                HttpPollerAdapter(cfg.ingestion.http_poller_targets, self._queue)
            )
        if cfg.ingestion.unifi.enabled:
            from oasisagent.ingestion.unifi import UnifiAdapter

            self._adapters.append(
                UnifiAdapter(cfg.ingestion.unifi, self._queue)
            )
        if cfg.ingestion.cloudflare.enabled:
            from oasisagent.ingestion.cloudflare import CloudflareAdapter

            self._adapters.append(
                CloudflareAdapter(cfg.ingestion.cloudflare, self._queue)
            )
        if cfg.ingestion.uptime_kuma.enabled:
            from oasisagent.ingestion.uptime_kuma import UptimeKumaAdapter

            self._adapters.append(
                UptimeKumaAdapter(cfg.ingestion.uptime_kuma, self._queue)
            )

        # --- Scanners ---
        self._build_scanners_from_config(cfg)

    def _build_scanners_from_config(self, cfg: OasisAgentConfig) -> None:
        """Build scanner adapters from config (config-driven fallback)."""
        assert self._queue is not None
        if not cfg.scanner.enabled:
            return

        adaptive_kw = {
            "adaptive_enabled": cfg.scanner.adaptive_enabled,
            "adaptive_fast_factor": cfg.scanner.adaptive_fast_factor,
            "adaptive_recovery_scans": cfg.scanner.adaptive_recovery_scans,
        }
        if cfg.scanner.certificate_expiry.enabled:
            from oasisagent.scanner.cert_expiry import CertExpiryScannerAdapter

            interval = cfg.scanner.certificate_expiry.interval or cfg.scanner.interval
            self._adapters.append(CertExpiryScannerAdapter(
                cfg.scanner.certificate_expiry, self._queue, interval,
                **adaptive_kw,
            ))
        if cfg.scanner.disk_space.enabled:
            from oasisagent.scanner.disk_space import DiskSpaceScannerAdapter

            interval = cfg.scanner.disk_space.interval or cfg.scanner.interval
            self._adapters.append(DiskSpaceScannerAdapter(
                cfg.scanner.disk_space, self._queue, interval,
                **adaptive_kw,
            ))
        if cfg.scanner.ha_health.enabled and cfg.handlers.homeassistant.enabled:
            from oasisagent.scanner.ha_health import HaHealthScannerAdapter

            interval = cfg.scanner.ha_health.interval or cfg.scanner.interval
            self._adapters.append(HaHealthScannerAdapter(
                cfg.scanner.ha_health, self._queue, interval,
                ha_config=cfg.handlers.homeassistant,
                **adaptive_kw,
            ))
        if cfg.scanner.docker_health.enabled and cfg.handlers.docker.enabled:
            from oasisagent.scanner.docker_health import DockerHealthScannerAdapter

            interval = cfg.scanner.docker_health.interval or cfg.scanner.interval
            self._adapters.append(DockerHealthScannerAdapter(
                cfg.scanner.docker_health, self._queue, interval,
                docker_config=cfg.handlers.docker,
                **adaptive_kw,
            ))
        if cfg.scanner.backup_freshness.enabled:
            from oasisagent.scanner.backup_freshness import (
                BackupFreshnessScannerAdapter,
            )

            interval = (
                cfg.scanner.backup_freshness.interval or cfg.scanner.interval
            )
            self._adapters.append(BackupFreshnessScannerAdapter(
                cfg.scanner.backup_freshness, self._queue, interval,
            ))

    async def _build_db_components(self) -> None:
        """Build all user-configurable components from SQLite tables.

        Queries ``connectors``, ``core_services``, and
        ``notification_channels`` tables directly. Per-row try/except so
        one bad config row never blocks others from starting.
        """
        store = self._config_store
        assert store is not None
        assert self._queue is not None

        from oasisagent.db.registry import get_type_meta

        # === Pass 1: Connectors ===
        connector_rows = await store.list_connectors()
        http_poller_configs: list[dict[str, Any]] = []

        for row in connector_rows:
            if not row["enabled"]:
                continue
            if row["type"] == "http_poller":
                http_poller_configs.append(row["config"])
                continue
            try:
                adapter = self._build_adapter_from_row(row["type"], row["config"])
                if adapter is not None:
                    self._adapters.append(adapter)
            except Exception:
                logger.exception(
                    "Failed to build adapter %s (id=%d)", row["type"], row["id"],
                )

        # HTTP poller is many-to-one: all rows become one adapter
        if http_poller_configs:
            try:
                from oasisagent.config import HttpPollerTargetConfig

                targets = [
                    HttpPollerTargetConfig(**{**cfg, "enabled": True})
                    for cfg in http_poller_configs
                ]
                self._adapters.append(HttpPollerAdapter(targets, self._queue))
            except Exception:
                logger.exception("Failed to build http_poller adapter")

        # === Pass 2: Handlers (before scanners) ===
        service_rows = await store.list_services()
        handler_db_types = set(self._HANDLER_NAME_TO_TYPE.values())

        for row in service_rows:
            if not row["enabled"] or row["type"] not in handler_db_types:
                continue
            try:
                handler = self._build_handler_from_row(row["type"], row["config"])
                if handler is not None:
                    self._handlers[handler.name()] = handler
            except Exception:
                logger.exception(
                    "Failed to build handler %s (id=%d)", row["type"], row["id"],
                )

        # === Pass 3: Scanners (after handlers — needs handler configs) ===
        await self._build_scanners_from_db(service_rows)

        # === Pass 4: Notification channels ===
        notification_rows = await store.list_notifications()
        channels: list[NotificationChannel] = []
        for row in notification_rows:
            if not row["enabled"]:
                continue
            try:
                channel = self._build_notification_from_row(
                    row["type"], row["config"],
                )
                if channel is not None:
                    channels.append(channel)
            except Exception:
                logger.exception(
                    "Failed to build notification %s (id=%d)",
                    row["type"], row["id"],
                )
        self._dispatcher = NotificationDispatcher(channels)

        # === Pass 5: Approval listener ===
        mqtt_rows = [
            r for r in connector_rows
            if r["type"] == "mqtt" and r["enabled"]
        ]
        if len(mqtt_rows) > 1:
            logger.warning(
                "Multiple MQTT connectors enabled (%d) — using first for approval listener",
                len(mqtt_rows),
            )
        if mqtt_rows:
            try:
                meta = get_type_meta("connectors", "mqtt")
                mqtt_config = meta.model(**{**mqtt_rows[0]["config"], "enabled": True})
                self._approval_listener = ApprovalListener(
                    config=mqtt_config,
                    on_approve=self._handle_approval,
                    on_reject=self._handle_rejection,
                )
            except Exception:
                logger.exception("Failed to build approval listener from MQTT row")

    async def _build_scanners_from_db(
        self, service_rows: list[dict[str, Any]],
    ) -> None:
        """Build scanner adapters from the 'scanner' service row.

        Scanners need handler configs for cross-references (ha_health needs
        ha_handler config, docker_health needs docker_handler config), so
        this runs after handlers are built (Pass 3).
        """
        assert self._queue is not None

        from oasisagent.config import (
            DockerHandlerConfig,
            HaHandlerConfig,
            ScannerConfig,
        )

        # Find the scanner service row
        scanner_row = None
        for row in service_rows:
            if row["type"] == "scanner" and row["enabled"]:
                scanner_row = row
                break
        if scanner_row is None:
            return

        try:
            scanner_cfg = ScannerConfig(**{**scanner_row["config"], "enabled": True})
        except Exception:
            logger.exception("Failed to parse scanner config (id=%d)", scanner_row["id"])
            return

        adaptive_kw = {
            "adaptive_enabled": scanner_cfg.adaptive_enabled,
            "adaptive_fast_factor": scanner_cfg.adaptive_fast_factor,
            "adaptive_recovery_scans": scanner_cfg.adaptive_recovery_scans,
        }

        if scanner_cfg.certificate_expiry.enabled:
            try:
                from oasisagent.scanner.cert_expiry import CertExpiryScannerAdapter

                interval = scanner_cfg.certificate_expiry.interval or scanner_cfg.interval
                self._adapters.append(CertExpiryScannerAdapter(
                    scanner_cfg.certificate_expiry, self._queue, interval,
                    **adaptive_kw,
                ))
            except Exception:
                logger.exception("Failed to build cert_expiry scanner")

        if scanner_cfg.disk_space.enabled:
            try:
                from oasisagent.scanner.disk_space import DiskSpaceScannerAdapter

                interval = scanner_cfg.disk_space.interval or scanner_cfg.interval
                self._adapters.append(DiskSpaceScannerAdapter(
                    scanner_cfg.disk_space, self._queue, interval,
                    **adaptive_kw,
                ))
            except Exception:
                logger.exception("Failed to build disk_space scanner")

        # ha_health needs the ha_handler config
        if scanner_cfg.ha_health.enabled:
            ha_config = self._find_handler_config(service_rows, "ha_handler", HaHandlerConfig)
            if ha_config is not None:
                try:
                    from oasisagent.scanner.ha_health import HaHealthScannerAdapter

                    interval = scanner_cfg.ha_health.interval or scanner_cfg.interval
                    self._adapters.append(HaHealthScannerAdapter(
                        scanner_cfg.ha_health, self._queue, interval,
                        ha_config=ha_config,
                        **adaptive_kw,
                    ))
                except Exception:
                    logger.exception("Failed to build ha_health scanner")
            else:
                logger.warning(
                    "ha_health scanner enabled but no enabled ha_handler found — skipping",
                )

        # docker_health needs the docker_handler config
        if scanner_cfg.docker_health.enabled:
            docker_config = self._find_handler_config(
                service_rows, "docker_handler", DockerHandlerConfig,
            )
            if docker_config is not None:
                try:
                    from oasisagent.scanner.docker_health import DockerHealthScannerAdapter

                    interval = scanner_cfg.docker_health.interval or scanner_cfg.interval
                    self._adapters.append(DockerHealthScannerAdapter(
                        scanner_cfg.docker_health, self._queue, interval,
                        docker_config=docker_config,
                        **adaptive_kw,
                    ))
                except Exception:
                    logger.exception("Failed to build docker_health scanner")
            else:
                logger.warning(
                    "docker_health scanner enabled but no enabled docker_handler found — skipping",
                )

        if scanner_cfg.backup_freshness.enabled:
            try:
                from oasisagent.scanner.backup_freshness import (
                    BackupFreshnessScannerAdapter,
                )

                interval = (
                    scanner_cfg.backup_freshness.interval or scanner_cfg.interval
                )
                self._adapters.append(BackupFreshnessScannerAdapter(
                    scanner_cfg.backup_freshness, self._queue, interval,
                ))
            except Exception:
                logger.exception("Failed to build backup_freshness scanner")

    @staticmethod
    def _find_handler_config(
        service_rows: list[dict[str, Any]],
        handler_type: str,
        config_cls: type,
    ) -> object | None:
        """Find an enabled handler row and return its validated config."""
        for row in service_rows:
            if row["type"] == handler_type and row["enabled"]:
                try:
                    return config_cls(**{**row["config"], "enabled": True})
                except Exception:
                    return None
        return None

    # -------------------------------------------------------------------
    # Component lifecycle
    # -------------------------------------------------------------------

    async def _start_components(self) -> None:
        """Start components in dependency order."""
        # Handlers first — they must be ready before events arrive
        for name, handler in self._handlers.items():
            try:
                await handler.start()
                logger.info("Handler started: %s", name)
            except Exception:
                logger.exception("Failed to start handler: %s", name)

        # Audit writer
        assert self._audit is not None
        try:
            await self._audit.start()
        except Exception:
            logger.exception("Failed to start audit writer")

        # Notification dispatcher
        assert self._dispatcher is not None
        try:
            await self._dispatcher.start()
        except Exception:
            logger.exception("Failed to start notification dispatcher")

        # LLM warm-up — probe each endpoint to establish initial health
        if self._llm_client is not None:
            try:
                await self._llm_client.warm_up()
            except Exception:
                logger.exception("LLM warm-up failed unexpectedly")

        # Approval listener — background task
        if self._approval_listener is not None:
            self._approval_listener_task = asyncio.create_task(
                self._run_approval_listener(),
                name="approval-listener",
            )
            logger.info("Approval listener started")

        # Interactive notification channel listeners (e.g., Telegram polling)
        for channel in self._dispatcher.interactive_channels:
            try:
                await channel.start_listener(self._handle_interactive_approval)
                logger.info("Interactive listener started: %s", channel.name())
            except Exception:
                logger.exception(
                    "Failed to start interactive listener: %s", channel.name()
                )

        # Metrics server
        if self._metrics_server is not None:
            try:
                await self._metrics_server.start()
            except Exception:
                logger.exception("Failed to start metrics server")

        # Service topology — load graph from DB
        if self._db is not None:
            try:
                self._topology_store = TopologyStore(self._db)
                self._service_graph = ServiceGraph()
                await self._service_graph.load_from_db(self._topology_store)
                self._cross_correlator = CrossDomainCorrelator(
                    graph=self._service_graph,
                    db=self._db,
                    window_seconds=self._config.agent.correlation_window,
                )
                logger.info("Service graph and cross-correlator loaded")
            except Exception:
                logger.exception("Failed to load service graph")

        # Ingestion adapters — launched as background tasks
        for adapter in self._adapters:
            task = asyncio.create_task(
                self._run_adapter(adapter), name=f"adapter-{adapter.name}"
            )
            self._adapter_tasks.append(task)
            logger.info("Ingestion adapter started: %s", adapter.name)

        # Topology discovery — runs after adapters are launched
        if self._topology_store is not None and self._service_graph is not None:
            task = asyncio.create_task(
                self._run_topology_discovery(),
                name="topology-discovery",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _run_adapter(self, adapter: IngestAdapter) -> None:
        """Run an ingestion adapter. Logs exceptions but never propagates."""
        try:
            await adapter.start()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "Ingestion adapter %s crashed", adapter.name
            )

    async def _run_approval_listener(self) -> None:
        """Run the approval listener. Logs exceptions but never propagates."""
        try:
            assert self._approval_listener is not None
            await self._approval_listener.start()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Approval listener crashed")

    async def _run_topology_discovery(self) -> None:
        """Periodically discover topology from all adapters.

        Runs on startup (after a short delay for adapters to connect)
        and then at ``agent.discovery_interval`` seconds. Merges
        discovered nodes/edges into the service graph and persists
        to SQLite.
        """
        assert self._service_graph is not None
        assert self._topology_store is not None

        interval = self._config.agent.discovery_interval

        # Wait for adapters to establish connections
        await asyncio.sleep(30)

        while not self._shutting_down:
            try:
                all_nodes = []
                all_edges = []
                for adapter in self._adapters:
                    try:
                        nodes, edges = await adapter.discover_topology()
                        all_nodes.extend(nodes)
                        all_edges.extend(edges)
                    except Exception:
                        logger.debug(
                            "Topology discovery failed for %s", adapter.name
                        )

                if all_nodes or all_edges:
                    diffs = await self._service_graph.merge_discovered(
                        all_nodes, all_edges, self._topology_store,
                    )
                    if diffs:
                        logger.info(
                            "Topology discovery: %d changes (%d nodes, %d edges)",
                            len(diffs),
                            len(all_nodes),
                            len(all_edges),
                        )

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Topology discovery cycle failed")

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    async def _shutdown(self) -> None:
        """Tear down components in reverse dependency order."""
        self._shutting_down = True

        # 1. Stop approval listener
        if self._approval_listener is not None:
            try:
                await self._approval_listener.stop()
            except Exception:
                logger.exception("Error stopping approval listener")
        if self._approval_listener_task is not None:
            self._approval_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._approval_listener_task

        # 1b. Stop interactive notification channel listeners
        if self._dispatcher is not None:
            for channel in self._dispatcher.interactive_channels:
                try:
                    await channel.stop_listener()
                except Exception:
                    logger.exception(
                        "Error stopping interactive listener: %s", channel.name()
                    )

        # 2. Expire all remaining pending actions
        if self._pending_queue is not None:
            for pending in self._pending_queue.list_pending():
                await self._pending_queue.reject(pending.id)
            logger.info("Rejected all remaining pending actions on shutdown")

        # 3. Stop ingestion adapters
        for adapter in self._adapters:
            try:
                await adapter.stop()
            except Exception:
                logger.exception("Error stopping adapter %s", adapter.name)

        # Cancel adapter tasks
        for task in self._adapter_tasks:
            task.cancel()
        if self._adapter_tasks:
            await asyncio.gather(*self._adapter_tasks, return_exceptions=True)

        # 4. Drain queue with timeout
        assert self._queue is not None
        timeout = self._config.agent.shutdown_timeout
        deadline = time.monotonic() + timeout
        remaining = self._queue.drain()
        for event in remaining:
            if time.monotonic() > deadline:
                logger.warning(
                    "Shutdown timeout — dropping %d remaining events",
                    len(remaining) - remaining.index(event),
                )
                break
            await self._process_one(event)

        # 5. Stop handlers
        for name, handler in self._handlers.items():
            try:
                await handler.stop()
            except Exception:
                logger.exception("Error stopping handler %s", name)

        # 6. Stop notification dispatcher
        if self._dispatcher is not None:
            try:
                await self._dispatcher.stop()
            except Exception:
                logger.exception("Error stopping notification dispatcher")

        # 7. Stop metrics server
        if self._metrics_server is not None:
            try:
                await self._metrics_server.stop()
            except Exception:
                logger.exception("Error stopping metrics server")

        # 8. Stop audit writer
        if self._audit is not None:
            try:
                await self._audit.stop()
            except Exception:
                logger.exception("Error stopping audit writer")

        # 9. Final stats flush + stop
        if self._stats_store is not None:
            try:
                await self._stats_store.flush(
                    events_processed=self._events_processed,
                    actions_taken=self._actions_taken,
                    errors=self._errors,
                )
                await self._stats_store.stop()
            except Exception:
                logger.exception("Error flushing stats on shutdown")

        # 10. Log final stats
        logger.info(
            "OasisAgent stopped — events=%d, actions=%d, errors=%d",
            self._events_processed,
            self._actions_taken,
            self._errors,
        )

    # -------------------------------------------------------------------
    # Signal handling
    # -------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Register SIGTERM/SIGINT handlers to trigger graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler)

    def _signal_handler(self) -> None:
        """Called on SIGTERM/SIGINT — sets the shutdown flag."""
        logger.info("Received shutdown signal")
        self._shutting_down = True

    def _schedule_task(self, coro: Coroutine[None, None, None]) -> None:
        """Schedule a coroutine as a background task with reference tracking."""
        task = asyncio.get_event_loop().create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # -------------------------------------------------------------------
    # Event processing pipeline
    # -------------------------------------------------------------------

    async def _process_one(self, event: Event) -> None:
        """Process a single event through the full pipeline.

        Errors are caught per-event — one failure never kills the loop.
        """
        start = time.monotonic()
        try:
            # 1. TTL check
            if self._is_expired(event):
                logger.info("Event %s expired (TTL), dropping", event.id)
                return

            # 1b. Repeated-event suppression — drop before any processing
            suppressed_count = self._suppression.check(event)
            if suppressed_count:
                logger.debug(
                    "Event %s suppressed (repeated %s for %s, count=%d)",
                    event.id,
                    event.event_type,
                    event.entity_id,
                    suppressed_count,
                )
                if self._audit is not None:
                    await self._audit.write_suppression(event, suppressed_count)
                return

            # 1c. Metrics: count ingested event
            _inc_events(event.source, event.severity.value)

            # 2. Correlation check
            assert self._correlator is not None
            correlation = self._correlator.check(event)
            event.metadata.correlation_id = correlation.correlation_id

            if not correlation.is_leader:
                correlated_result = DecisionResult(
                    event_id=event.id,
                    tier=DecisionTier.T0,
                    disposition=DecisionDisposition.CORRELATED,
                    diagnosis=(
                        f"Correlated with leader event {correlation.leader_event_id}"
                    ),
                    details={"leader_event_id": correlation.leader_event_id},
                )
                await self._audit_decision(event, correlated_result)
                self._events_processed += 1
                _observe_processing_time("t0", time.monotonic() - start)
                logger.debug(
                    "Event %s correlated with leader %s, skipping",
                    event.id,
                    correlation.leader_event_id,
                )
                return

            # 3. Decision (T0 → T1 → T2, guardrails applied)
            assert self._decision_engine is not None
            result = await self._decision_engine.process_event(event)

            duration_ms = (time.monotonic() - start) * 1000

            # 3. Audit the decision (best-effort)
            await self._audit_decision(event, result)

            # 4. Handler dispatch
            # T2 results may carry multiple recommended_actions, each
            # independently approved by guardrails in the decision engine.
            if result.recommended_actions and result.disposition == DecisionDisposition.MATCHED:
                await self._dispatch_t2_actions(event, result, duration_ms)
            elif self._should_enqueue(result):
                await self._enqueue_pending(event, result)
            elif self._should_execute(result):
                action_result = await self._dispatch_handler(event, result)
                await self._audit_action(event, result, action_result, duration_ms)
            elif self._is_dry_run(result):
                # DRY_RUN always notifies, skip _should_notify check
                self._events_processed += 1
                _observe_processing_time(result.tier.value, time.monotonic() - start)
                await self._send_notification(event, result)
                return

            # 5. Notification (if warranted)
            if self._should_notify(result):
                await self._send_notification(event, result)

            self._events_processed += 1
            _observe_processing_time(result.tier.value, time.monotonic() - start)

            # 6. Cross-domain correlation (async, best-effort)
            if self._cross_correlator is not None:
                try:
                    await self._cross_correlator.correlate(event, result)
                except Exception:
                    logger.debug(
                        "Cross-domain correlation failed for event %s",
                        event.id,
                    )

        except Exception:
            logger.exception("Unhandled error processing event %s", event.id)
            self._errors += 1

    # -------------------------------------------------------------------
    # Pipeline helpers
    # -------------------------------------------------------------------

    def _is_expired(self, event: Event) -> bool:
        """Check if an event has exceeded its TTL.

        Per-event TTL (EventMetadata.ttl) takes precedence over the
        global default (agent.event_ttl).
        """
        ttl = event.metadata.ttl if event.metadata.ttl > 0 else self._config.agent.event_ttl
        if ttl <= 0:
            return False
        age = (datetime.now(UTC) - event.timestamp).total_seconds()
        return age > ttl

    def _should_enqueue(self, result: DecisionResult) -> bool:
        """Determine if the result should be enqueued for operator approval.

        RECOMMEND-tier T0 matches go to the pending queue instead of
        auto-executing. This is tier-agnostic — any RECOMMEND action
        from T0, T1, or T2 follows the same path.
        """
        if result.disposition != DecisionDisposition.MATCHED:
            return False
        if result.guardrail_result is None:
            return False
        if not result.guardrail_result.allowed:
            return False
        if result.guardrail_result.dry_run:
            return False
        return result.guardrail_result.risk_tier == RiskTier.RECOMMEND

    def _should_execute(self, result: DecisionResult) -> bool:
        """Determine if the result warrants handler execution.

        Execute only when MATCHED and guardrails allow (AUTO_FIX tier,
        not dry-run, not blocked).
        """
        if result.disposition != DecisionDisposition.MATCHED:
            return False
        if result.guardrail_result is None:
            return False
        if not result.guardrail_result.allowed:
            return False
        if result.guardrail_result.dry_run:
            return False
        return result.guardrail_result.risk_tier == RiskTier.AUTO_FIX

    def _is_dry_run(self, result: DecisionResult) -> bool:
        """Check if the result is a dry-run match."""
        return result.disposition == DecisionDisposition.DRY_RUN

    def _should_notify(self, result: DecisionResult) -> bool:
        """Determine if the result warrants a notification.

        Per dispatch rules table in §15:
        - MATCHED + AUTO_FIX: notify on success or failure
        - MATCHED + RECOMMEND: notify with recommended action
        - MATCHED + ESCALATE: notify with full context
        - MATCHED + BLOCK: no notification (silent deny)
        - DROPPED: no notification
        - ESCALATED (human): notify with T1 context
        - ESCALATED (T2): depends on T2 result (handled inside DecisionEngine)
        """
        if result.disposition == DecisionDisposition.DROPPED:
            return False
        if result.disposition == DecisionDisposition.BLOCKED:
            return False
        if result.disposition == DecisionDisposition.UNMATCHED:
            return False
        return result.disposition in (
            DecisionDisposition.MATCHED,
            DecisionDisposition.ESCALATED,
            DecisionDisposition.DRY_RUN,
        )

    async def _dispatch_handler(
        self, event: Event, result: DecisionResult
    ) -> ActionResult:
        """Find the appropriate handler and execute the action.

        Uses result.matched_fix_id from the DecisionResult rather than
        re-matching against the registry — the decision engine is the
        single source of truth for which fix to apply.
        """
        assert self._circuit_breaker is not None
        assert self._registry is not None

        # Look up the fix the decision engine already matched
        fix = (
            self._registry.get_fix_by_id(result.matched_fix_id)
            if result.matched_fix_id
            else None
        )
        if fix is None:
            return ActionResult(
                status=ActionStatus.SKIPPED,
                error_message="No matching fix found for dispatch",
            )

        handler = self._handlers.get(fix.action.handler)
        if handler is None:
            logger.warning(
                "No handler registered for '%s' (event %s)",
                fix.action.handler,
                event.id,
            )
            return ActionResult(
                status=ActionStatus.SKIPPED,
                error_message=f"Handler '{fix.action.handler}' not registered",
            )

        action = RecommendedAction(
            description=fix.diagnosis,
            handler=fix.action.handler,
            operation=fix.action.operation,
            params=fix.action.details,
            risk_tier=fix.risk_tier,
        )

        try:
            action_result = await handler.execute(event, action)
            success = action_result.status == ActionStatus.SUCCESS
            self._circuit_breaker.record_attempt(
                event.entity_id, success=success
            )
            if success:
                self._actions_taken += 1
                await self._verify_action(handler, event, action, action_result)
            return action_result
        except Exception as exc:
            logger.exception(
                "Handler %s failed for event %s",
                fix.action.handler,
                event.id,
            )
            self._circuit_breaker.record_attempt(
                event.entity_id, success=False
            )
            return ActionResult(
                status=ActionStatus.FAILURE,
                error_message=str(exc),
            )

    async def _dispatch_t2_actions(
        self, event: Event, result: DecisionResult, duration_ms: float
    ) -> None:
        """Dispatch each T2 recommended action independently.

        Each action was already guardrail-checked by the decision engine.
        AUTO_FIX actions execute immediately; RECOMMEND actions are
        enqueued for operator approval. This is tier-agnostic — the
        same RECOMMEND path is used regardless of whether the action
        came from T0, T1, or T2.
        """
        assert self._circuit_breaker is not None
        assert self._pending_queue is not None

        for action in result.recommended_actions:
            cb_entity = action.target_entity_id or event.entity_id

            # RECOMMEND-tier actions go to the pending queue — except
            # notify-only actions which are auto-executed (no state mutation).
            if action.risk_tier == RiskTier.RECOMMEND and action.operation == "notify":
                await self._auto_execute_notify(event, result, action)
                continue

            if action.risk_tier == RiskTier.RECOMMEND:
                pending = await self._pending_queue.add(
                    event_id=event.id,
                    action=action,
                    diagnosis=result.diagnosis,
                    timeout_minutes=self._config.guardrails.approval_timeout_minutes,
                    entity_id=cb_entity,
                    severity=event.severity.value,
                    source=event.source,
                    system=event.system,
                )
                await self._publish_pending_action(pending)
                await self._publish_pending_list()
                continue

            handler = self._handlers.get(action.handler)
            if handler is None:
                logger.warning(
                    "No handler registered for '%s' (T2 action on event %s)",
                    action.handler,
                    event.id,
                )
                continue

            try:
                action_result = await handler.execute(event, action)
                success = action_result.status == ActionStatus.SUCCESS
                self._circuit_breaker.record_attempt(
                    cb_entity, success=success
                )
                if success:
                    self._actions_taken += 1
                    await self._verify_action(handler, event, action, action_result)

                # Audit each action individually
                if action_result.duration_ms is None:
                    action_result = action_result.model_copy(
                        update={"duration_ms": duration_ms}
                    )
                if self._audit is not None:
                    try:
                        await self._audit.write_action(event, action, action_result)
                    except Exception:
                        logger.warning(
                            "Audit action write failed for event %s",
                            event.id,
                            exc_info=True,
                        )
            except Exception:
                logger.exception(
                    "Handler %s failed for T2 action on event %s",
                    action.handler,
                    event.id,
                )
                self._circuit_breaker.record_attempt(
                    cb_entity, success=False
                )

    # -------------------------------------------------------------------
    # Notify auto-execution
    # -------------------------------------------------------------------

    async def _auto_execute_notify(
        self, event: Event, result: DecisionResult, action: RecommendedAction
    ) -> None:
        """Auto-execute a notify action, bypassing the approval queue.

        Notify actions log a message but never mutate system state, so
        requiring operator approval adds no safety value and causes
        infinite enqueue/expire loops when the pending action times out.
        """
        handler = self._handlers.get(action.handler)
        if handler is None:
            logger.warning(
                "No handler registered for '%s' (auto-execute notify, event %s)",
                action.handler,
                event.id,
            )
            return

        logger.info(
            "Auto-executing notify action (no approval needed): %s [event=%s]",
            action.description[:80],
            event.id,
        )

        try:
            action_result = await handler.execute(event, action)
            if action_result.status == ActionStatus.SUCCESS:
                self._actions_taken += 1
        except Exception:
            logger.exception(
                "Auto-execute notify failed for event %s", event.id
            )

        # Still send the notification so it appears in the feed
        await self._send_notification(event, result)

    # -------------------------------------------------------------------
    # Approval queue
    # -------------------------------------------------------------------

    async def _enqueue_pending(
        self, event: Event, result: DecisionResult
    ) -> None:
        """Enqueue a RECOMMEND-tier T0 match for operator approval."""
        assert self._pending_queue is not None
        assert self._registry is not None

        fix = (
            self._registry.get_fix_by_id(result.matched_fix_id)
            if result.matched_fix_id
            else None
        )
        if fix is None:
            return

        action = RecommendedAction(
            description=fix.diagnosis,
            handler=fix.action.handler,
            operation=fix.action.operation,
            params=fix.action.details,
            risk_tier=fix.risk_tier,
        )

        # Notify-only actions don't mutate state — skip the approval queue
        # and execute directly to avoid infinite enqueue/expire loops.
        if action.operation == "notify":
            await self._auto_execute_notify(event, result, action)
            return

        pending = await self._pending_queue.add(
            event_id=event.id,
            action=action,
            diagnosis=result.diagnosis,
            timeout_minutes=self._config.guardrails.approval_timeout_minutes,
            entity_id=event.entity_id,
            severity=event.severity.value,
            source=event.source,
            system=event.system,
        )
        if pending is None:
            logger.debug(
                "Duplicate pending action skipped for event %s", event.id,
            )
            return
        await self._publish_pending_action(pending)
        await self._publish_pending_list()

        # Notify operator about the pending action
        await self._send_notification(event, result)

    async def _handle_interactive_approval(
        self, action_id: str, decision: ApprovalDecision,
    ) -> None:
        """Called by interactive notification channels (Telegram, etc.).

        Routes the decision to the appropriate handler based on the
        operator's choice. Unlike the MQTT callback, this is already
        async so we can call the processors directly.
        """
        if decision == ApprovalDecision.APPROVED:
            await self._process_approval(action_id)
        elif decision == ApprovalDecision.REJECTED:
            await self._process_rejection(action_id)

    def _handle_approval(self, action_id: str) -> None:
        """Called by the approval listener when an action is approved.

        Schedules the async dispatch on the event loop since this
        callback is invoked synchronously from the MQTT message handler.
        """
        self._schedule_task(self._process_approval(action_id))

    def _handle_rejection(self, action_id: str) -> None:
        """Called by the approval listener when an action is rejected.

        Schedules the async processing on the event loop.
        """
        self._schedule_task(self._process_rejection(action_id))

    async def _process_approval(self, action_id: str) -> None:
        """Dispatch an approved pending action through the handler pipeline."""
        assert self._pending_queue is not None
        assert self._circuit_breaker is not None

        pending = await self._pending_queue.approve(action_id)
        if pending is None:
            return

        # Clear the retained MQTT message for this action
        await self._clear_pending_mqtt(action_id)
        await self._publish_pending_list()

        handler = self._handlers.get(pending.action.handler)
        if handler is None:
            logger.warning(
                "No handler registered for '%s' (approved action %s)",
                pending.action.handler,
                action_id,
            )
            return

        # Build a minimal Event for dispatch context.
        # TODO: PendingAction should carry entity_id from the original event
        # so the circuit breaker correlates approved action failures with the
        # entity's failure budget. Same backlog bucket as target_entity_id
        # on RecommendedAction (issue #19).
        from oasisagent.models import Event, Severity

        event = Event(
            id=pending.event_id,
            source="approval_queue",
            system="oasisagent",
            event_type="approved_action",
            entity_id=f"pending:{action_id}",
            severity=Severity.INFO,
            timestamp=pending.created_at,
        )

        try:
            action_result = await handler.execute(event, pending.action)
            success = action_result.status == ActionStatus.SUCCESS
            self._circuit_breaker.record_attempt(
                event.entity_id, success=success
            )
            if success:
                self._actions_taken += 1
                await self._verify_action(
                    handler, event, pending.action, action_result
                )
            logger.info(
                "Approved action %s executed: status=%s",
                action_id,
                action_result.status,
            )
        except Exception:
            logger.exception(
                "Handler failed for approved action %s", action_id
            )
            self._circuit_breaker.record_attempt(
                event.entity_id, success=False
            )

    async def _process_rejection(self, action_id: str) -> None:
        """Record a rejected pending action."""
        assert self._pending_queue is not None

        pending = await self._pending_queue.reject(action_id)
        if pending is None:
            return

        # Clear the retained MQTT message
        await self._clear_pending_mqtt(action_id)
        await self._publish_pending_list()

        logger.info("Action %s rejected by operator", action_id)

    async def _expire_stale_actions(self) -> None:
        """Sweep for expired pending actions and notify."""
        if self._pending_queue is None:
            return

        expired = await self._pending_queue.expire_stale()
        for pending in expired:
            self._schedule_task(self._notify_expired(pending))

    async def _prune_notifications(self) -> None:
        """Prune old notifications and archive to InfluxDB."""
        if self._notification_store is None:
            return

        try:
            archived = await self._notification_store.prune()
            if archived and self._audit is not None:
                for row in archived:
                    try:
                        await self._audit.write_notification_archive(row)
                    except Exception:
                        logger.debug(
                            "Failed to archive notification %s to InfluxDB",
                            row.get("id", "?"),
                        )
        except Exception:
            logger.warning("Notification pruning failed", exc_info=True)

    async def _notify_expired(self, pending: PendingAction) -> None:
        """Send notification for an expired pending action and clean up MQTT."""
        await self._clear_pending_mqtt(pending.id)
        await self._publish_pending_list()

        if self._dispatcher is None:
            return

        from oasisagent.models import Severity

        notification = Notification(
            event_id=pending.event_id,
            severity=Severity.WARNING,
            title=f"[EXPIRED] Pending action expired: {pending.action.description[:60]}",
            message=(
                f"Action: {pending.action.operation} via {pending.action.handler}\n"
                f"Diagnosis: {pending.diagnosis}\n"
                f"Created: {pending.created_at.isoformat()}\n"
                f"Expired: {pending.expires_at.isoformat()}"
            ),
        )

        try:
            await self._dispatcher.dispatch(notification)
        except Exception:
            logger.warning(
                "Notification failed for expired action %s",
                pending.id,
                exc_info=True,
            )

    def _get_mqtt_channel(self) -> MqttNotificationChannel | None:
        """Find the MQTT notification channel, if configured."""
        if self._dispatcher is None:
            return None
        for channel in self._dispatcher.channels:
            if isinstance(channel, MqttNotificationChannel):
                return channel
        return None

    async def _publish_pending_action(self, pending: PendingAction) -> None:
        """Publish a pending action to oasis/pending/{action_id} (retained).

        MQTT message contract:
        - Topic: oasis/pending/{action_id}
        - Payload: JSON with id, event_id, action, diagnosis, timestamps
        - QoS 1, retain=True so late subscribers see pending items
        """
        mqtt = self._get_mqtt_channel()
        if mqtt is None:
            return

        import json

        payload = json.dumps(pending.model_dump(mode="json"), default=str)
        topic = f"oasis/pending/{pending.id}"

        await mqtt.publish_raw(topic, payload, qos=1, retain=True)

    async def _publish_pending_list(self) -> None:
        """Publish the current pending list to oasis/pending/list (retained).

        This is a snapshot of all PENDING actions so that new subscribers
        (CLI, UI) get current state immediately on connect.
        """
        if self._pending_queue is None:
            return

        mqtt = self._get_mqtt_channel()
        if mqtt is None:
            return

        import json

        payload = json.dumps(self._pending_queue.to_list_payload(), default=str)
        await mqtt.publish_raw("oasis/pending/list", payload, qos=1, retain=True)

    async def _clear_pending_mqtt(self, action_id: str) -> None:
        """Clear a retained MQTT message by publishing empty payload."""
        mqtt = self._get_mqtt_channel()
        if mqtt is None:
            return

        await mqtt.publish_raw(
            f"oasis/pending/{action_id}", b"", qos=1, retain=True
        )

    # -------------------------------------------------------------------
    # Verification
    # -------------------------------------------------------------------

    async def _verify_action(
        self,
        handler: Handler,
        event: Event,
        action: RecommendedAction,
        action_result: ActionResult,
    ) -> VerifyResult | None:
        """Verify a successfully executed action had the desired effect.

        Returns None if verification is skipped (non-SUCCESS result or
        dry_run mode). The handler owns the wait/polling strategy.
        """
        if action_result.status != ActionStatus.SUCCESS:
            return None

        # DRY_RUN guard: even if somehow reached with DRY_RUN status,
        # nothing was actually executed — nothing to verify
        if self._config.guardrails.dry_run:
            return None

        try:
            verify_result = await handler.verify(event, action, action_result)
        except Exception as exc:
            logger.warning(
                "Verification raised for %s on event %s: %s",
                action.operation,
                event.id,
                exc,
            )
            verify_result = VerifyResult(verified=False, message=str(exc))

        # Audit the verification result (best-effort)
        if self._audit is not None:
            try:
                await self._audit.write_verify(event, action, verify_result)
            except Exception:
                logger.warning(
                    "Audit verify write failed for event %s",
                    event.id,
                    exc_info=True,
                )

        if verify_result.verified:
            logger.info(
                "Verification passed for %s on %s: %s",
                action.operation,
                event.entity_id,
                verify_result.message,
            )
        else:
            logger.warning(
                "Verification failed for %s on %s: %s",
                action.operation,
                event.entity_id,
                verify_result.message,
            )
            await self._send_verify_failed_notification(
                event, action, verify_result
            )

        return verify_result

    async def _send_verify_failed_notification(
        self,
        event: Event,
        action: RecommendedAction,
        verify_result: VerifyResult,
    ) -> None:
        """Send a notification when action verification fails."""
        if self._dispatcher is None:
            return

        notification = Notification(
            event_id=event.id,
            severity=event.severity,
            title=f"[VERIFY_FAILED] {action.operation} on {event.entity_id}",
            message=(
                f"Action: {action.description}\n"
                f"Handler: {action.handler}\n"
                f"Verify message: {verify_result.message}\n"
                f"Checked at: {verify_result.checked_at.isoformat()}"
            ),
        )

        try:
            await self._dispatcher.dispatch(notification)
        except Exception:
            logger.warning(
                "Notification failed for verify failure on event %s",
                event.id,
                exc_info=True,
            )

    # -------------------------------------------------------------------
    # Audit
    # -------------------------------------------------------------------

    async def _audit_decision(
        self, event: Event, result: DecisionResult
    ) -> None:
        """Write decision to audit trail. Best-effort."""
        risk_tier = ""
        if result.guardrail_result is not None:
            risk_tier = result.guardrail_result.risk_tier.value
        _inc_decisions(result.tier.value, result.disposition.value, risk_tier)

        if self._audit is None:
            return
        try:
            await self._audit.write_event(event)
            await self._audit.write_decision(event, result)
        except Exception:
            logger.warning(
                "Audit write failed for event %s", event.id, exc_info=True
            )

    async def _audit_action(
        self,
        event: Event,
        result: DecisionResult,
        action_result: ActionResult,
        duration_ms: float,
    ) -> None:
        """Write action result to audit trail. Best-effort."""
        fix = (
            self._registry.get_fix_by_id(result.matched_fix_id)
            if self._registry and result.matched_fix_id
            else None
        )
        if fix is not None:
            _inc_actions(
                fix.action.handler, fix.action.operation, action_result.status.value
            )

        if self._audit is None:
            return

        if fix is None:
            return

        action = RecommendedAction(
            description=fix.diagnosis,
            handler=fix.action.handler,
            operation=fix.action.operation,
            params=fix.action.details,
            risk_tier=fix.risk_tier,
        )

        # Attach pipeline timing to the action result
        if action_result.duration_ms is None:
            action_result = action_result.model_copy(
                update={"duration_ms": duration_ms}
            )

        try:
            await self._audit.write_action(event, action, action_result)
        except Exception:
            logger.warning(
                "Audit action write failed for event %s",
                event.id,
                exc_info=True,
            )

    async def _send_notification(
        self, event: Event, result: DecisionResult
    ) -> None:
        """Send a notification for the event. Best-effort."""
        if self._dispatcher is None:
            return

        notification = Notification(
            event_id=event.id,
            severity=event.severity,
            title=self._notification_title(result),
            message=self._notification_message(event, result),
        )

        try:
            await self._dispatcher.dispatch(notification)
        except Exception:
            logger.warning(
                "Notification failed for event %s",
                event.id,
                exc_info=True,
            )

    def _notification_title(self, result: DecisionResult) -> str:
        """Generate a notification title from the decision result."""
        disposition = result.disposition.value.upper()
        if result.diagnosis:
            return f"[{disposition}] {result.diagnosis[:80]}"
        return f"[{disposition}]"

    def _notification_message(
        self, event: Event, result: DecisionResult
    ) -> str:
        """Generate a notification message with event context."""
        parts: list[str] = [
            f"Event: {event.event_type} on {event.entity_id}",
            f"System: {event.system}",
            f"Severity: {event.severity.value}",
            f"Tier: {result.tier.value.upper()}",
            f"Disposition: {result.disposition.value}",
        ]
        if result.diagnosis:
            parts.append(f"Diagnosis: {result.diagnosis}")
        if result.matched_fix_id:
            parts.append(f"Fix: {result.matched_fix_id}")
        escalate_to: Any = result.details.get("escalate_to")
        if escalate_to:
            parts.append(f"Escalate to: {escalate_to}")
        return "\n".join(parts)
