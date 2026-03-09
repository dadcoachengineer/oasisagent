"""Tests for the verification loop (§16.4).

After a handler executes successfully, the orchestrator calls handler.verify()
to confirm the action had the desired effect. Verification failures escalate
via notification and audit.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    VerifyResult,
)
from oasisagent.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**guardrail_overrides: Any) -> OasisAgentConfig:
    guardrail_defaults: dict[str, Any] = {}
    guardrail_defaults.update(guardrail_overrides)

    return OasisAgentConfig(
        agent=AgentConfig(
            event_queue_size=100,
            shutdown_timeout=2,
            event_ttl=300,
            known_fixes_dir="/nonexistent",
        ),
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
        guardrails=GuardrailsConfig(**guardrail_defaults),
        audit=AuditConfig(
            influxdb=InfluxDbConfig(enabled=False),
        ),
        notifications=NotificationsConfig(
            mqtt=MqttNotificationConfig(enabled=False),
        ),
    )


def _make_event(entity_id: str = "sensor.test") -> Event:
    return Event(
        source="test",
        system="homeassistant",
        event_type="state_changed",
        entity_id=entity_id,
        severity=Severity.WARNING,
        timestamp=datetime.now(UTC),
        metadata=EventMetadata(ttl=300),
    )


def _make_action() -> RecommendedAction:
    return RecommendedAction(
        description="Restart ZWave integration",
        handler="homeassistant",
        operation="restart_integration",
        params={"integration": "zwave_js"},
        risk_tier=RiskTier.AUTO_FIX,
    )


def _matched_result(event_id: str) -> DecisionResult:
    return DecisionResult(
        event_id=event_id,
        tier=DecisionTier.T0,
        disposition=DecisionDisposition.MATCHED,
        matched_fix_id="test-fix-001",
        diagnosis="Restart integration",
        guardrail_result=GuardrailResult(
            allowed=True,
            reason="ok",
            risk_tier=RiskTier.AUTO_FIX,
        ),
    )


def _setup_orchestrator(
    config: OasisAgentConfig | None = None,
) -> Orchestrator:
    orchestrator = Orchestrator(config or _make_config())
    orchestrator._build_components()
    return orchestrator


def _mock_components(orchestrator: Orchestrator) -> dict[str, Any]:
    mocks: dict[str, Any] = {}

    mock_decision = AsyncMock()
    orchestrator._decision_engine = mock_decision
    mocks["decision"] = mock_decision

    mock_audit = AsyncMock()
    orchestrator._audit = mock_audit
    mocks["audit"] = mock_audit

    mock_dispatcher = AsyncMock()
    orchestrator._dispatcher = mock_dispatcher
    mocks["dispatcher"] = mock_dispatcher

    mock_cb = MagicMock()
    mock_cb.check.return_value = CircuitBreakerResult(allowed=True, reason="ok")
    mock_cb.record_attempt.return_value = CircuitBreakerResult(
        allowed=True, reason="ok"
    )
    orchestrator._circuit_breaker = mock_cb
    mocks["circuit_breaker"] = mock_cb

    return mocks


def _add_handler_and_registry(
    orchestrator: Orchestrator,
    verify_result: VerifyResult | None = None,
    execute_status: ActionStatus = ActionStatus.SUCCESS,
) -> AsyncMock:
    """Add a mock handler and registry, return the mock handler."""
    mock_handler = AsyncMock()
    mock_handler.name.return_value = "homeassistant"
    mock_handler.execute.return_value = ActionResult(status=execute_status)
    if verify_result is not None:
        mock_handler.verify.return_value = verify_result
    else:
        mock_handler.verify.return_value = VerifyResult(
            verified=True, message="Entity recovered"
        )
    orchestrator._handlers["homeassistant"] = mock_handler

    mock_registry = MagicMock()
    mock_fix = MagicMock()
    mock_fix.action.handler = "homeassistant"
    mock_fix.action.operation = "restart_integration"
    mock_fix.action.details = {}
    mock_fix.diagnosis = "Restart integration"
    mock_fix.risk_tier = RiskTier.AUTO_FIX
    mock_registry.get_fix_by_id.return_value = mock_fix
    orchestrator._registry = mock_registry

    return mock_handler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVerifyCalledAfterSuccess:
    async def test_verify_called_after_successful_execute(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        mock_handler = _add_handler_and_registry(orchestrator)

        await orchestrator._process_one(event)

        mock_handler.verify.assert_called_once()
        args = mock_handler.verify.call_args[0]
        assert args[0] == event  # event
        assert args[1].operation == "restart_integration"  # action
        assert args[2].status == ActionStatus.SUCCESS  # action_result

    async def test_verify_skipped_on_failure(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        mock_handler = _add_handler_and_registry(
            orchestrator, execute_status=ActionStatus.FAILURE
        )

        await orchestrator._process_one(event)

        mock_handler.verify.assert_not_called()

    async def test_verify_skipped_on_skipped(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        mock_handler = _add_handler_and_registry(
            orchestrator, execute_status=ActionStatus.SKIPPED
        )

        await orchestrator._process_one(event)

        mock_handler.verify.assert_not_called()


class TestVerifyAudit:
    async def test_verify_success_audited(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        _add_handler_and_registry(
            orchestrator,
            verify_result=VerifyResult(verified=True, message="Entity recovered"),
        )

        await orchestrator._process_one(event)

        mocks["audit"].write_verify.assert_called_once()
        call_args = mocks["audit"].write_verify.call_args[0]
        assert call_args[0] == event
        assert call_args[1].operation == "restart_integration"
        assert call_args[2].verified is True

    async def test_verify_failure_audited(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        _add_handler_and_registry(
            orchestrator,
            verify_result=VerifyResult(verified=False, message="Still unavailable"),
        )

        await orchestrator._process_one(event)

        mocks["audit"].write_verify.assert_called_once()
        call_args = mocks["audit"].write_verify.call_args[0]
        assert call_args[2].verified is False


class TestVerifyFailureEscalates:
    async def test_verify_failure_sends_notification(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event(entity_id="switch.zwave")
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        _add_handler_and_registry(
            orchestrator,
            verify_result=VerifyResult(verified=False, message="Still unavailable"),
        )

        await orchestrator._process_one(event)

        # Two dispatch calls: one for the action notification, one for verify failure
        assert mocks["dispatcher"].dispatch.call_count >= 1
        # Find the verify failure notification
        for call in mocks["dispatcher"].dispatch.call_args_list:
            notification = call[0][0]
            if "[VERIFY_FAILED]" in notification.title:
                assert "restart_integration" in notification.title
                assert "switch.zwave" in notification.title
                assert "Still unavailable" in notification.message
                break
        else:
            raise AssertionError("No VERIFY_FAILED notification dispatched")

    async def test_verify_success_no_failure_notification(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        _add_handler_and_registry(
            orchestrator,
            verify_result=VerifyResult(verified=True, message="OK"),
        )

        await orchestrator._process_one(event)

        # Check no VERIFY_FAILED notification was sent
        for call in mocks["dispatcher"].dispatch.call_args_list:
            notification = call[0][0]
            assert "[VERIFY_FAILED]" not in notification.title


class TestVerifyException:
    async def test_verify_exception_treated_as_failure(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        mock_handler = _add_handler_and_registry(orchestrator)
        mock_handler.verify.side_effect = RuntimeError("connection lost")

        await orchestrator._process_one(event)

        # Audited as verify failure
        mocks["audit"].write_verify.assert_called_once()
        verify_result = mocks["audit"].write_verify.call_args[0][2]
        assert verify_result.verified is False
        assert "connection lost" in verify_result.message

        # Notification sent
        found_verify_failed = False
        for call in mocks["dispatcher"].dispatch.call_args_list:
            notification = call[0][0]
            if "[VERIFY_FAILED]" in notification.title:
                found_verify_failed = True
                break
        assert found_verify_failed

    async def test_verify_exception_does_not_propagate(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()
        mocks["decision"].process_event.return_value = _matched_result(event.id)
        mock_handler = _add_handler_and_registry(orchestrator)
        mock_handler.verify.side_effect = RuntimeError("connection lost")

        # Should not raise — error is caught and logged
        await orchestrator._process_one(event)

        assert orchestrator._events_processed == 1
        assert orchestrator._errors == 0


class TestVerifyInT2Dispatch:
    async def test_verify_called_for_t2_auto_fix_actions(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)
        event = _make_event()

        action = _make_action()
        t2_result = DecisionResult(
            event_id=event.id,
            tier=DecisionTier.T2,
            disposition=DecisionDisposition.MATCHED,
            diagnosis="T2 diagnosis",
            recommended_actions=[action],
        )
        mocks["decision"].process_event.return_value = t2_result

        mock_handler = AsyncMock()
        mock_handler.name.return_value = "homeassistant"
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        mock_handler.verify.return_value = VerifyResult(
            verified=True, message="OK"
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        await orchestrator._process_one(event)

        mock_handler.verify.assert_called_once()


class TestVerifyInApprovalDispatch:
    async def test_verify_called_for_approved_action(self) -> None:
        orchestrator = _setup_orchestrator()
        _mock_components(orchestrator)

        mock_handler = AsyncMock()
        mock_handler.name.return_value = "homeassistant"
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        mock_handler.verify.return_value = VerifyResult(
            verified=True, message="Recovered"
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        # Add a pending action
        from oasisagent.approval.pending import PendingQueue

        orchestrator._pending_queue = PendingQueue()
        action = _make_action()
        action = action.model_copy(update={"risk_tier": RiskTier.RECOMMEND})
        pending = orchestrator._pending_queue.add(
            event_id="evt-1",
            action=action,
            diagnosis="Test",
            timeout_minutes=30,
        )

        await orchestrator._process_approval(pending.id)

        mock_handler.verify.assert_called_once()


class TestVerifyDoesNotBlockPipeline:
    async def test_verify_failure_does_not_block_next_event(self) -> None:
        orchestrator = _setup_orchestrator()
        mocks = _mock_components(orchestrator)

        # First event: verify fails
        event1 = _make_event(entity_id="sensor.one")
        mock_handler = _add_handler_and_registry(
            orchestrator,
            verify_result=VerifyResult(verified=False, message="Failed"),
        )
        mocks["decision"].process_event.return_value = _matched_result(event1.id)
        await orchestrator._process_one(event1)

        # Second event: should still process normally
        event2 = _make_event(entity_id="sensor.two")
        mock_handler.verify.return_value = VerifyResult(
            verified=True, message="OK"
        )
        mocks["decision"].process_event.return_value = _matched_result(event2.id)
        await orchestrator._process_one(event2)

        assert orchestrator._events_processed == 2
        assert mock_handler.verify.call_count == 2


class TestVerifyDryRunSkipped:
    async def test_verify_skipped_when_dry_run_enabled(self) -> None:
        config = _make_config(dry_run=True)
        orchestrator = _setup_orchestrator(config)
        _mock_components(orchestrator)
        event = _make_event()

        mock_handler = AsyncMock()
        mock_handler.name.return_value = "homeassistant"
        mock_handler.execute.return_value = ActionResult(
            status=ActionStatus.SUCCESS
        )
        orchestrator._handlers["homeassistant"] = mock_handler

        action = _make_action()
        action_result = ActionResult(status=ActionStatus.SUCCESS)

        result = await orchestrator._verify_action(
            mock_handler, event, action, action_result
        )

        assert result is None
        mock_handler.verify.assert_not_called()
