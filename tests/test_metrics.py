"""Tests for the Prometheus metrics module."""

from __future__ import annotations

import time

from oasisagent.approval.pending import PendingQueue
from oasisagent.engine.queue import EventQueue
from oasisagent.metrics import (
    ACTIONS_TOTAL,
    CIRCUIT_BREAKER_TRIPS_TOTAL,
    DECISIONS_TOTAL,
    EVENT_PROCESSING_SECONDS,
    EVENTS_TOTAL,
    PENDING_ACTIONS,
    QUEUE_DEPTH,
    REGISTRY,
    UPTIME_SECONDS,
    MetricsServer,
    _metrics_handler,
    _update_callback_gauges,
    inc_actions,
    inc_circuit_breaker_trips,
    inc_decisions,
    inc_events,
    observe_processing_time,
    set_callback_sources,
)
from oasisagent.models import RecommendedAction, RiskTier


class TestCustomRegistry:
    def test_registry_is_not_default(self) -> None:
        """Custom registry isolates from global default."""
        from prometheus_client import REGISTRY as DEFAULT_REGISTRY

        assert REGISTRY is not DEFAULT_REGISTRY

    def test_all_metrics_on_custom_registry(self) -> None:
        """All defined metrics are registered on the custom registry."""
        collectors = REGISTRY._names_to_collectors.values()
        collector_names = {c._name for c in collectors if hasattr(c, "_name")}
        # prometheus_client stores internal _name without _total suffix for counters
        expected = {
            "oasis_events",
            "oasis_decisions",
            "oasis_actions",
            "oasis_circuit_breaker_trips",
            "oasis_event_processing_seconds",
            "oasis_queue_depth",
            "oasis_pending_actions",
            "oasis_uptime_seconds",
        }
        assert expected.issubset(collector_names)


class TestIncEvents:
    def test_increments_with_labels(self) -> None:
        before = EVENTS_TOTAL.labels(source="mqtt", severity="error")._value.get()
        inc_events("mqtt", "error")
        after = EVENTS_TOTAL.labels(source="mqtt", severity="error")._value.get()
        assert after == before + 1

    def test_different_labels_independent(self) -> None:
        before_a = EVENTS_TOTAL.labels(source="ha_ws", severity="warning")._value.get()
        before_b = EVENTS_TOTAL.labels(source="ha_ws", severity="critical")._value.get()
        inc_events("ha_ws", "warning")
        after_a = EVENTS_TOTAL.labels(source="ha_ws", severity="warning")._value.get()
        after_b = EVENTS_TOTAL.labels(source="ha_ws", severity="critical")._value.get()
        assert after_a == before_a + 1
        assert after_b == before_b


class TestIncDecisions:
    def test_increments_with_labels(self) -> None:
        before = DECISIONS_TOTAL.labels(
            tier="t0", disposition="matched", risk_tier="auto_fix"
        )._value.get()
        inc_decisions("t0", "matched", "auto_fix")
        after = DECISIONS_TOTAL.labels(
            tier="t0", disposition="matched", risk_tier="auto_fix"
        )._value.get()
        assert after == before + 1

    def test_empty_risk_tier(self) -> None:
        before = DECISIONS_TOTAL.labels(
            tier="t1", disposition="dropped", risk_tier=""
        )._value.get()
        inc_decisions("t1", "dropped")
        after = DECISIONS_TOTAL.labels(
            tier="t1", disposition="dropped", risk_tier=""
        )._value.get()
        assert after == before + 1


class TestIncActions:
    def test_increments_with_labels(self) -> None:
        before = ACTIONS_TOTAL.labels(
            handler="docker", operation="restart_container", result="success"
        )._value.get()
        inc_actions("docker", "restart_container", "success")
        after = ACTIONS_TOTAL.labels(
            handler="docker", operation="restart_container", result="success"
        )._value.get()
        assert after == before + 1


class TestIncCircuitBreakerTrips:
    def test_increments_with_label(self) -> None:
        before = CIRCUIT_BREAKER_TRIPS_TOTAL.labels(
            trigger_type="entity"
        )._value.get()
        inc_circuit_breaker_trips("entity")
        after = CIRCUIT_BREAKER_TRIPS_TOTAL.labels(
            trigger_type="entity"
        )._value.get()
        assert after == before + 1


class TestObserveProcessingTime:
    def test_records_observation(self) -> None:
        before = EVENT_PROCESSING_SECONDS.labels(tier="t2")._sum.get()
        observe_processing_time("t2", 0.5)
        after = EVENT_PROCESSING_SECONDS.labels(tier="t2")._sum.get()
        assert after >= before + 0.5


class TestCallbackGauges:
    def test_queue_depth_reads_from_event_queue(self) -> None:
        queue = EventQueue(max_size=100)
        pending = PendingQueue()
        set_callback_sources(queue, pending)

        _update_callback_gauges()
        assert QUEUE_DEPTH._value.get() == 0

    async def test_pending_actions_reads_from_pending_queue(self) -> None:
        queue = EventQueue(max_size=100)
        pending = PendingQueue()

        action_a = RecommendedAction(
            description="test",
            handler="docker",
            operation="restart_container",
            params={},
            risk_tier=RiskTier.RECOMMEND,
            target_entity_id="container.nginx",
        )
        action_b = RecommendedAction(
            description="test",
            handler="docker",
            operation="restart_container",
            params={},
            risk_tier=RiskTier.RECOMMEND,
            target_entity_id="container.redis",
        )
        await pending.add("evt-1", action_a, "test diagnosis", timeout_minutes=30)
        await pending.add("evt-2", action_b, "test diagnosis", timeout_minutes=30)

        set_callback_sources(queue, pending)
        _update_callback_gauges()
        assert PENDING_ACTIONS._value.get() == 2

    def test_uptime_increases(self) -> None:
        import oasisagent.metrics as m

        old_start = m._start_time
        m._start_time = time.monotonic() - 10.0
        try:
            _update_callback_gauges()
            assert UPTIME_SECONDS._value.get() >= 10.0
        finally:
            m._start_time = old_start


class TestMetricsHandler:
    async def test_returns_200_with_prometheus_content_type(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        request = make_mocked_request("GET", "/metrics")
        response = await _metrics_handler(request)
        assert response.status == 200
        assert "text/plain" in response.content_type

    async def test_response_contains_metric_names(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        # Ensure at least one sample exists
        inc_events("test_handler", "info")

        request = make_mocked_request("GET", "/metrics")
        response = await _metrics_handler(request)
        body = response.body.decode()
        assert "oasis_events_total" in body
        assert "oasis_decisions_total" in body
        assert "oasis_actions_total" in body
        assert "oasis_queue_depth" in body
        assert "oasis_uptime_seconds" in body


class TestMetricsServer:
    async def test_start_stop(self) -> None:
        server = MetricsServer(port=0)
        # Port 0 won't actually bind usefully, but tests the lifecycle
        # Use a high random port instead
        server = MetricsServer(port=19876)
        await server.start()
        assert server._runner is not None
        await server.stop()
        assert server._runner is None

    async def test_stop_without_start(self) -> None:
        server = MetricsServer(port=19877)
        await server.stop()  # Should not raise


class TestPendingQueueCount:
    async def test_pending_count_with_mixed_statuses(self) -> None:
        queue = PendingQueue()

        def _action(entity: str) -> RecommendedAction:
            return RecommendedAction(
                description="test",
                handler="docker",
                operation="restart_container",
                params={},
                risk_tier=RiskTier.RECOMMEND,
                target_entity_id=entity,
            )

        p1 = await queue.add("evt-1", _action("a"), "diag", timeout_minutes=30)
        p2 = await queue.add("evt-2", _action("b"), "diag", timeout_minutes=30)
        await queue.add("evt-3", _action("c"), "diag", timeout_minutes=30)

        assert queue.pending_count == 3

        await queue.approve(p1.id)
        assert queue.pending_count == 2

        await queue.reject(p2.id)
        assert queue.pending_count == 1

    def test_pending_count_empty(self) -> None:
        queue = PendingQueue()
        assert queue.pending_count == 0


class TestOrchestratorRegistration:
    async def test_metrics_server_created_when_port_set(self) -> None:
        from oasisagent.config import OasisAgentConfig
        from oasisagent.orchestrator import Orchestrator

        config = OasisAgentConfig.model_validate({
            "agent": {"metrics_port": 9090, "correlation_window": 0},
            "notifications": {"mqtt": {"enabled": False}},
            "llm": {
                "triage": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
                "reasoning": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
            },
        })

        orch = Orchestrator(config)
        orch._build_components()
        assert orch._metrics_server is not None

    async def test_metrics_server_not_created_when_disabled(self) -> None:
        from oasisagent.config import OasisAgentConfig
        from oasisagent.orchestrator import Orchestrator

        config = OasisAgentConfig.model_validate({
            "agent": {"metrics_port": 0, "correlation_window": 0},
            "notifications": {"mqtt": {"enabled": False}},
            "llm": {
                "triage": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
                "reasoning": {
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "test",
                },
            },
        })

        orch = Orchestrator(config)
        orch._build_components()
        assert orch._metrics_server is None
