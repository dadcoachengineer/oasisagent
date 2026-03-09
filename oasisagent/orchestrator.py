"""Orchestrator — wires components together and runs the event loop.

The orchestrator is the application's main loop. It builds all components
from config, starts them in dependency order, pulls events from the queue,
and runs them through the full pipeline. Graceful shutdown on SIGTERM/SIGINT.

ARCHITECTURE.md §15 defines the orchestrator specification.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oasisagent.audit.influxdb import AuditWriter
from oasisagent.engine.circuit_breaker import CircuitBreaker
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionEngine,
    DecisionResult,
)
from oasisagent.engine.guardrails import GuardrailsEngine
from oasisagent.engine.known_fixes import KnownFixRegistry
from oasisagent.engine.queue import EventQueue
from oasisagent.handlers.homeassistant import HomeAssistantHandler
from oasisagent.ingestion.ha_log_poller import HaLogPollerAdapter
from oasisagent.ingestion.ha_websocket import HaWebSocketAdapter
from oasisagent.ingestion.mqtt import MqttAdapter
from oasisagent.llm.client import LLMClient
from oasisagent.llm.triage import TriageService
from oasisagent.models import (
    ActionResult,
    ActionStatus,
    Notification,
    RecommendedAction,
    RiskTier,
)
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.mqtt import MqttNotificationChannel

if TYPE_CHECKING:
    from oasisagent.config import OasisAgentConfig
    from oasisagent.handlers.base import Handler
    from oasisagent.ingestion.base import IngestAdapter
    from oasisagent.models import Event

logger = logging.getLogger(__name__)


class Orchestrator:
    """Builds, starts, and runs all OasisAgent components.

    This is the application entry point. ``run()`` blocks until a shutdown
    signal is received or ``shutdown()`` is called externally.
    """

    def __init__(self, config: OasisAgentConfig) -> None:
        self._config = config
        self._shutting_down = False

        # Components — populated by _build_components()
        self._queue: EventQueue | None = None
        self._registry: KnownFixRegistry | None = None
        self._circuit_breaker: CircuitBreaker | None = None
        self._guardrails: GuardrailsEngine | None = None
        self._llm_client: LLMClient | None = None
        self._triage_service: TriageService | None = None
        self._decision_engine: DecisionEngine | None = None
        self._handlers: dict[str, Handler] = {}
        self._audit: AuditWriter | None = None
        self._dispatcher: NotificationDispatcher | None = None
        self._adapters: list[IngestAdapter] = []
        self._adapter_tasks: list[asyncio.Task[None]] = []

        # Stats
        self._events_processed: int = 0
        self._actions_taken: int = 0
        self._errors: int = 0

    async def run(self) -> None:
        """Start all components and enter the main event loop.

        Blocks until shutdown signal. This is the application entry point.
        """
        self._build_components()
        await self._start_components()
        self._install_signal_handlers()

        logger.info("OasisAgent started — entering event loop")

        try:
            while not self._shutting_down:
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
        finally:
            await self._shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown. Called by signal handler or externally."""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutdown requested")

    # -------------------------------------------------------------------
    # Component construction
    # -------------------------------------------------------------------

    def _build_components(self) -> None:
        """Instantiate all components from config. No I/O — just wiring."""
        cfg = self._config

        # 1. Event queue
        self._queue = EventQueue(
            max_size=cfg.agent.event_queue_size,
            dedup_window_seconds=cfg.agent.dedup_window_seconds,
        )

        # 2. Known fixes registry
        self._registry = KnownFixRegistry()
        fixes_dir = Path(cfg.agent.known_fixes_dir)
        if fixes_dir.exists():
            self._registry.load(fixes_dir)
        else:
            logger.warning("Known fixes directory not found: %s", fixes_dir)

        # 3. Circuit breaker
        self._circuit_breaker = CircuitBreaker(cfg.guardrails.circuit_breaker)

        # 4. Guardrails engine
        self._guardrails = GuardrailsEngine(cfg.guardrails)

        # 5. LLM client (stateless)
        self._llm_client = LLMClient(cfg.llm)

        # 6. Triage service
        self._triage_service = TriageService(self._llm_client)

        # 7. Decision engine
        self._decision_engine = DecisionEngine(
            registry=self._registry,
            guardrails=self._guardrails,
            triage_service=self._triage_service,
        )

        # 8. Handlers
        if cfg.handlers.homeassistant.enabled:
            ha = HomeAssistantHandler(cfg.handlers.homeassistant)
            self._handlers[ha.name()] = ha

        # 9. Audit writer
        self._audit = AuditWriter(cfg.audit)

        # 10. Notification channels
        channels = []
        if cfg.notifications.mqtt.enabled:
            channels.append(MqttNotificationChannel(cfg.notifications.mqtt))
        self._dispatcher = NotificationDispatcher(channels)

        # 11. Ingestion adapters
        if cfg.ingestion.mqtt.enabled:
            self._adapters.append(MqttAdapter(cfg.ingestion.mqtt, self._queue))
        if cfg.ingestion.ha_websocket.enabled:
            self._adapters.append(
                HaWebSocketAdapter(cfg.ingestion.ha_websocket, self._queue)
            )
        if cfg.ingestion.ha_log_poller.enabled:
            self._adapters.append(
                HaLogPollerAdapter(cfg.ingestion.ha_log_poller, self._queue)
            )

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

        # Ingestion adapters — launched as background tasks
        for adapter in self._adapters:
            task = asyncio.create_task(
                self._run_adapter(adapter), name=f"adapter-{adapter.name}"
            )
            self._adapter_tasks.append(task)
            logger.info("Ingestion adapter started: %s", adapter.name)

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

    async def _shutdown(self) -> None:
        """Tear down components in reverse dependency order."""
        self._shutting_down = True

        # 1. Stop ingestion adapters
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

        # 2. Drain queue with timeout
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

        # 3. Stop handlers
        for name, handler in self._handlers.items():
            try:
                await handler.stop()
            except Exception:
                logger.exception("Error stopping handler %s", name)

        # 4. Stop notification dispatcher
        if self._dispatcher is not None:
            try:
                await self._dispatcher.stop()
            except Exception:
                logger.exception("Error stopping notification dispatcher")

        # 5. Stop audit writer
        if self._audit is not None:
            try:
                await self._audit.stop()
            except Exception:
                logger.exception("Error stopping audit writer")

        # 6. Log final stats
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

            # 2. Decision (T0 → T1 → T2, guardrails applied)
            assert self._decision_engine is not None
            result = await self._decision_engine.process_event(event)

            duration_ms = (time.monotonic() - start) * 1000

            # 3. Audit the decision (best-effort)
            await self._audit_decision(event, result)

            # 4. Handler dispatch (if action required and not dry-run)
            if self._should_execute(result):
                action_result = await self._dispatch_handler(event, result)
                await self._audit_action(event, result, action_result, duration_ms)
            elif self._is_dry_run(result):
                # DRY_RUN always notifies, skip _should_notify check
                self._events_processed += 1
                await self._send_notification(event, result)
                return

            # 5. Notification (if warranted)
            if self._should_notify(result):
                await self._send_notification(event, result)

            self._events_processed += 1

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

    async def _audit_decision(
        self, event: Event, result: DecisionResult
    ) -> None:
        """Write decision to audit trail. Best-effort."""
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
        if self._audit is None:
            return

        fix = (
            self._registry.get_fix_by_id(result.matched_fix_id)
            if self._registry and result.matched_fix_id
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
