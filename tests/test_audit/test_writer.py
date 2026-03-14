"""Tests for the InfluxDB audit writer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.audit.influxdb import _BUFFER_MAX, AuditNotStartedError, AuditWriter
from oasisagent.config import AuditConfig, InfluxDbConfig
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(enabled: bool = True) -> AuditConfig:
    return AuditConfig(
        influxdb=InfluxDbConfig(
            enabled=enabled,
            url="http://localhost:8086",
            token="test-token",
            org="testorg",
            bucket="testbucket",
        ),
    )


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source": "mqtt",
        "system": "homeassistant",
        "event_type": "state_unavailable",
        "entity_id": "light.kitchen",
        "severity": Severity.WARNING,
        "timestamp": datetime(2026, 3, 8, 12, 0, 0, tzinfo=UTC),
        "payload": {"old_state": "on", "new_state": "unavailable"},
        "metadata": EventMetadata(correlation_id="corr-123"),
    }
    defaults.update(overrides)
    return Event(**defaults)


def _make_decision(**overrides: Any) -> DecisionResult:
    defaults: dict[str, Any] = {
        "event_id": "evt-001",
        "tier": DecisionTier.T0,
        "disposition": DecisionDisposition.MATCHED,
        "matched_fix_id": "ha-fix-1",
        "diagnosis": "Known issue",
        "guardrail_result": GuardrailResult(
            allowed=True,
            reason="ok",
            risk_tier=RiskTier.RECOMMEND,
        ),
    }
    defaults.update(overrides)
    return DecisionResult(**defaults)


def _make_action_pair() -> tuple[RecommendedAction, ActionResult]:
    action = RecommendedAction(
        description="Restart integration",
        handler="homeassistant",
        operation="restart_integration",
        params={"entry_id": "abc123"},
        risk_tier=RiskTier.AUTO_FIX,
    )
    result = ActionResult(
        status=ActionStatus.SUCCESS,
        details={"entry_id": "abc123"},
        duration_ms=150.0,
    )
    return action, result


async def _started_writer(enabled: bool = True) -> AuditWriter:
    """Create a writer with a mocked InfluxDB client."""
    writer = AuditWriter(_make_config(enabled=enabled))
    if enabled:
        writer._write_api = AsyncMock()
        writer._client = MagicMock()
        writer._client.close = AsyncMock()
    return writer


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """start/stop lifecycle follows Handler pattern."""

    @patch("oasisagent.audit.influxdb.InfluxDBClientAsync")
    async def test_start_creates_client(self, mock_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.write_api.return_value = MagicMock()
        mock_cls.return_value = mock_instance

        writer = AuditWriter(_make_config())
        await writer.start()

        mock_cls.assert_called_once_with(
            url="http://localhost:8086",
            token="test-token",
            org="testorg",
        )
        assert writer._client is not None
        assert writer._write_api is not None

    async def test_stop_closes_client(self) -> None:
        writer = await _started_writer()
        mock_client = writer._client

        await writer.stop()

        mock_client.close.assert_called_once()
        assert writer._client is None
        assert writer._write_api is None

    async def test_stop_without_start_is_noop(self) -> None:
        writer = AuditWriter(_make_config())
        await writer.stop()  # Should not raise

    async def test_write_before_start_raises(self) -> None:
        writer = AuditWriter(_make_config())

        with pytest.raises(AuditNotStartedError):
            await writer.write_event(_make_event())

    @patch("oasisagent.audit.influxdb.InfluxDBClientAsync")
    async def test_disabled_start_no_client_created(
        self, mock_cls: MagicMock
    ) -> None:
        writer = AuditWriter(_make_config(enabled=False))
        await writer.start()

        mock_cls.assert_not_called()
        assert writer._client is None


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


class TestDisabledMode:
    """All writes are no-ops when InfluxDB is disabled."""

    async def test_write_event_noop(self) -> None:
        writer = await _started_writer(enabled=False)
        await writer.write_event(_make_event())  # Should not raise

    async def test_write_decision_noop(self) -> None:
        writer = await _started_writer(enabled=False)
        await writer.write_decision(_make_event(), _make_decision())

    async def test_write_action_noop(self) -> None:
        writer = await _started_writer(enabled=False)
        action, result = _make_action_pair()
        await writer.write_action(_make_event(), action, result)

    async def test_write_circuit_breaker_noop(self) -> None:
        writer = await _started_writer(enabled=False)
        cb_result = CircuitBreakerResult(
            allowed=False, reason="tripped", entity_tripped=True
        )
        await writer.write_circuit_breaker("light.kitchen", cb_result)


# ---------------------------------------------------------------------------
# write_event
# ---------------------------------------------------------------------------


class TestWriteEvent:
    async def test_writes_correct_point(self) -> None:
        writer = await _started_writer()
        event = _make_event()

        await writer.write_event(event)

        writer._write_api.write.assert_called_once()
        call_kwargs = writer._write_api.write.call_args.kwargs
        assert call_kwargs["bucket"] == "testbucket"

        point = call_kwargs["record"]
        line = point.to_line_protocol()
        assert "oasis_event" in line
        assert "source=mqtt" in line
        assert "system=homeassistant" in line
        assert "event_type=state_unavailable" in line
        assert "entity_id=light.kitchen" in line
        assert "severity=warning" in line
        assert "corr-123" in line

    async def test_payload_serialized_as_json(self) -> None:
        writer = await _started_writer()
        event = _make_event(payload={"key": "value"})

        await writer.write_event(event)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        # Line protocol escapes inner quotes: {\"key\": \"value\"}
        assert "key" in line
        assert "value" in line


# ---------------------------------------------------------------------------
# write_decision
# ---------------------------------------------------------------------------


class TestWriteDecision:
    async def test_writes_correct_point(self) -> None:
        writer = await _started_writer()
        event = _make_event()
        decision = _make_decision()

        await writer.write_decision(event, decision)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        assert "oasis_decision" in line
        assert "tier=t0" in line
        assert "disposition=matched" in line
        assert "risk_tier=recommend" in line
        assert "Known issue" in line

    async def test_includes_event_timestamp_field(self) -> None:
        writer = await _started_writer()
        event = _make_event()

        await writer.write_decision(event, _make_decision())

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        assert "event_timestamp=" in line

    async def test_no_guardrail_result_empty_risk_tier(self) -> None:
        writer = await _started_writer()
        decision = _make_decision(guardrail_result=None)

        await writer.write_decision(_make_event(), decision)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        # risk_tier tag should be empty string (InfluxDB omits empty tags)
        assert "oasis_decision" in line


# ---------------------------------------------------------------------------
# write_action
# ---------------------------------------------------------------------------


class TestWriteAction:
    async def test_writes_correct_point(self) -> None:
        writer = await _started_writer()
        event = _make_event()
        action, result = _make_action_pair()

        await writer.write_action(event, action, result)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        assert "oasis_action" in line
        assert "handler=homeassistant" in line
        assert "operation=restart_integration" in line
        assert "action_status=success" in line
        assert "duration_ms=150" in line

    async def test_failure_includes_error_message(self) -> None:
        writer = await _started_writer()
        action = RecommendedAction(
            description="Test",
            handler="homeassistant",
            operation="notify",
            risk_tier=RiskTier.RECOMMEND,
        )
        result = ActionResult(
            status=ActionStatus.FAILURE,
            error_message="Connection refused",
        )

        await writer.write_action(_make_event(), action, result)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        assert "Connection refused" in line


# ---------------------------------------------------------------------------
# write_circuit_breaker
# ---------------------------------------------------------------------------


class TestWriteCircuitBreaker:
    async def test_entity_tripped(self) -> None:
        writer = await _started_writer()
        cb = CircuitBreakerResult(
            allowed=False, reason="3 attempts", entity_tripped=True
        )

        await writer.write_circuit_breaker("light.kitchen", cb)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        assert "oasis_circuit_breaker" in line
        assert "entity_id=light.kitchen" in line
        assert "trigger_type=entity" in line

    async def test_global_tripped(self) -> None:
        writer = await _started_writer()
        cb = CircuitBreakerResult(
            allowed=False, reason="failure rate", global_tripped=True
        )

        await writer.write_circuit_breaker("entity.new", cb)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        assert "trigger_type=global" in line

    async def test_cooldown(self) -> None:
        writer = await _started_writer()
        cb = CircuitBreakerResult(
            allowed=False, reason="cooldown active", entity_cooldown=True
        )

        await writer.write_circuit_breaker("light.kitchen", cb)

        point = writer._write_api.write.call_args.kwargs["record"]
        line = point.to_line_protocol()
        assert "trigger_type=cooldown" in line


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_write_error_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        writer = await _started_writer()
        writer._write_api.write.side_effect = Exception("connection refused")

        import logging

        with caplog.at_level(logging.WARNING, logger="oasisagent.audit.influxdb"):
            await writer.write_event(_make_event())

        assert any("Audit write failed" in r.message for r in caplog.records)
        # Should NOT raise

    async def test_non_retryable_error_buffers_immediately(self) -> None:
        """A non-retryable error should buffer after 1 attempt (no retry)."""
        writer = await _started_writer()
        writer._write_api.write.side_effect = ValueError("bad data")

        await writer.write_event(_make_event())

        # Only 1 call (no retries for non-retryable errors)
        assert writer._write_api.write.call_count == 1
        assert writer.buffer_size == 1

    @patch("oasisagent.audit.influxdb.asyncio.sleep", new_callable=AsyncMock)
    async def test_retryable_error_retries_then_buffers(
        self, mock_sleep: AsyncMock
    ) -> None:
        """ConnectionError should retry 3 times, then buffer."""
        writer = await _started_writer()
        writer._write_api.write.side_effect = ConnectionError("refused")

        await writer.write_event(_make_event())

        # 3 attempts total (1 initial + 2 retries)
        assert writer._write_api.write.call_count == 3
        assert writer.buffer_size == 1
        # Backoff: 0.5s, 1.0s
        assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# Retry buffer
# ---------------------------------------------------------------------------


class TestRetryBuffer:
    """Tests for the in-memory retry buffer for failed writes."""

    async def test_failed_write_buffered(self) -> None:
        writer = await _started_writer()
        writer._write_api.write.side_effect = ValueError("fail")

        await writer.write_event(_make_event())

        assert writer.buffer_size == 1

    async def test_buffer_drained_on_next_success(self) -> None:
        writer = await _started_writer()

        # First write fails — point is buffered
        writer._write_api.write.side_effect = ValueError("fail")
        await writer.write_event(_make_event())
        assert writer.buffer_size == 1

        # Second write succeeds — buffer should drain
        writer._write_api.write.side_effect = None
        writer._write_api.write.reset_mock()
        await writer.write_event(_make_event())

        # 1 call for the new point + 1 call to drain the buffered point
        assert writer._write_api.write.call_count == 2
        assert writer.buffer_size == 0

    async def test_buffer_drain_stops_on_failure(self) -> None:
        writer = await _started_writer()

        # Buffer 2 points
        writer._write_api.write.side_effect = ValueError("fail")
        await writer.write_event(_make_event())
        await writer.write_event(_make_event())
        assert writer.buffer_size == 2

        # Next write succeeds for the new point, but drain fails
        call_count = 0

        async def succeed_then_fail(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise ConnectionError("down again")

        writer._write_api.write.side_effect = succeed_then_fail
        writer._write_api.write.reset_mock()
        await writer.write_event(_make_event())

        # New point succeeded (call 1), drain attempt failed (call 2)
        assert writer.buffer_size == 2  # both still in buffer

    async def test_buffer_bounded(self) -> None:
        writer = await _started_writer()
        writer._write_api.write.side_effect = ValueError("fail")

        # Fill buffer past max
        for _ in range(_BUFFER_MAX + 5):
            await writer.write_event(_make_event())

        assert writer.buffer_size == _BUFFER_MAX

    async def test_buffer_cleared_on_stop(self) -> None:
        writer = await _started_writer()
        writer._write_api.write.side_effect = ValueError("fail")

        await writer.write_event(_make_event())
        assert writer.buffer_size == 1

        await writer.stop()
        assert writer.buffer_size == 0

    async def test_stop_warns_about_buffered_points(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        writer = await _started_writer()
        writer._write_api.write.side_effect = ValueError("fail")
        await writer.write_event(_make_event())

        with caplog.at_level(logging.WARNING, logger="oasisagent.audit.influxdb"):
            await writer.stop()

        assert any("buffered points" in r.message for r in caplog.records)

    async def test_multiple_buffered_points_drain_in_order(self) -> None:
        writer = await _started_writer()

        # Buffer 3 points
        writer._write_api.write.side_effect = ValueError("fail")
        for _ in range(3):
            await writer.write_event(_make_event())
        assert writer.buffer_size == 3

        # All succeed on next write
        writer._write_api.write.side_effect = None
        writer._write_api.write.reset_mock()
        await writer.write_event(_make_event())

        # 1 new point + 3 drained = 4 calls
        assert writer._write_api.write.call_count == 4
        assert writer.buffer_size == 0

    async def test_buffer_size_property(self) -> None:
        writer = await _started_writer()
        assert writer.buffer_size == 0

        writer._write_api.write.side_effect = ValueError("fail")
        await writer.write_event(_make_event())
        assert writer.buffer_size == 1

        await writer.write_event(_make_event())
        assert writer.buffer_size == 2
