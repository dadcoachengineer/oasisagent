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
from typing import TYPE_CHECKING, Any

from oasisagent.approval.listener import ApprovalListener
from oasisagent.approval.pending import PendingAction, PendingQueue
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
from oasisagent.llm.reasoning import ReasoningService
from oasisagent.llm.triage import TriageService
from oasisagent.models import (
    ActionResult,
    ActionStatus,
    Notification,
    RecommendedAction,
    RiskTier,
    VerifyResult,
)
from oasisagent.notifications.dispatcher import NotificationDispatcher
from oasisagent.notifications.mqtt import MqttNotificationChannel

if TYPE_CHECKING:
    from collections.abc import Coroutine

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
        self._reasoning_service: ReasoningService | None = None
        self._decision_engine: DecisionEngine | None = None
        self._handlers: dict[str, Handler] = {}
        self._audit: AuditWriter | None = None
        self._dispatcher: NotificationDispatcher | None = None
        self._pending_queue: PendingQueue | None = None
        self._approval_listener: ApprovalListener | None = None
        self._approval_listener_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
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
                # Sweep expired pending actions each tick
                self._expire_stale_actions()

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

        # 7. Reasoning service (T2)
        # TODO: Wire entity_context fetching (handler.get_context()) into the
        # T2 pipeline once §16.2 entity context enrichment is implemented.
        self._reasoning_service = ReasoningService(self._llm_client)

        # 8. Decision engine
        self._decision_engine = DecisionEngine(
            registry=self._registry,
            guardrails=self._guardrails,
            triage_service=self._triage_service,
            reasoning_service=self._reasoning_service,
        )

        # 9. Handlers
        if cfg.handlers.homeassistant.enabled:
            ha = HomeAssistantHandler(cfg.handlers.homeassistant)
            self._handlers[ha.name()] = ha

        # 10. Audit writer
        self._audit = AuditWriter(cfg.audit)

        # 11. Notification channels
        channels = []
        if cfg.notifications.mqtt.enabled:
            channels.append(MqttNotificationChannel(cfg.notifications.mqtt))
        self._dispatcher = NotificationDispatcher(channels)

        # 12. Pending action queue (for RECOMMEND-tier actions)
        self._pending_queue = PendingQueue()

        # 13. Approval listener (subscribes to MQTT approve/reject topics)
        if cfg.ingestion.mqtt.enabled:
            self._approval_listener = ApprovalListener(
                config=cfg.ingestion.mqtt,
                on_approve=self._handle_approval,
                on_reject=self._handle_rejection,
            )

        # 14. Ingestion adapters
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

        # Approval listener — background task
        if self._approval_listener is not None:
            self._approval_listener_task = asyncio.create_task(
                self._run_approval_listener(),
                name="approval-listener",
            )
            logger.info("Approval listener started")

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

    async def _run_approval_listener(self) -> None:
        """Run the approval listener. Logs exceptions but never propagates."""
        try:
            assert self._approval_listener is not None
            await self._approval_listener.start()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Approval listener crashed")

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

        # 2. Expire all remaining pending actions
        if self._pending_queue is not None:
            for pending in self._pending_queue.list_pending():
                self._pending_queue.reject(pending.id)
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

        # 7. Stop audit writer
        if self._audit is not None:
            try:
                await self._audit.stop()
            except Exception:
                logger.exception("Error stopping audit writer")

        # 8. Log final stats
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

            # 2. Decision (T0 → T1 → T2, guardrails applied)
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
            # RECOMMEND-tier actions go to the pending queue
            if action.risk_tier == RiskTier.RECOMMEND:
                pending = self._pending_queue.add(
                    event_id=event.id,
                    action=action,
                    diagnosis=result.diagnosis,
                    timeout_minutes=self._config.guardrails.approval_timeout_minutes,
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
                    event.entity_id, success=success
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
                    event.entity_id, success=False
                )

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

        pending = self._pending_queue.add(
            event_id=event.id,
            action=action,
            diagnosis=result.diagnosis,
            timeout_minutes=self._config.guardrails.approval_timeout_minutes,
        )
        await self._publish_pending_action(pending)
        await self._publish_pending_list()

        # Notify operator about the pending action
        await self._send_notification(event, result)

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

        pending = self._pending_queue.approve(action_id)
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

        pending = self._pending_queue.reject(action_id)
        if pending is None:
            return

        # Clear the retained MQTT message
        await self._clear_pending_mqtt(action_id)
        await self._publish_pending_list()

        logger.info("Action %s rejected by operator", action_id)

    def _expire_stale_actions(self) -> None:
        """Sweep for expired pending actions and notify."""
        if self._pending_queue is None:
            return

        expired = self._pending_queue.expire_stale()
        for pending in expired:
            self._schedule_task(self._notify_expired(pending))

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
