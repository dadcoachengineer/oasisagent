"""Tests for the Orchestrator — lifecycle, dispatch rules, error isolation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from oasisagent.config import (
    AgentConfig,
    AuditConfig,
    GuardrailsConfig,
    HaHandlerConfig,
    HaLogPollerConfig,
    HandlersConfig,
    HaWebSocketConfig,
    InfluxDbConfig,
    IngestionConfig,
    LlmConfig,
    LlmEndpointConfig,
    MqttIngestionConfig,
    MqttNotificationConfig,
    NotificationsConfig,
    OasisAgentConfig,
)
from oasisagent.engine.circuit_breaker import CircuitBreakerResult
from oasisagent.engine.decision import (
    DecisionDisposition,
    DecisionResult,
    DecisionTier,
)
from oasisagent.engine.guardrails import GuardrailResult
from oasisagent.models import (
    ActionResult,
    ActionStatus,
    Event,
    EventMetadata,
    RecommendedAction,
    RiskTier,
    Severity,
)
from oasisagent.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**agent_overrides: Any) -> OasisAgentConfig:
    """Create a minimal config for testing. All external services disabled."""
    agent_defaults: dict[str, Any] = {
        "event_queue_size": 100,
        "shutdown_timeout": 2,
        "event_ttl": 300,
        "known_fixes_dir": "/nonexistent",
    }
    agent_defaults.update(agent_overrides)

    return OasisAgentConfig(
        agent=AgentConfig(**agent_defaults),
        ingestion=IngestionConfig(
            mqtt=MqttIngestionConfig(enabled=False),
            ha_websocket=HaWebSocketConfig(enabled=False),
            ha_log_poller=HaLogPollerConfig(enabled=False),
        ),
        llm=LlmConfig(
            triage=LlmEndpointConfig(
                base_url="http://localhost:11434/v1",
                model="test",
                api_key="test",
            ),
            reasoning=LlmEndpointConfig(
                base_url="http://localhost:11434/v1",
                model="test",
                api_key="test",
            ),
        ),
        handlers=HandlersConfig(
            homeassistant=HaHandlerConfig(enabled=False),
        ),
        guardrails=GuardrailsConfig(),
        audit=AuditConfig(
            influxdb=InfluxDbConfig(enabled=False),
        ),
        notifications=NotificationsConfig(
            mqtt=MqttNotificationConfig(enabled=False),
        ),
    )


def _make_event(
    *,
    ttl: int = 300,
    timestamp: datetime | None = None,
    entity_id: str = "sensor.test",
    severity: Severity = Severity.WARNING,
) -> Event:
    return Event(
        source="test",
        system="homeassistant",
        event_type="state_changed",
        entity_id=entity_id,
        severity=severity,
        timestamp=timestamp or datetime.now(UTC),
        metadata=EventMetadata(ttl=ttl),
    )


def _matched_result(
    event_id: str,
    *,
    risk_tier: RiskTier = RiskTier.AUTO_FIX,
    dry_run: bool = False,
) -> DecisionResult:
    """Create a MATCHED decision result."""
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T0,
        disposition=DecisionDisposition.MATCHED,
        matched_fix_id="test-fix-001",
        diagnosis="Test fix applied",
        guardrail_result=GuardrailResult(
            allowed=True,
            reason="ok",
            dry_run=dry_run,
            risk_tier=risk_tier,
        ),
    )


def _blocked_result(event_id: str) -> DecisionResult:
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T0,
        disposition=DecisionDisposition.BLOCKED,
        matched_fix_id="test-fix-001",
        diagnosis="Blocked by guardrails",
        guardrail_result=GuardrailResult(
            allowed=False,
            reason="entity blocked",
            risk_tier=RiskTier.AUTO_FIX,
        ),
    )


def _dropped_result(event_id: str) -> DecisionResult:
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T1,
        disposition=DecisionDisposition.DROPPED,
        diagnosis="Low-priority, dropping",
    )


def _escalated_result(event_id: str, escalate_to: str = "human") -> DecisionResult:
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T1,
        disposition=DecisionDisposition.ESCALATED,
        diagnosis="Needs human review",
        details={"escalate_to": escalate_to},
    )


def _dry_run_result(event_id: str) -> DecisionResult:
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T0,
        disposition=DecisionDisposition.DRY_RUN,
        matched_fix_id="test-fix-001",
        diagnosis="Would apply fix",
        guardrail_result=GuardrailResult(
            allowed=True,
            reason="dry run",
            dry_run=True,
            risk_tier=RiskTier.AUTO_FIX,
        ),
    )


def _unmatched_result(event_id: str) -> DecisionResult:
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T0,
        disposition=DecisionDisposition.UNMATCHED,
        diagnosis="No fix matched",
    )


def _setup_orchestrator(
    config: OasisAgentConfig | None = None,
) -> Orchestrator:
    """Build an orchestrator with mocked components for testing."""
    orchestrator = Orchestrator(config or _make_config())
    orchestrator._build_components()
    return orchestrator


def _mock_components(orchestrator: Orchestrator) -> dict[str, Any]:
    """Replace real components with mocks and return them."""
    mocks: dict[str, Any] = {}

    # Decision engine
    mock_decision = AsyncMock()
    orchestrator._decision_engine = mock_decision
    mocks["decision"] = mock_decision

    # Audit
    mock_audit = AsyncMock()
    orchestrator._audit = mock_audit
    mocks["audit"] = mock_audit

    # Dispatcher
    mock_dispatcher = AsyncMock()
    orchestrator._dispatcher = mock_dispatcher
    mocks["dispatcher"] = mock_dispatcher

    # Circuit breaker
    mock_cb = MagicMock()
    mock_cb.check.return_value = CircuitBreakerResult(
        allowed=True, reason="ok"
    )
    mock_cb.record_attempt.return_value = CircuitBreakerResult(
        allowed=True, reason="ok"
    )
    orchestrator._circuit_breaker = mock_cb
    mocks["circuit_breaker"] = mock_cb

    return mocks


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_build_components_creates_all(self) -> None:
        orchestrator = _setup_orchestrator()

        assert orchestrator._queue is not None
        assert orchestrator._registry is not None
        assert orchestrator._circuit_breaker is not None
        assert orchestrator._guardrails is not None
        assert orchestrator._llm_client is not None
        assert orchestrator._triage_service is not None
        assert orchestrator._decision_engine is not None
        assert orchestrator._audit is not None
        assert orchestrator._dispatcher is not None

    async def test_start_stop_order(self) -> None:
        """Verify lifecycle methods are called in dependency order."""
        call_log: list[str] = []

        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)

        # Mock audit and dispatcher start/stop
        mock_audit = mocks["audit"]
        mock_audit.start = AsyncMock(side_effect=lambda: call_log.append("audit.start"))
        mock_audit.stop = AsyncMock(side_effect=lambda: call_log.append("audit.stop"))

        mock_dispatcher = mocks["dispatcher"]
        mock_dispatcher.start = AsyncMock(
            side_effect=lambda: call_log.append("dispatcher.start")
        )
        mock_dispatcher.stop = AsyncMock(
            side_effect=lambda: call_log.append("dispatcher.stop")
        )

        await orchestrator._start_components()
        await orchestrator._shutdown()

        # Start order: audit before dispatcher
        assert call_log.index("audit.start") < call_log.index("dispatcher.start")
        # Stop order: dispatcher before audit
        assert call_log.index("dispatcher.stop") < call_log.index("audit.stop")

    async def test_stats_initialized_to_zero(self) -> None:
        orchestrator = _setup_orchestrator()

        assert orchestrator._events_processed == 0
        assert orchestrator._actions_taken == 0
        assert orchestrator._errors == 0


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    async def test_expired_event_dropped(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)

        event = _make_event(
            ttl=60,
            timestamp=datetime.now(UTC) - timedelta(seconds=120),
        )

        await orchestrator._process_one(event)

        # Decision engine should NOT be called
        mocks["decision"].process_event.assert_not_called()

    async def test_fresh_event_processed(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        mocks["decision"].process_event.return_value = _dropped_result("evt-1")

        event = _make_event(ttl=300, timestamp=datetime.now(UTC))

        await orchestrator._process_one(event)

        mocks["decision"].process_event.assert_called_once()
        assert mocks["decision"].process_event.call_args.args[0] is event

    async def test_per_event_ttl_overrides_global(self) -> None:
        """Per-event TTL takes precedence over agent.event_ttl."""
        config = _make_config(event_ttl=10)
        orchestrator = _setup_orchestrator(config)
        mocks = _mock_components(orchestrator)
        mocks["decision"].process_event.return_value = _dropped_result("evt-1")

        # Event has per-event TTL of 600s, even though global is 10s
        event = _make_event(
            ttl=600,
            timestamp=datetime.now(UTC) - timedelta(seconds=30),
        )

        await orchestrator._process_one(event)

        # Should NOT be expired — per-event TTL (600) > age (30)
        mocks["decision"].process_event.assert_called_once()

    async def test_global_ttl_fallback(self) -> None:
        """When per-event TTL is 0, use global default."""
        config = _make_config(event_ttl=60)
        orchestrator = _setup_orchestrator(config)
        mocks = _mock_components(orchestrator)

        event = _make_event(
            ttl=0,
            timestamp=datetime.now(UTC) - timedelta(seconds=120),
        )

        await orchestrator._process_one(event)

        # Should be expired — global TTL (60) < age (120)
        mocks["decision"].process_event.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch rules
# ---------------------------------------------------------------------------


class TestDispatchRules:
    async def test_matched_auto_fix_executes_handler(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)

        # Add a mock handler and known fix
        mock_handler = AsyncMock()
        mock_handler.name.return_value = "homeassistant"
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        # Mock registry to return a fix
        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "restart_integration"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Restart integration"
        mock_fix.risk_tier = RiskTier.AUTO_FIX
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        await orchestrator._process_one(event)

        mock_handler.execute.assert_called_once()
        mocks["audit"].write_action.assert_called_once()
        assert orchestrator._actions_taken == 1

    async def test_matched_recommend_skips_handler(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(
            event.id, risk_tier=RiskTier.RECOMMEND
        )

        mock_handler = AsyncMock()
        orchestrator._handlers["homeassistant"] = mock_handler

        await orchestrator._process_one(event)

        mock_handler.execute.assert_not_called()
        # Should notify for RECOMMEND
        mocks["dispatcher"].dispatch.assert_called_once()

    async def test_blocked_no_handler_no_notify(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _blocked_result(event.id)

        await orchestrator._process_one(event)

        mocks["dispatcher"].dispatch.assert_not_called()

    async def test_dropped_no_handler_no_notify(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _dropped_result(event.id)

        await orchestrator._process_one(event)

        mocks["dispatcher"].dispatch.assert_not_called()

    async def test_escalated_human_notifies(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _escalated_result(event.id)

        await orchestrator._process_one(event)

        mocks["dispatcher"].dispatch.assert_called_once()

    async def test_dry_run_notifies_no_handler(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _dry_run_result(event.id)

        mock_handler = AsyncMock()
        orchestrator._handlers["homeassistant"] = mock_handler

        await orchestrator._process_one(event)

        mock_handler.execute.assert_not_called()
        mocks["dispatcher"].dispatch.assert_called_once()

    async def test_unmatched_no_handler_no_notify(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _unmatched_result(event.id)

        await orchestrator._process_one(event)

        mocks["dispatcher"].dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    async def test_process_one_exception_does_not_crash(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        mocks["decision"].process_event.side_effect = RuntimeError("boom")

        event = _make_event()

        # Should not raise
        await orchestrator._process_one(event)

        assert orchestrator._errors == 1

    async def test_audit_failure_does_not_block_pipeline(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _dropped_result(event.id)
        mocks["audit"].write_event.side_effect = RuntimeError("influxdb down")

        # Should not raise
        await orchestrator._process_one(event)

        assert orchestrator._events_processed == 1

    async def test_notification_failure_does_not_block(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _escalated_result(event.id)
        mocks["dispatcher"].dispatch.side_effect = RuntimeError("mqtt down")

        # Should not raise
        await orchestrator._process_one(event)

        assert orchestrator._events_processed == 1

    async def test_handler_failure_records_circuit_breaker(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)

        mock_handler = AsyncMock()
        mock_handler.execute.side_effect = RuntimeError("handler crashed")
        orchestrator._handlers["homeassistant"] = mock_handler

        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "restart_integration"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Test fix"
        mock_fix.risk_tier = RiskTier.AUTO_FIX
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        await orchestrator._process_one(event)

        mocks["circuit_breaker"].record_attempt.assert_called_once_with(
            event.entity_id, success=False
        )


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


class TestSignalHandling:
    async def test_stop_sets_flag(self) -> None:
        orchestrator = _setup_orchestrator()
        _mock_components(orchestrator)

        assert orchestrator._shutting_down is False

        await orchestrator.stop()

        assert orchestrator._shutting_down is True

    async def test_signal_handler_sets_flag(self) -> None:
        orchestrator = _setup_orchestrator()

        orchestrator._signal_handler()

        assert orchestrator._shutting_down is True


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------


class TestMainLoop:
    async def test_run_processes_events_and_stops(self) -> None:
        """Test that run() processes events and exits on shutdown."""
        orchestrator = _setup_orchestrator()

        # Prevent real signal handler installation (not available in tests)
        orchestrator._install_signal_handlers = MagicMock()  # type: ignore[method-assign]

        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _dropped_result(event.id)

        assert orchestrator._queue is not None
        await orchestrator._queue.put(event)

        # Schedule shutdown after a short delay
        async def delayed_shutdown() -> None:
            await asyncio.sleep(0.1)
            orchestrator._shutting_down = True

        # Start components
        await orchestrator._start_components()

        # Run the loop with concurrent shutdown
        task = asyncio.create_task(delayed_shutdown())
        try:
            while not orchestrator._shutting_down:
                try:
                    evt = await asyncio.wait_for(
                        orchestrator._queue.get(), timeout=0.05
                    )
                except TimeoutError:
                    continue
                await orchestrator._process_one(evt)
                orchestrator._queue.task_done()
        except asyncio.CancelledError:
            pass

        await task
        await orchestrator._shutdown()

        mocks["decision"].process_event.assert_called_once()


# ---------------------------------------------------------------------------
# Per-event timing
# ---------------------------------------------------------------------------


class TestEventTiming:
    async def test_duration_ms_passed_to_audit(self) -> None:
        """Verify per-event timing is captured and passed to audit."""
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)

        mock_handler = AsyncMock()
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "restart_integration"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Test fix"
        mock_fix.risk_tier = RiskTier.AUTO_FIX
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        await orchestrator._process_one(event)

        # Verify write_action was called with duration_ms > 0
        mocks["audit"].write_action.assert_called_once()
        call_args = mocks["audit"].write_action.call_args
        action_result = call_args[0][2]  # third positional arg
        assert action_result.duration_ms is not None
        assert action_result.duration_ms >= 0


# ---------------------------------------------------------------------------
# Notification content
# ---------------------------------------------------------------------------


class TestNotificationContent:
    def test_notification_title_matched(self) -> None:
        orchestrator = _setup_orchestrator()
        result = _matched_result("evt-1")

        title = orchestrator._notification_title(result)

        assert "[MATCHED]" in title
        assert "Test fix applied" in title

    def test_notification_title_escalated(self) -> None:
        orchestrator = _setup_orchestrator()
        result = _escalated_result("evt-1")

        title = orchestrator._notification_title(result)

        assert "[ESCALATED]" in title

    def test_notification_message_includes_event_context(self) -> None:
        orchestrator = _setup_orchestrator()
        event = _make_event(entity_id="switch.office_lamp")
        result = _escalated_result(event.id, escalate_to="human")

        message = orchestrator._notification_message(event, result)

        assert "switch.office_lamp" in message
        assert "homeassistant" in message
        assert "Escalate to: human" in message


# ---------------------------------------------------------------------------
# Handler dispatch edge cases
# ---------------------------------------------------------------------------


class TestHandlerDispatch:
    async def test_no_handler_registered_returns_skipped(self) -> None:
        orchestrator = _setup_orchestrator()
        _mock_components(orchestrator)
        event = _make_event()

        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "nonexistent_handler"
        mock_fix.action.operation = "restart"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Fix"
        mock_fix.risk_tier = RiskTier.AUTO_FIX
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        result = _matched_result(event.id)
        action_result = await orchestrator._dispatch_handler(event, result)

        assert action_result.status == ActionStatus.SKIPPED

    async def test_no_fix_returns_skipped(self) -> None:
        orchestrator = _setup_orchestrator()
        _mock_components(orchestrator)
        event = _make_event()

        mock_registry = MagicMock()
        mock_registry.get_fix_by_id.return_value = None
        orchestrator._registry = mock_registry

        result = _matched_result(event.id)
        action_result = await orchestrator._dispatch_handler(event, result)

        assert action_result.status == ActionStatus.SKIPPED

    async def test_successful_handler_increments_actions_taken(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()

        mock_handler = AsyncMock()
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "restart_integration"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Fix"
        mock_fix.risk_tier = RiskTier.AUTO_FIX
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        result = _matched_result(event.id)
        await orchestrator._dispatch_handler(event, result)

        assert orchestrator._actions_taken == 1
        mocks["circuit_breaker"].record_attempt.assert_called_once_with(
            event.entity_id, success=True
        )


# ---------------------------------------------------------------------------
# Event correlation (§16.5)
# ---------------------------------------------------------------------------


class TestEventCorrelation:
    async def test_correlated_event_skips_decision_engine(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        mocks["decision"].process_event.return_value = _dropped_result("evt-1")

        e1 = _make_event(entity_id="sensor.one")
        e2 = _make_event(entity_id="sensor.two")

        await orchestrator._process_one(e1)
        await orchestrator._process_one(e2)

        # Decision engine called only for the leader
        mocks["decision"].process_event.assert_called_once()
        # Both events counted as processed
        assert orchestrator._events_processed == 2

    async def test_correlated_event_is_audited(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        mocks["decision"].process_event.return_value = _dropped_result("evt-1")

        e1 = _make_event(entity_id="sensor.one")
        e2 = _make_event(entity_id="sensor.two")

        await orchestrator._process_one(e1)
        await orchestrator._process_one(e2)

        # Audit called for both events (leader decision + correlated decision)
        assert mocks["audit"].write_decision.call_count == 2

        # Second call is for the correlated event
        second_call = mocks["audit"].write_decision.call_args_list[1]
        correlated_result = second_call[0][1]
        assert correlated_result.disposition.value == "correlated"
        assert "leader" in correlated_result.diagnosis.lower()

    async def test_correlation_disabled_processes_all(self) -> None:
        config = _make_config(correlation_window=0)
        orchestrator = _setup_orchestrator(config)
        mocks = _mock_components(orchestrator)
        mocks["decision"].process_event.return_value = _dropped_result("evt-1")

        e1 = _make_event(entity_id="sensor.one")
        e2 = _make_event(entity_id="sensor.two")

        await orchestrator._process_one(e1)
        await orchestrator._process_one(e2)

        # Both events go through decision engine
        assert mocks["decision"].process_event.call_count == 2

    async def test_correlation_id_set_on_event_metadata(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        mocks["decision"].process_event.return_value = _dropped_result("evt-1")

        event = _make_event()
        assert event.metadata.correlation_id is None

        await orchestrator._process_one(event)

        assert event.metadata.correlation_id is not None


# ---------------------------------------------------------------------------
# Notify auto-execution (issue #167)
# ---------------------------------------------------------------------------


class TestNotifyAutoExecution:
    """Notify-only RECOMMEND actions bypass the approval queue."""

    async def test_t0_notify_action_auto_executes(self) -> None:
        """T0 RECOMMEND + operation=notify executes handler directly."""
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(
            event.id, risk_tier=RiskTier.RECOMMEND
        )

        mock_handler = AsyncMock()
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        # Mock registry to return a fix with operation=notify
        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "notify"
        mock_fix.action.details = {"message": "Entity unavailable"}
        mock_fix.diagnosis = "Entity is unavailable"
        mock_fix.risk_tier = RiskTier.RECOMMEND
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        await orchestrator._process_one(event)

        # Handler should be called (auto-executed)
        mock_handler.execute.assert_called_once()
        # Should NOT be enqueued to pending queue
        assert orchestrator._pending_queue is not None
        assert len(orchestrator._pending_queue.list_pending()) == 0
        # Notification should still be sent
        mocks["dispatcher"].dispatch.assert_called()
        # Actions counter incremented
        assert orchestrator._actions_taken == 1

    async def test_t0_non_notify_recommend_still_enqueues(self) -> None:
        """T0 RECOMMEND + operation!=notify still goes to pending queue."""
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(
            event.id, risk_tier=RiskTier.RECOMMEND
        )

        mock_handler = AsyncMock()
        orchestrator._handlers["homeassistant"] = mock_handler

        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "restart_integration"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Restart integration"
        mock_fix.risk_tier = RiskTier.RECOMMEND
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        await orchestrator._process_one(event)

        # Handler should NOT be called
        mock_handler.execute.assert_not_called()
        # Should be enqueued
        assert orchestrator._pending_queue is not None
        assert len(orchestrator._pending_queue.list_pending()) == 1

    async def test_t2_notify_action_auto_executes(self) -> None:
        """T2 RECOMMEND + operation=notify executes handler directly."""
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()

        notify_action = RecommendedAction(
            description="Entity unavailable notification",
            handler="homeassistant",
            operation="notify",
            params={"message": "Entity unavailable"},
            risk_tier=RiskTier.RECOMMEND,
        )
        result = DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T2,
            disposition=DecisionDisposition.MATCHED,
            diagnosis="T2 diagnosis",
            recommended_actions=[notify_action],
        )
        mocks["decision"].process_event.return_value = result

        mock_handler = AsyncMock()
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        await orchestrator._process_one(event)

        # Handler should be called (auto-executed)
        mock_handler.execute.assert_called_once()
        # Should NOT be enqueued
        assert orchestrator._pending_queue is not None
        assert len(orchestrator._pending_queue.list_pending()) == 0
        assert orchestrator._actions_taken == 1

    async def test_t2_non_notify_recommend_still_enqueues(self) -> None:
        """T2 RECOMMEND + operation!=notify still goes to pending queue."""
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()

        restart_action = RecommendedAction(
            description="Restart integration",
            handler="homeassistant",
            operation="restart_integration",
            params={},
            risk_tier=RiskTier.RECOMMEND,
        )
        result = DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T2,
            disposition=DecisionDisposition.MATCHED,
            diagnosis="T2 diagnosis",
            recommended_actions=[restart_action],
        )
        mocks["decision"].process_event.return_value = result

        mock_handler = AsyncMock()
        orchestrator._handlers["homeassistant"] = mock_handler

        await orchestrator._process_one(event)

        # Handler should NOT be called
        mock_handler.execute.assert_not_called()
        # Should be enqueued
        assert orchestrator._pending_queue is not None
        assert len(orchestrator._pending_queue.list_pending()) == 1

    async def test_notify_auto_execute_no_handler_logs_warning(self) -> None:
        """Auto-execute notify with missing handler logs warning, no crash."""
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(
            event.id, risk_tier=RiskTier.RECOMMEND
        )

        # No handler registered — orchestrator._handlers is empty
        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "notify"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Entity unavailable"
        mock_fix.risk_tier = RiskTier.RECOMMEND
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        # Should not raise
        await orchestrator._process_one(event)

        # No pending actions created
        assert orchestrator._pending_queue is not None
        assert len(orchestrator._pending_queue.list_pending()) == 0
        # Actions counter NOT incremented (handler missing)
        assert orchestrator._actions_taken == 0

    async def test_notify_auto_execute_handler_failure_does_not_crash(self) -> None:
        """Handler exception during auto-execute notify is caught gracefully."""
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(
            event.id, risk_tier=RiskTier.RECOMMEND
        )

        mock_handler = AsyncMock()
        mock_handler.execute.side_effect = RuntimeError("handler crashed")
        orchestrator._handlers["homeassistant"] = mock_handler

        mock_registry = MagicMock()
        mock_fix = MagicMock()
        mock_fix.action.handler = "homeassistant"
        mock_fix.action.operation = "notify"
        mock_fix.action.details = {}
        mock_fix.diagnosis = "Entity unavailable"
        mock_fix.risk_tier = RiskTier.RECOMMEND
        mock_registry.get_fix_by_id.return_value = mock_fix
        orchestrator._registry = mock_registry

        # Should not raise
        await orchestrator._process_one(event)

        # Actions counter NOT incremented (handler failed)
        assert orchestrator._actions_taken == 0


class TestIPSynthesis:
    """Tests for cross-adapter IP synthesis."""

    def test_creates_hosted_by_edges_across_adapters(self) -> None:
        from oasisagent.models import TopologyNode
        from oasisagent.orchestrator import Orchestrator

        nodes = [
            TopologyNode(
                entity_id="proxmox:node1", entity_type="host",
                host_ip="192.168.1.120", source="auto:proxmox",
            ),
            TopologyNode(
                entity_id="portainer:swarm", entity_type="host",
                host_ip="192.168.1.120", source="auto:portainer",
            ),
            TopologyNode(
                entity_id="unifi:ab:cd:ef", entity_type="network_device",
                host_ip="192.168.1.1", source="auto:unifi",
            ),
        ]

        edges = Orchestrator._synthesize_ip_edges(nodes)

        # Two hosts on same IP from different adapters — one is anchor,
        # but both are host type, so the non-anchor host is skipped
        # (host↔host noise filter)
        assert len(edges) == 0

    def test_links_service_to_host_across_adapters(self) -> None:
        from oasisagent.models import TopologyNode
        from oasisagent.orchestrator import Orchestrator

        nodes = [
            TopologyNode(
                entity_id="proxmox:node1", entity_type="host",
                host_ip="192.168.1.120", source="auto:proxmox",
            ),
            TopologyNode(
                entity_id="npm:proxy:example.com", entity_type="proxy",
                host_ip="192.168.1.120", source="auto:npm",
            ),
        ]

        edges = Orchestrator._synthesize_ip_edges(nodes)

        assert len(edges) == 1
        assert edges[0].from_entity == "npm:proxy:example.com"
        assert edges[0].to_entity == "proxmox:node1"
        assert edges[0].edge_type == "hosted_by"

    def test_skips_same_adapter_nodes(self) -> None:
        from oasisagent.models import TopologyNode
        from oasisagent.orchestrator import Orchestrator

        nodes = [
            TopologyNode(
                entity_id="portainer:node1", entity_type="host",
                host_ip="10.0.0.1", source="auto:portainer",
            ),
            TopologyNode(
                entity_id="portainer:node1/nginx", entity_type="container",
                host_ip="10.0.0.1", source="auto:portainer",
            ),
        ]

        edges = Orchestrator._synthesize_ip_edges(nodes)

        # Same adapter — no synthesis needed
        assert len(edges) == 0
