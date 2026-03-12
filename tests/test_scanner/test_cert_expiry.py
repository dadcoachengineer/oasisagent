"""Tests for the TLS certificate expiry scanner."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from oasisagent.config import CertExpiryCheckConfig
from oasisagent.models import Severity
from oasisagent.scanner.cert_expiry import CertExpiryScannerAdapter


def _make_config(**overrides: object) -> CertExpiryCheckConfig:
    defaults: dict = {
        "enabled": True,
        "endpoints": ["example.com"],
        "warning_days": 30,
        "critical_days": 7,
        "interval": 60,
    }
    defaults.update(overrides)
    return CertExpiryCheckConfig(**defaults)


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_scanner(
    config: CertExpiryCheckConfig | None = None,
    queue: MagicMock | None = None,
) -> CertExpiryScannerAdapter:
    return CertExpiryScannerAdapter(
        config=config or _make_config(),
        queue=queue or _mock_queue(),
        interval=60,
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestCertExpiryConfig:
    def test_defaults(self) -> None:
        cfg = CertExpiryCheckConfig()
        assert cfg.enabled is False
        assert cfg.warning_days == 30
        assert cfg.critical_days == 7
        assert cfg.interval == 900

    def test_minimum_interval(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CertExpiryCheckConfig(interval=10)


# ---------------------------------------------------------------------------
# Name and endpoint parsing
# ---------------------------------------------------------------------------


class TestCertExpiryScanner:
    def test_name(self) -> None:
        scanner = _make_scanner()
        assert scanner.name == "scanner.cert_expiry"

    def test_parse_endpoint_with_port(self) -> None:
        host, port = CertExpiryScannerAdapter._parse_endpoint("example.com:8443")
        assert host == "example.com"
        assert port == 8443

    def test_parse_endpoint_without_port(self) -> None:
        host, port = CertExpiryScannerAdapter._parse_endpoint("example.com")
        assert host == "example.com"
        assert port == 443


# ---------------------------------------------------------------------------
# State-based dedup (evaluate logic)
# ---------------------------------------------------------------------------


class TestCertExpiryEvaluate:
    def test_first_scan_ok_no_event(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate("example.com", 90)
        assert events == []

    def test_first_scan_warning_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate("example.com", 20)
        assert len(events) == 1
        assert events[0].event_type == "certificate_expiry"
        assert events[0].severity == Severity.WARNING
        assert events[0].payload["state"] == "warning"

    def test_first_scan_critical_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate("example.com", 3)
        assert len(events) == 1
        assert events[0].event_type == "certificate_expiry"
        assert events[0].severity == Severity.ERROR
        assert events[0].payload["state"] == "critical"

    def test_same_state_dedup(self) -> None:
        scanner = _make_scanner()
        events1 = scanner._evaluate("example.com", 20)
        events2 = scanner._evaluate("example.com", 18)
        assert len(events1) == 1
        assert len(events2) == 0  # still warning, no new event

    def test_warning_to_critical_emits(self) -> None:
        scanner = _make_scanner()
        scanner._evaluate("example.com", 20)  # warning
        events = scanner._evaluate("example.com", 3)  # -> critical
        assert len(events) == 1
        assert events[0].severity == Severity.ERROR
        assert events[0].payload["state"] == "critical"

    def test_critical_to_ok_emits_renewal(self) -> None:
        scanner = _make_scanner()
        scanner._evaluate("example.com", 3)  # critical
        events = scanner._evaluate("example.com", 90)  # -> ok
        assert len(events) == 1
        assert events[0].event_type == "certificate_renewed"
        assert events[0].severity == Severity.INFO
        assert events[0].payload["previous_state"] == "critical"

    def test_warning_to_ok_emits_renewal(self) -> None:
        scanner = _make_scanner()
        scanner._evaluate("example.com", 20)  # warning
        events = scanner._evaluate("example.com", 90)  # -> ok
        assert len(events) == 1
        assert events[0].event_type == "certificate_renewed"
        assert events[0].payload["previous_state"] == "warning"

    def test_dedup_key_includes_endpoint(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate("example.com", 3)
        assert events[0].metadata.dedup_key == "scanner.cert_expiry:example.com"

    def test_multiple_endpoints_tracked_independently(self) -> None:
        cfg = _make_config(endpoints=["a.com", "b.com"])
        scanner = _make_scanner(config=cfg)
        events_a = scanner._evaluate("a.com", 20)
        events_b = scanner._evaluate("b.com", 90)
        assert len(events_a) == 1  # warning
        assert len(events_b) == 0  # ok, first scan


# ---------------------------------------------------------------------------
# Check error handling
# ---------------------------------------------------------------------------


class TestCertExpiryErrors:
    def test_check_error_emits_once(self) -> None:
        scanner = _make_scanner()
        events1 = scanner._emit_check_error("example.com", RuntimeError("timeout"))
        events2 = scanner._emit_check_error("example.com", RuntimeError("timeout"))
        assert len(events1) == 1
        assert len(events2) == 0  # deduped
        assert events1[0].event_type == "certificate_check_error"

    def test_check_error_after_ok_emits(self) -> None:
        scanner = _make_scanner()
        scanner._evaluate("example.com", 90)  # ok
        events = scanner._emit_check_error("example.com", RuntimeError("fail"))
        assert len(events) == 1
