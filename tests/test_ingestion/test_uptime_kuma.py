"""Tests for the Uptime Kuma ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from oasisagent.clients.uptime_kuma import MonitorMetrics
from oasisagent.config import UptimeKumaAdapterConfig
from oasisagent.ingestion.uptime_kuma import UptimeKumaAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> UptimeKumaAdapterConfig:
    defaults: dict = {
        "enabled": True,
        "url": "http://uptime-kuma:3001",
        "api_key": "uk2_test",
        "poll_interval": 60,
        "timeout": 10,
        "response_time_threshold_ms": 5000,
        "cert_warning_days": 30,
        "cert_critical_days": 7,
    }
    defaults.update(overrides)
    return UptimeKumaAdapterConfig(**defaults)


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(
    config: UptimeKumaAdapterConfig | None = None,
    queue: MagicMock | None = None,
) -> UptimeKumaAdapter:
    return UptimeKumaAdapter(
        config=config or _make_config(),
        queue=queue or _mock_queue(),
    )


def _make_monitor(**overrides: object) -> MonitorMetrics:
    defaults: dict = {
        "name": "Google",
        "monitor_type": "http",
        "url": "https://google.com",
        "hostname": "",
        "port": "",
        "status": 1,
        "response_time_ms": 42.0,
        "cert_days_remaining": 45,
        "cert_is_valid": True,
    }
    defaults.update(overrides)
    return MonitorMetrics(**defaults)


# ---------------------------------------------------------------------------
# Name and config
# ---------------------------------------------------------------------------


class TestAdapterBasics:
    def test_name(self) -> None:
        adapter = _make_adapter()
        assert adapter.name == "uptime_kuma"

    def test_config_defaults(self) -> None:
        cfg = UptimeKumaAdapterConfig()
        assert cfg.enabled is False
        assert cfg.poll_interval == 60
        assert cfg.response_time_threshold_ms == 5000


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


class TestStatusTransitions:
    def test_first_poll_up_no_event(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_status(_make_monitor(status=1))
        assert events == []

    def test_first_poll_down_emits(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_status(_make_monitor(status=0))
        assert len(events) == 1
        assert events[0].event_type == "monitor_down"
        assert events[0].severity == Severity.ERROR

    def test_up_to_down_emits(self) -> None:
        adapter = _make_adapter()
        adapter._check_status(_make_monitor(status=1))
        events = adapter._check_status(_make_monitor(status=0))
        assert len(events) == 1
        assert events[0].event_type == "monitor_down"

    def test_down_to_up_emits_recovery(self) -> None:
        adapter = _make_adapter()
        adapter._check_status(_make_monitor(status=0))
        events = adapter._check_status(_make_monitor(status=1))
        assert len(events) == 1
        assert events[0].event_type == "monitor_recovered"
        assert events[0].severity == Severity.INFO

    def test_same_status_dedup(self) -> None:
        adapter = _make_adapter()
        adapter._check_status(_make_monitor(status=1))
        events = adapter._check_status(_make_monitor(status=1))
        assert events == []

    def test_dedup_key_includes_monitor_name(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_status(_make_monitor(name="NAS", status=0))
        assert events[0].metadata.dedup_key == "uptime_kuma:NAS"

    def test_independent_monitor_tracking(self) -> None:
        adapter = _make_adapter()
        adapter._check_status(_make_monitor(name="A", status=1))
        events = adapter._check_status(_make_monitor(name="B", status=0))
        assert len(events) == 1
        assert events[0].entity_id == "B"


# ---------------------------------------------------------------------------
# Response time (slow) transitions
# ---------------------------------------------------------------------------


class TestResponseTimeTransitions:
    def test_no_response_time_no_event(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_response_time(
            _make_monitor(response_time_ms=None)
        )
        assert events == []

    def test_below_threshold_no_event(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_response_time(
            _make_monitor(response_time_ms=1000)
        )
        assert events == []

    def test_above_threshold_emits_slow(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_response_time(
            _make_monitor(response_time_ms=6000)
        )
        assert len(events) == 1
        assert events[0].event_type == "monitor_slow"
        assert events[0].severity == Severity.WARNING
        assert events[0].payload["response_time_ms"] == 6000

    def test_slow_same_state_dedup(self) -> None:
        adapter = _make_adapter()
        adapter._check_response_time(_make_monitor(response_time_ms=6000))
        events = adapter._check_response_time(
            _make_monitor(response_time_ms=7000)
        )
        assert events == []  # still slow, no new event

    def test_hysteresis_prevents_flapping(self) -> None:
        """Must drop below 80% of threshold to clear slow state."""
        adapter = _make_adapter()  # threshold=5000
        adapter._check_response_time(_make_monitor(response_time_ms=6000))
        # 4500 is above 5000*0.8=4000 — still in hysteresis band
        events = adapter._check_response_time(
            _make_monitor(response_time_ms=4500)
        )
        assert events == []  # still considered slow

    def test_recovery_below_hysteresis(self) -> None:
        adapter = _make_adapter()  # threshold=5000
        adapter._check_response_time(_make_monitor(response_time_ms=6000))
        # 3000 is below 5000*0.8=4000 — clears hysteresis
        events = adapter._check_response_time(
            _make_monitor(response_time_ms=3000)
        )
        assert len(events) == 1
        assert events[0].event_type == "monitor_slow_recovered"
        assert events[0].severity == Severity.INFO


# ---------------------------------------------------------------------------
# Certificate transitions
# ---------------------------------------------------------------------------


class TestCertTransitions:
    def test_no_cert_data_no_event(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_cert(
            _make_monitor(cert_days_remaining=None)
        )
        assert events == []

    def test_cert_ok_first_poll_no_event(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_cert(
            _make_monitor(cert_days_remaining=90)
        )
        assert events == []

    def test_cert_warning_emits(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_cert(
            _make_monitor(cert_days_remaining=20)
        )
        assert len(events) == 1
        assert events[0].event_type == "certificate_expiry"
        assert events[0].severity == Severity.WARNING
        assert events[0].payload["state"] == "warning"

    def test_cert_critical_emits(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_cert(
            _make_monitor(cert_days_remaining=3)
        )
        assert len(events) == 1
        assert events[0].event_type == "certificate_expiry"
        assert events[0].severity == Severity.ERROR
        assert events[0].payload["state"] == "critical"

    def test_cert_same_state_dedup(self) -> None:
        adapter = _make_adapter()
        adapter._check_cert(_make_monitor(cert_days_remaining=20))
        events = adapter._check_cert(_make_monitor(cert_days_remaining=18))
        assert events == []

    def test_cert_warning_to_critical(self) -> None:
        adapter = _make_adapter()
        adapter._check_cert(_make_monitor(cert_days_remaining=20))
        events = adapter._check_cert(_make_monitor(cert_days_remaining=3))
        assert len(events) == 1
        assert events[0].severity == Severity.ERROR

    def test_cert_recovery(self) -> None:
        adapter = _make_adapter()
        adapter._check_cert(_make_monitor(cert_days_remaining=3))
        events = adapter._check_cert(_make_monitor(cert_days_remaining=90))
        assert len(events) == 1
        assert events[0].event_type == "certificate_renewed"
        assert events[0].payload["previous_state"] == "critical"

    def test_cert_dedup_key_has_suffix(self) -> None:
        adapter = _make_adapter()
        events = adapter._check_cert(_make_monitor(cert_days_remaining=3))
        assert events[0].metadata.dedup_key == "uptime_kuma:Google:cert"


# ---------------------------------------------------------------------------
# Full process_monitors
# ---------------------------------------------------------------------------


class TestProcessMonitors:
    def test_processes_multiple_monitors(self) -> None:
        adapter = _make_adapter()
        monitors = [
            _make_monitor(name="A", status=0),  # first poll down
            _make_monitor(name="B", status=1, response_time_ms=6000),  # slow
        ]
        events = adapter._process_monitors(monitors)
        types = {e.event_type for e in events}
        assert "monitor_down" in types
        assert "monitor_slow" in types

    def test_event_source_and_system(self) -> None:
        adapter = _make_adapter()
        events = adapter._process_monitors([_make_monitor(status=0)])
        assert events[0].source == "uptime_kuma"
        assert events[0].system == "uptime_kuma"

    def test_event_payload_includes_monitor_info(self) -> None:
        adapter = _make_adapter()
        events = adapter._process_monitors([_make_monitor(status=0)])
        assert events[0].payload["monitor_type"] == "http"
        assert events[0].payload["url"] == "https://google.com"


# ---------------------------------------------------------------------------
# Startup retry behaviour
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


class TestStartupRetry:
    @pytest.mark.asyncio
    async def test_retries_on_initial_connection_failure(self) -> None:
        """start() should retry with back-off, not return permanently."""
        adapter = _make_adapter()

        call_count = 0
        original_sleep = asyncio.sleep

        async def _start_side_effect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("refused")

        async def _noop_sleep(_: float) -> None:
            await original_sleep(0)

        adapter._client = AsyncMock()
        adapter._client.start = AsyncMock(side_effect=_start_side_effect)
        adapter._client.fetch_metrics = AsyncMock(return_value=[])
        adapter._client.close = AsyncMock()

        with patch("oasisagent.ingestion.uptime_kuma.asyncio.sleep", side_effect=_noop_sleep):
            task = asyncio.create_task(adapter.start())
            # Let event loop drain — retries + poll loop start
            for _ in range(200):
                await original_sleep(0)
            adapter._stopping = True
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()

        assert call_count >= 3
        assert adapter._connected is True
