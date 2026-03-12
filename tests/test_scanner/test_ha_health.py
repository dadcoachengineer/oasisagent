"""Tests for the HA integration health scanner."""

from __future__ import annotations

from oasisagent.config import HaHandlerConfig, HaHealthCheckConfig
from oasisagent.models import Severity
from oasisagent.scanner.ha_health import HaHealthScannerAdapter


def _make_config(**overrides: object) -> HaHealthCheckConfig:
    defaults: dict = {"enabled": True, "interval": 60}
    defaults.update(overrides)
    return HaHealthCheckConfig(**defaults)


def _make_ha_config(**overrides: object) -> HaHandlerConfig:
    defaults: dict = {
        "enabled": True,
        "url": "http://ha.local:8123",
        "token": "test-token",
    }
    defaults.update(overrides)
    return HaHandlerConfig(**defaults)


def _mock_queue():  # noqa: ANN202
    from unittest.mock import MagicMock

    return MagicMock()


def _make_scanner(
    config: HaHealthCheckConfig | None = None,
    ha_config: HaHandlerConfig | None = None,
) -> HaHealthScannerAdapter:
    return HaHealthScannerAdapter(
        config=config or _make_config(),
        queue=_mock_queue(),
        interval=60,
        ha_config=ha_config or _make_ha_config(),
    )


def _make_entry(
    domain: str = "zwave_js",
    state: str = "loaded",
    title: str = "Z-Wave JS",
    entry_id: str = "abc123",
) -> dict:
    return {
        "domain": domain,
        "state": state,
        "title": title,
        "entry_id": entry_id,
    }


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestHaHealthConfig:
    def test_defaults(self) -> None:
        cfg = HaHealthCheckConfig()
        assert cfg.enabled is False
        assert cfg.interval == 900


# ---------------------------------------------------------------------------
# Name and basic properties
# ---------------------------------------------------------------------------


class TestHaHealthScanner:
    def test_name(self) -> None:
        scanner = _make_scanner()
        assert scanner.name == "scanner.ha_health"


# ---------------------------------------------------------------------------
# State-based dedup (evaluate_entries)
# ---------------------------------------------------------------------------


class TestHaHealthEvaluate:
    def test_healthy_entry_first_poll_no_event(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_entries([_make_entry(state="loaded")])
        assert events == []

    def test_setup_error_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_entries([
            _make_entry(domain="zwave_js", state="setup_error"),
        ])
        assert len(events) == 1
        assert events[0].event_type == "integration_unhealthy"
        assert events[0].severity == Severity.ERROR
        assert events[0].entity_id == "zwave_js"
        assert events[0].payload["state"] == "setup_error"

    def test_config_entry_not_ready_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_entries([
            _make_entry(state="config_entry_not_ready"),
        ])
        assert len(events) == 1
        assert events[0].payload["state"] == "config_entry_not_ready"

    def test_not_loaded_emits(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_entries([
            _make_entry(state="not_loaded"),
        ])
        assert len(events) == 1

    def test_same_error_state_dedup(self) -> None:
        scanner = _make_scanner()
        entry = _make_entry(state="setup_error")
        events1 = scanner._evaluate_entries([entry])
        events2 = scanner._evaluate_entries([entry])
        assert len(events1) == 1
        assert len(events2) == 0  # deduped

    def test_recovery_emits(self) -> None:
        scanner = _make_scanner()
        scanner._evaluate_entries([
            _make_entry(state="setup_error"),
        ])
        events = scanner._evaluate_entries([
            _make_entry(state="loaded"),
        ])
        assert len(events) == 1
        assert events[0].event_type == "integration_recovered"
        assert events[0].severity == Severity.INFO
        assert events[0].payload["previous_state"] == "setup_error"

    def test_transition_between_error_states(self) -> None:
        scanner = _make_scanner()
        scanner._evaluate_entries([
            _make_entry(state="setup_error"),
        ])
        events = scanner._evaluate_entries([
            _make_entry(state="config_entry_not_ready"),
        ])
        assert len(events) == 1
        assert events[0].payload["state"] == "config_entry_not_ready"

    def test_dedup_key_includes_domain(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_entries([
            _make_entry(domain="hue", state="setup_error"),
        ])
        assert events[0].metadata.dedup_key == "scanner.ha_health:hue"

    def test_multiple_domains_tracked_independently(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_entries([
            _make_entry(domain="zwave_js", state="setup_error"),
            _make_entry(domain="mqtt", state="loaded"),
        ])
        assert len(events) == 1
        assert events[0].entity_id == "zwave_js"

    def test_setup_in_progress_not_alerted(self) -> None:
        """setup_in_progress is in _ERROR_STATES but not _ALERT_STATES."""
        scanner = _make_scanner()
        events = scanner._evaluate_entries([
            _make_entry(state="setup_in_progress"),
        ])
        assert events == []

    def test_source_and_system(self) -> None:
        scanner = _make_scanner()
        events = scanner._evaluate_entries([
            _make_entry(state="setup_error"),
        ])
        assert events[0].source == "scanner.ha_health"
        assert events[0].system == "homeassistant"
