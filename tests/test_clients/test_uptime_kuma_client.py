"""Tests for the Uptime Kuma Prometheus metrics client."""

from __future__ import annotations

import pytest

from oasisagent.clients.uptime_kuma import UptimeKumaClient

# ---------------------------------------------------------------------------
# Prometheus text parsing
# ---------------------------------------------------------------------------

# Shortened labels to stay within line length limits. Real Uptime Kuma
# output includes monitor_hostname and monitor_port but the parser handles
# their absence gracefully.

_SAMPLE_METRICS = (
    '# HELP monitor_status Monitor Status (1 = UP, 0= DOWN)\n'
    '# TYPE monitor_status gauge\n'
    'monitor_status{monitor_name="Google",monitor_type="http",'
    'monitor_url="https://google.com",monitor_hostname="",'
    'monitor_port=""} 1\n'
    'monitor_status{monitor_name="NAS",monitor_type="http",'
    'monitor_url="https://nas.local:8443",monitor_hostname="",'
    'monitor_port=""} 0\n'
    '# HELP monitor_response_time Monitor Response Time (ms)\n'
    '# TYPE monitor_response_time gauge\n'
    'monitor_response_time{monitor_name="Google",monitor_type="http",'
    'monitor_url="https://google.com",monitor_hostname="",'
    'monitor_port=""} 42\n'
    'monitor_response_time{monitor_name="NAS",monitor_type="http",'
    'monitor_url="https://nas.local:8443",monitor_hostname="",'
    'monitor_port=""} 0\n'
    '# HELP monitor_cert_days_remaining Cert Days Remaining\n'
    '# TYPE monitor_cert_days_remaining gauge\n'
    'monitor_cert_days_remaining{monitor_name="Google",'
    'monitor_type="http",monitor_url="https://google.com",'
    'monitor_hostname="",monitor_port=""} 45\n'
    '# HELP monitor_cert_is_valid Monitor Cert Is Valid\n'
    '# TYPE monitor_cert_is_valid gauge\n'
    'monitor_cert_is_valid{monitor_name="Google",'
    'monitor_type="http",monitor_url="https://google.com",'
    'monitor_hostname="",monitor_port=""} 1\n'
)

_MINIMAL_METRICS = (
    'monitor_status{monitor_name="Test",monitor_type="http",'
    'monitor_url="http://test.local",monitor_hostname="",'
    'monitor_port=""} 1\n'
)

_EMPTY_METRICS = (
    '# HELP some_other_metric Not our concern\n'
    '# TYPE some_other_metric gauge\n'
    'some_other_metric{foo="bar"} 42\n'
)


class TestPrometheusParser:
    def test_parse_full_metrics(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        monitors = client._parse_metrics(_SAMPLE_METRICS)

        assert len(monitors) == 2

        google = next(m for m in monitors if m.name == "Google")
        assert google.status == 1
        assert google.response_time_ms == 42.0
        assert google.cert_days_remaining == 45
        assert google.cert_is_valid is True
        assert google.monitor_type == "http"
        assert google.url == "https://google.com"

        nas = next(m for m in monitors if m.name == "NAS")
        assert nas.status == 0
        assert nas.response_time_ms == 0.0
        assert nas.cert_days_remaining is None
        assert nas.cert_is_valid is None

    def test_parse_minimal(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        monitors = client._parse_metrics(_MINIMAL_METRICS)
        assert len(monitors) == 1
        assert monitors[0].name == "Test"
        assert monitors[0].status == 1
        assert monitors[0].response_time_ms is None

    def test_parse_empty(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        monitors = client._parse_metrics(_EMPTY_METRICS)
        assert monitors == []

    def test_parse_blank_input(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        monitors = client._parse_metrics("")
        assert monitors == []

    def test_parse_ignores_comments_and_type_lines(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        metrics = "# HELP foo bar\n# TYPE foo gauge\n"
        monitors = client._parse_metrics(metrics)
        assert monitors == []

    def test_parse_ignores_unknown_metrics(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        metrics = (
            'unknown_metric{monitor_name="X",'
            'monitor_type="http",'
            'monitor_url="http://x.com",'
            'monitor_hostname="",monitor_port=""} 1\n'
        )
        monitors = client._parse_metrics(metrics)
        assert monitors == []

    def test_missing_monitor_name_skipped(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        metrics = 'monitor_status{monitor_type="http"} 1\n'
        monitors = client._parse_metrics(metrics)
        assert monitors == []


class TestClientLifecycle:
    @pytest.mark.asyncio
    async def test_fetch_without_start_raises(self) -> None:
        client = UptimeKumaClient("http://localhost:3001", "test-key")
        with pytest.raises(RuntimeError, match="not started"):
            await client.fetch_metrics()
