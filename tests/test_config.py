"""Tests for configuration loading and validation."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from oasisagent.config import (
    ConfigError,
    LogLevel,
    OasisAgentConfig,
    _interpolate_env_vars,
    load_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, content: str) -> Path:
    """Write a config YAML string to a temp file and return its path."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(dedent(content))
    return config_file


def _minimal_config() -> str:
    """Return a minimal valid config YAML string (all sections use defaults)."""
    return """\
        agent:
          name: test-agent
    """


def _full_config() -> str:
    """Return a full config YAML with all sections populated."""
    return """\
        agent:
          name: test-agent
          log_level: debug
          event_queue_size: 500
          shutdown_timeout: 10

        ingestion:
          mqtt:
            enabled: true
            broker: mqtt://broker:1883
            username: user
            password: pass
            client_id: test
            qos: 1
            topics:
              - pattern: "ha/error/#"
                system: homeassistant
                event_type: automation_error
                severity: error
              - pattern: "oasis/alerts/#"
                system: auto
                event_type: auto
                severity: auto
          ha_websocket:
            enabled: true
            url: ws://ha:8123/api/websocket
            token: test-token
            subscriptions:
              state_changes:
                enabled: true
                trigger_states: ["unavailable"]
                ignore_entities: ["sensor.flaky"]
                min_duration: 120
              automation_failures:
                enabled: false
              service_call_errors:
                enabled: true
          ha_log_poller:
            enabled: true
            url: http://ha:8123
            token: test-token
            poll_interval: 15
            dedup_window: 600
            patterns:
              - regex: "Error setting up integration '(.+)'"
                event_type: integration_failure
                severity: error

        llm:
          triage:
            base_url: http://ollama:11434/v1
            model: qwen2.5:7b
            api_key: not-needed
            timeout: 5
            max_tokens: 1024
            temperature: 0.1
          reasoning:
            base_url: https://api.anthropic.com
            model: claude-sonnet-4-5-20250929
            api_key: sk-test
            timeout: 45
            max_tokens: 4096
            temperature: 0.2
          options:
            cost_tracking: true
            retry_attempts: 3
            fallback_to_triage: false
            log_prompts: true

        handlers:
          homeassistant:
            enabled: true
            url: http://ha:8123
            token: test-token
            verify_timeout: 60
          docker:
            enabled: false
            socket: unix:///var/run/docker.sock
          proxmox:
            enabled: false
            url: https://pve:8006
            user: root@pam
            token_name: agent
            token_value: secret
            verify_ssl: false

        guardrails:
          blocked_domains:
            - "lock.*"
            - "alarm_control_panel.*"
          blocked_entities:
            - "switch.dangerous"
          kill_switch: false
          dry_run: true
          circuit_breaker:
            max_attempts_per_entity: 5
            window_minutes: 30
            cooldown_minutes: 10
            global_failure_rate_threshold: 0.5
            global_pause_minutes: 60

        audit:
          influxdb:
            enabled: true
            url: http://influx:8086
            token: influx-token
            org: myorg
            bucket: oasisagent
          retention_days: 30

        notifications:
          mqtt:
            enabled: true
            broker: mqtt://broker:1883
            topic_prefix: oasis/notify
            username: user
            password: pass
          email:
            enabled: false
          webhook:
            enabled: false
    """


# ---------------------------------------------------------------------------
# Environment variable interpolation
# ---------------------------------------------------------------------------


class TestEnvVarInterpolation:
    """Tests for ${VAR} and ${VAR:-default} interpolation."""

    def test_simple_substitution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _interpolate_env_vars("value: ${MY_VAR}") == "value: hello"

    def test_default_value_when_unset(self) -> None:
        result = _interpolate_env_vars("key: ${UNSET_VAR:-fallback}")
        assert result == "key: fallback"

    def test_default_value_empty_string(self) -> None:
        result = _interpolate_env_vars("key: ${UNSET_VAR:-}")
        assert result == "key: "

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "real")
        result = _interpolate_env_vars("key: ${MY_VAR:-fallback}")
        assert result == "key: real"

    def test_missing_required_var_raises(self) -> None:
        with pytest.raises(ConfigError, match="MISSING_VAR"):
            _interpolate_env_vars("key: ${MISSING_VAR}")

    def test_multiple_missing_vars_reported(self) -> None:
        with pytest.raises(ConfigError, match="ALPHA") as exc_info:
            _interpolate_env_vars("a: ${ALPHA}\nb: ${BETA}")
        assert "BETA" in str(exc_info.value)

    def test_embedded_in_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOST", "192.168.1.100")
        monkeypatch.setenv("PORT", "1883")
        result = _interpolate_env_vars("broker: mqtt://${HOST}:${PORT}")
        assert result == "broker: mqtt://192.168.1.100:1883"

    def test_no_interpolation_needed(self) -> None:
        raw = "key: plain_value"
        assert _interpolate_env_vars(raw) == raw

    def test_multiple_on_same_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        result = _interpolate_env_vars("${A} and ${B}")
        assert result == "1 and 2"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for the load_config() function."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="Config file not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_file_not_found_message_suggests_example(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"config\.example\.yaml"):
            load_config(tmp_path / "config.yaml")

    def test_minimal_config_loads(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _minimal_config())
        config = load_config(path)
        assert isinstance(config, OasisAgentConfig)
        assert config.agent.name == "test-agent"

    def test_full_config_loads(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _full_config())
        config = load_config(path)
        assert config.agent.name == "test-agent"
        assert config.agent.log_level == LogLevel.DEBUG
        assert config.agent.event_queue_size == 500

    def test_env_var_in_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_TOKEN", "my-secret-token")
        content = """\
            handlers:
              homeassistant:
                token: ${TEST_TOKEN}
        """
        path = _write_config(tmp_path, content)
        config = load_config(path)
        assert config.handlers.homeassistant.token == "my-secret-token"

    def test_missing_env_var_raises(self, tmp_path: Path) -> None:
        content = """\
            handlers:
              homeassistant:
                token: ${MISSING_TOKEN}
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError, match="MISSING_TOKEN"):
            load_config(path)

    def test_env_var_with_default(self, tmp_path: Path) -> None:
        content = """\
            llm:
              triage:
                base_url: http://localhost:11434/v1
                model: qwen2.5:7b
                api_key: ${LLM_KEY:-not-needed}
        """
        path = _write_config(tmp_path, content)
        config = load_config(path)
        assert config.llm.triage.api_key == "not-needed"

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "not: valid: yaml: [")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_non_mapping_yaml(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "- a list\n- not a mapping")
        with pytest.raises(ConfigError, match="YAML mapping"):
            load_config(path)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    """Verify defaults match ARCHITECTURE.md §11."""

    def test_agent_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "agent: {}")
        config = load_config(path)
        assert config.agent.name == "oasis-agent"
        assert config.agent.log_level == LogLevel.INFO
        assert config.agent.event_queue_size == 1000
        assert config.agent.shutdown_timeout == 30

    def test_guardrails_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "guardrails: {}")
        config = load_config(path)
        assert config.guardrails.kill_switch is False
        assert config.guardrails.dry_run is False
        assert "lock.*" in config.guardrails.blocked_domains
        assert "camera.*" in config.guardrails.blocked_domains

    def test_circuit_breaker_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "guardrails: {}")
        config = load_config(path)
        cb = config.guardrails.circuit_breaker
        assert cb.max_attempts_per_entity == 3
        assert cb.window_minutes == 60
        assert cb.cooldown_minutes == 15
        assert cb.global_failure_rate_threshold == 0.3
        assert cb.global_pause_minutes == 30

    def test_audit_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "audit: {}")
        config = load_config(path)
        assert config.audit.influxdb.enabled is True
        assert config.audit.influxdb.bucket == "oasisagent"
        assert config.audit.retention_days == 90

    def test_ha_websocket_subscription_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "ingestion:\n  ha_websocket: {}")
        config = load_config(path)
        subs = config.ingestion.ha_websocket.subscriptions
        assert subs.state_changes.enabled is True
        assert subs.state_changes.trigger_states == ["unavailable", "unknown"]
        assert subs.state_changes.min_duration == 60
        assert subs.automation_failures.enabled is True
        assert subs.service_call_errors.enabled is True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for Pydantic validation catching invalid config."""

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        """extra='forbid' catches typos in config keys."""
        content = """\
            agent:
              name: test
              typo_field: true
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError, match="typo_field"):
            load_config(path)

    def test_nested_extra_field_rejected(self, tmp_path: Path) -> None:
        content = """\
            guardrails:
              circuit_breaker:
                max_attempts_per_entity: 3
                bogus_setting: 42
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError, match="bogus_setting"):
            load_config(path)

    def test_invalid_log_level(self, tmp_path: Path) -> None:
        content = """\
            agent:
              log_level: verbose
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError):
            load_config(path)

    def test_negative_queue_size(self, tmp_path: Path) -> None:
        content = """\
            agent:
              event_queue_size: -1
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError):
            load_config(path)

    def test_qos_out_of_range(self, tmp_path: Path) -> None:
        content = """\
            ingestion:
              mqtt:
                qos: 5
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError):
            load_config(path)

    def test_temperature_out_of_range(self, tmp_path: Path) -> None:
        content = """\
            llm:
              triage:
                base_url: http://localhost:11434/v1
                model: test
                temperature: 3.0
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError):
            load_config(path)

    def test_failure_rate_threshold_out_of_range(self, tmp_path: Path) -> None:
        content = """\
            guardrails:
              circuit_breaker:
                global_failure_rate_threshold: 1.5
        """
        path = _write_config(tmp_path, content)
        with pytest.raises(ConfigError):
            load_config(path)


# ---------------------------------------------------------------------------
# Nested model structure
# ---------------------------------------------------------------------------


class TestNestedModels:
    """Verify nested models parse correctly from YAML."""

    def test_mqtt_topic_mappings(self, tmp_path: Path) -> None:
        content = """\
            ingestion:
              mqtt:
                topics:
                  - pattern: "ha/error/#"
                    system: homeassistant
                    event_type: automation_error
                    severity: error
                  - pattern: "oasis/#"
        """
        path = _write_config(tmp_path, content)
        config = load_config(path)
        topics = config.ingestion.mqtt.topics
        assert len(topics) == 2
        assert topics[0].pattern == "ha/error/#"
        assert topics[0].system == "homeassistant"
        assert topics[1].system == "auto"
        assert topics[1].severity == "auto"

    def test_log_patterns(self, tmp_path: Path) -> None:
        content = """\
            ingestion:
              ha_log_poller:
                patterns:
                  - regex: "Error setting up integration '(.+)'"
                    event_type: integration_failure
                    severity: error
                  - regex: "(.+) is unavailable"
                    event_type: state_unavailable
        """
        path = _write_config(tmp_path, content)
        config = load_config(path)
        patterns = config.ingestion.ha_log_poller.patterns
        assert len(patterns) == 2
        assert patterns[0].event_type == "integration_failure"
        assert patterns[0].severity == "error"
        assert patterns[1].severity == "warning"  # default

    def test_ha_websocket_subscriptions(self, tmp_path: Path) -> None:
        content = """\
            ingestion:
              ha_websocket:
                subscriptions:
                  state_changes:
                    enabled: false
                    trigger_states: ["unavailable"]
                    ignore_entities: ["sensor.flaky"]
                    min_duration: 300
                  automation_failures:
                    enabled: false
        """
        path = _write_config(tmp_path, content)
        config = load_config(path)
        subs = config.ingestion.ha_websocket.subscriptions
        assert subs.state_changes.enabled is False
        assert subs.state_changes.trigger_states == ["unavailable"]
        assert subs.state_changes.ignore_entities == ["sensor.flaky"]
        assert subs.state_changes.min_duration == 300
        assert subs.automation_failures.enabled is False
        assert subs.service_call_errors.enabled is True  # default

    def test_email_from_alias(self, tmp_path: Path) -> None:
        """The 'from' YAML key maps to from_address via alias."""
        content = """\
            notifications:
              email:
                enabled: true
                from: agent@test.com
                to:
                  - admin@test.com
        """
        path = _write_config(tmp_path, content)
        config = load_config(path)
        assert config.notifications.email.from_address == "agent@test.com"
        assert config.notifications.email.to == ["admin@test.com"]
