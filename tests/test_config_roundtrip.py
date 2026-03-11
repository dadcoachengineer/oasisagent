"""Config round-trip tests — load config.example.yaml and verify schema sync.

Catches drift between config.py Pydantic models and config.example.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from oasisagent.config import ConfigError, OasisAgentConfig, load_config

_PROJECT_ROOT = Path(__file__).parent.parent
_EXAMPLE_CONFIG = _PROJECT_ROOT / "config.example.yaml"

# Environment variables referenced in config.example.yaml
_REQUIRED_ENV_VARS = {
    "MQTT_USER": "test_user",
    "MQTT_PASS": "test_pass",
    "HA_TOKEN": "test_ha_token",
    "TRIAGE_LLM_API_KEY": "test_triage_key",
    "REASONING_LLM_API_KEY": "test_reasoning_key",
    "PORTAINER_API_KEY": "test_portainer_key",
    "PROXMOX_USER": "root@pam",
    "PROXMOX_TOKEN_NAME": "oasis",
    "PROXMOX_TOKEN_VALUE": "test_proxmox_token",
    "INFLUXDB_TOKEN": "test_influx_token",
    # ${VAR} appears in a comment in config.example.yaml — the interpolator
    # operates on raw text before YAML parsing, so it matches comments too.
    "VAR": "placeholder",
}


@pytest.fixture()
def _env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all required env vars for config.example.yaml."""
    for name, value in _REQUIRED_ENV_VARS.items():
        monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# Round-trip: example config loads successfully
# ---------------------------------------------------------------------------


class TestExampleConfigLoads:
    @pytest.mark.usefixtures("_env_vars")
    def test_load_parses_without_error(self) -> None:
        config = load_config(_EXAMPLE_CONFIG)
        assert isinstance(config, OasisAgentConfig)

    @pytest.mark.usefixtures("_env_vars")
    def test_agent_section_populated(self) -> None:
        config = load_config(_EXAMPLE_CONFIG)
        assert config.agent.name == "oasis-agent"
        assert config.agent.event_queue_size == 1000

    @pytest.mark.usefixtures("_env_vars")
    def test_ingestion_mqtt_topics_present(self) -> None:
        config = load_config(_EXAMPLE_CONFIG)
        assert len(config.ingestion.mqtt.topics) >= 1

    @pytest.mark.usefixtures("_env_vars")
    def test_llm_endpoints_configured(self) -> None:
        config = load_config(_EXAMPLE_CONFIG)
        assert config.llm.triage.model == "qwen2.5:7b"
        assert "anthropic" in config.llm.reasoning.base_url

    @pytest.mark.usefixtures("_env_vars")
    def test_guardrails_blocked_domains(self) -> None:
        config = load_config(_EXAMPLE_CONFIG)
        assert "lock.*" in config.guardrails.blocked_domains
        assert config.guardrails.kill_switch is False

    @pytest.mark.usefixtures("_env_vars")
    def test_env_vars_interpolated(self) -> None:
        config = load_config(_EXAMPLE_CONFIG)
        assert config.ingestion.mqtt.username == "test_user"
        assert config.handlers.homeassistant.token == "test_ha_token"
        assert config.audit.influxdb.token == "test_influx_token"


# ---------------------------------------------------------------------------
# Schema sync: every OasisAgentConfig section key exists in example YAML
# ---------------------------------------------------------------------------


class TestSchemaSync:
    def test_all_top_level_sections_present_in_yaml(self) -> None:
        """Every top-level field in OasisAgentConfig must appear in config.example.yaml."""
        raw = _EXAMPLE_CONFIG.read_text()
        data = yaml.safe_load(raw)
        yaml_keys = set(data.keys())

        schema_keys = set(OasisAgentConfig.model_fields.keys())

        missing = schema_keys - yaml_keys
        assert missing == set(), (
            f"Config sections in schema but missing from config.example.yaml: {missing}"
        )

    def test_no_extra_sections_in_yaml(self) -> None:
        """config.example.yaml should not have sections unknown to the schema."""
        raw = _EXAMPLE_CONFIG.read_text()
        data = yaml.safe_load(raw)
        yaml_keys = set(data.keys())

        schema_keys = set(OasisAgentConfig.model_fields.keys())

        extra = yaml_keys - schema_keys
        assert extra == set(), (
            f"Sections in config.example.yaml not in schema: {extra}"
        )


# ---------------------------------------------------------------------------
# Missing env vars produce clear errors
# ---------------------------------------------------------------------------


class TestMissingEnvVars:
    def test_missing_required_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Set some but not all required vars
        monkeypatch.setenv("MQTT_USER", "user")
        # MQTT_PASS, HA_TOKEN, etc. are missing

        with pytest.raises(ConfigError, match="Missing required environment variables"):
            load_config(_EXAMPLE_CONFIG)
