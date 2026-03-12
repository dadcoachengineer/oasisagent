"""Tests for the HTTP polling ingestion adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from oasisagent.config import (
    ExtractMapping,
    HttpPollerTargetConfig,
    ThresholdConfig,
)
from oasisagent.ingestion.http_poller import HttpPollerAdapter
from oasisagent.models import Severity


def _make_target(**overrides: object) -> HttpPollerTargetConfig:
    """Create an HttpPollerTargetConfig with sensible defaults."""
    defaults = {
        "url": "http://localhost:8080/health",
        "system": "test-service",
        "mode": "health_check",
        "interval": 5,
    }
    defaults.update(overrides)
    return HttpPollerTargetConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_extract_mode_requires_extract_config(self) -> None:
        with pytest.raises(ValidationError, match="extract config is required"):
            _make_target(mode="extract")

    def test_threshold_mode_requires_threshold_config(self) -> None:
        with pytest.raises(ValidationError, match="threshold config is required"):
            _make_target(mode="threshold")

    def test_minimum_interval(self) -> None:
        with pytest.raises(ValidationError):
            _make_target(interval=2)

    def test_valid_extract_config(self) -> None:
        target = _make_target(
            mode="extract",
            extract={"entity_id": "name", "event_type": "status_update"},
        )
        assert target.mode == "extract"
        assert target.extract is not None

    def test_valid_threshold_config(self) -> None:
        target = _make_target(
            mode="threshold",
            threshold={
                "value_expr": "cpu_percent",
                "warning": 80.0,
                "critical": 95.0,
                "entity_id": "node-1",
            },
        )
        assert target.threshold is not None
        assert target.threshold.critical == 95.0

    def test_threshold_critical_must_exceed_warning(self) -> None:
        with pytest.raises(ValidationError, match=r"critical.*must be greater than warning"):
            _make_target(
                mode="threshold",
                threshold={
                    "value_expr": "cpu",
                    "warning": 95.0,
                    "critical": 80.0,
                    "entity_id": "node-1",
                },
            )

    def test_health_check_no_extra_config_needed(self) -> None:
        target = _make_target()
        assert target.mode == "health_check"


# ---------------------------------------------------------------------------
# Health check mode
# ---------------------------------------------------------------------------


class TestHealthCheckMode:
    def test_200_no_event(self) -> None:
        target = _make_target()
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_health_check(target, 200)
        queue.put_nowait.assert_not_called()

    def test_non_200_emits_event(self) -> None:
        target = _make_target()
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_health_check(target, 503)
        queue.put_nowait.assert_called_once()

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "health_check_failed"
        assert event.severity == Severity.ERROR
        assert event.system == "test-service"
        assert event.payload["status_code"] == 503

    def test_state_transition_dedup(self) -> None:
        """Consecutive failures should emit only one event."""
        target = _make_target()
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_health_check(target, 503)  # first failure
        adapter._handle_health_check(target, 503)  # same state
        assert queue.put_nowait.call_count == 1

    def test_recovery_emits_info(self) -> None:
        target = _make_target()
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_health_check(target, 503)  # fail
        adapter._handle_health_check(target, 200)  # recover

        assert queue.put_nowait.call_count == 2
        recovery = queue.put_nowait.call_args_list[1][0][0]
        assert recovery.event_type == "health_check_recovered"
        assert recovery.severity == Severity.INFO

    def test_healthy_to_healthy_no_event(self) -> None:
        target = _make_target()
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_health_check(target, 200)  # first healthy
        adapter._handle_health_check(target, 200)  # still healthy
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Extract mode
# ---------------------------------------------------------------------------


class TestExtractMode:
    def test_jmespath_extracts_entity_id(self) -> None:
        target = _make_target(
            mode="extract",
            extract=ExtractMapping(
                entity_id="hostname",
                event_type="status_update",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_extract(target, {"hostname": "node-1", "status": "degraded"})

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "node-1"
        assert event.event_type == "status_update"

    def test_jmespath_extracts_severity(self) -> None:
        target = _make_target(
            mode="extract",
            extract=ExtractMapping(
                entity_id="name",
                event_type="alert",
                severity_expr="severity",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_extract(target, {
            "name": "disk-1",
            "severity": "critical",
        })

        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.CRITICAL

    def test_jmespath_extracts_payload_subset(self) -> None:
        target = _make_target(
            mode="extract",
            extract=ExtractMapping(
                entity_id="name",
                event_type="metrics",
                payload_expr="metrics",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_extract(target, {
            "name": "svc-1",
            "metrics": {"cpu": 45, "mem": 80},
            "extra": "ignored",
        })

        event = queue.put_nowait.call_args[0][0]
        assert event.payload == {"cpu": 45, "mem": 80}
        assert "extra" not in event.payload

    def test_missing_entity_id_uses_system(self) -> None:
        target = _make_target(
            mode="extract",
            extract=ExtractMapping(
                entity_id="nonexistent",
                event_type="alert",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_extract(target, {"data": "value"})

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "test-service"

    def test_nested_jmespath(self) -> None:
        target = _make_target(
            mode="extract",
            extract=ExtractMapping(
                entity_id="node.hostname",
                event_type="node_status",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_extract(target, {
            "node": {"hostname": "pve-01", "status": "online"},
        })

        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "pve-01"

    def test_default_severity_is_warning(self) -> None:
        target = _make_target(
            mode="extract",
            extract=ExtractMapping(entity_id="name", event_type="test"),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_extract(target, {"name": "x"})

        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Threshold mode
# ---------------------------------------------------------------------------


class TestThresholdMode:
    def test_below_warning_no_event(self) -> None:
        target = _make_target(
            mode="threshold",
            threshold=ThresholdConfig(
                value_expr="cpu",
                warning=80.0,
                critical=95.0,
                entity_id="node-1",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_threshold(target, {"cpu": 50.0})
        queue.put_nowait.assert_not_called()

    def test_above_warning_emits_warning(self) -> None:
        target = _make_target(
            mode="threshold",
            threshold=ThresholdConfig(
                value_expr="cpu",
                warning=80.0,
                critical=95.0,
                entity_id="node-1",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_threshold(target, {"cpu": 85.0})

        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.WARNING
        assert event.payload["value"] == 85.0

    def test_above_critical_emits_error(self) -> None:
        target = _make_target(
            mode="threshold",
            threshold=ThresholdConfig(
                value_expr="cpu",
                warning=80.0,
                critical=95.0,
                entity_id="node-1",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_threshold(target, {"cpu": 97.0})

        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.ERROR
        assert event.payload["level"] == "critical"

    def test_state_dedup(self) -> None:
        """Same threshold state on consecutive polls emits only once."""
        target = _make_target(
            mode="threshold",
            threshold=ThresholdConfig(
                value_expr="cpu",
                warning=80.0,
                critical=95.0,
                entity_id="node-1",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_threshold(target, {"cpu": 85.0})  # ok→warning
        adapter._handle_threshold(target, {"cpu": 87.0})  # warning→warning
        assert queue.put_nowait.call_count == 1

    def test_recovery_emits_info(self) -> None:
        target = _make_target(
            mode="threshold",
            threshold=ThresholdConfig(
                value_expr="cpu",
                warning=80.0,
                critical=95.0,
                entity_id="node-1",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_threshold(target, {"cpu": 85.0})  # ok→warning
        adapter._handle_threshold(target, {"cpu": 50.0})  # warning→ok

        assert queue.put_nowait.call_count == 2
        recovery = queue.put_nowait.call_args_list[1][0][0]
        assert recovery.event_type == "threshold_exceeded_recovered"
        assert recovery.severity == Severity.INFO

    def test_warning_to_critical_transition(self) -> None:
        target = _make_target(
            mode="threshold",
            threshold=ThresholdConfig(
                value_expr="cpu",
                warning=80.0,
                critical=95.0,
                entity_id="node-1",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_threshold(target, {"cpu": 85.0})  # ok→warning
        adapter._handle_threshold(target, {"cpu": 97.0})  # warning→critical

        assert queue.put_nowait.call_count == 2
        critical_event = queue.put_nowait.call_args_list[1][0][0]
        assert critical_event.severity == Severity.ERROR

    def test_non_numeric_value_no_event(self) -> None:
        target = _make_target(
            mode="threshold",
            threshold=ThresholdConfig(
                value_expr="cpu",
                warning=80.0,
                critical=95.0,
                entity_id="node-1",
            ),
        )
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        adapter._handle_threshold(target, {"cpu": "not_a_number"})
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Connection errors
# ---------------------------------------------------------------------------


class TestConnectionErrors:
    def test_connection_error_emits_event(self) -> None:
        target = _make_target()
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        import aiohttp

        adapter._emit_connection_error(target, aiohttp.ClientError("refused"))

        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "connection_error"
        assert event.severity == Severity.ERROR

    def test_duplicate_connection_error_no_spam(self) -> None:
        """Second consecutive connection error should not emit."""
        target = _make_target()
        queue = _mock_queue()
        adapter = HttpPollerAdapter([target], queue)

        import aiohttp

        adapter._emit_connection_error(target, aiohttp.ClientError("first"))
        adapter._emit_connection_error(target, aiohttp.ClientError("second"))
        assert queue.put_nowait.call_count == 1


# ---------------------------------------------------------------------------
# Auth config
# ---------------------------------------------------------------------------


class TestAuthConfig:
    def test_basic_auth(self) -> None:
        target = _make_target(
            auth_mode="basic",
            auth_username="admin",
            auth_password="secret",
        )
        assert target.auth_mode == "basic"
        assert target.auth_username == "admin"

    def test_token_auth(self) -> None:
        target = _make_target(
            auth_mode="token",
            auth_header="X-API-Key",
            auth_value="mytoken",
        )
        assert target.auth_mode == "token"
        assert target.auth_header == "X-API-Key"

    def test_no_auth_default(self) -> None:
        target = _make_target()
        assert target.auth_mode == "none"

    def test_basic_auth_missing_credentials(self) -> None:
        with pytest.raises(ValidationError, match="auth_username and auth_password required"):
            _make_target(auth_mode="basic")

    def test_token_auth_missing_value(self) -> None:
        with pytest.raises(ValidationError, match="auth_value required"):
            _make_target(auth_mode="token")


# ---------------------------------------------------------------------------
# Adapter lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter = HttpPollerAdapter([], _mock_queue())
        assert adapter.name == "http_poller"

    async def test_healthy_no_targets(self) -> None:
        adapter = HttpPollerAdapter([], _mock_queue())
        assert not await adapter.healthy()

    async def test_stop_sets_flag(self) -> None:
        adapter = HttpPollerAdapter([], _mock_queue())
        await adapter.stop()
        assert adapter._stopping is True
