"""Configuration loading and validation for OasisAgent.

Loads config.yaml, interpolates ${VAR} environment variable references,
and validates against Pydantic models matching ARCHITECTURE.md §11.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Environment variable interpolation
# ---------------------------------------------------------------------------

_ENV_VAR_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""


def _interpolate_env_vars(raw: str) -> str:
    """Replace ${VAR} and ${VAR:-default} in a raw YAML string.

    Operates on the raw string *before* yaml.safe_load() so that env var
    references work in any value position, including inside URLs like
    ``mqtt://${MQTT_HOST}:1883``.

    Raises:
        ConfigError: If a referenced env var is not set and has no default.
    """
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        value = os.environ.get(name)
        if value is not None:
            return value
        if default is not None:
            return default
        missing.append(name)
        return match.group(0)

    result = _ENV_VAR_PATTERN.sub(_replace, raw)

    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(
            f"Missing required environment variables: {names}. "
            f"Set them in your environment or .env file."
        )

    return result


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_config(path: Path) -> OasisAgentConfig:
    """Load and validate configuration from a YAML file.

    Args:
        path: Path to the config.yaml file.

    Returns:
        Validated OasisAgentConfig instance.

    Raises:
        ConfigError: If the file is not found, env vars are missing,
            or validation fails.
    """
    if not path.exists():
        raise ConfigError(
            f"Config file not found at {path}. "
            f"Copy config.example.yaml to config.yaml and customize it."
        )

    raw = path.read_text()
    interpolated = _interpolate_env_vars(raw)

    try:
        data = yaml.safe_load(interpolated)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Config file at {path} must contain a YAML mapping, got {type(data)}.")

    try:
        return OasisAgentConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"Config validation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Pydantic models — organized bottom-up (leaf models first)
# ---------------------------------------------------------------------------


class LogLevel(StrEnum):
    """Valid log levels."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# -- Agent ------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Global agent settings."""

    model_config = ConfigDict(extra="forbid")

    name: str = "oasis-agent"
    log_level: LogLevel = LogLevel.INFO
    event_queue_size: Annotated[int, Field(ge=1)] = 1000
    dedup_window_seconds: Annotated[int, Field(ge=0)] = 300
    shutdown_timeout: Annotated[int, Field(ge=1)] = 30
    event_ttl: Annotated[int, Field(ge=0)] = 300
    known_fixes_dir: str = "known_fixes/"
    correlation_window: Annotated[int, Field(ge=0)] = 30
    metrics_port: Annotated[int, Field(ge=0, le=65535)] = 0


# -- Ingestion: MQTT --------------------------------------------------------


class MqttTopicMapping(BaseModel):
    """Maps an MQTT topic pattern to event fields."""

    model_config = ConfigDict(extra="forbid")

    pattern: str
    system: str = "auto"
    event_type: str = "auto"
    severity: str = "auto"


class MqttIngestionConfig(BaseModel):
    """MQTT subscriber configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    broker: str = "mqtt://localhost:1883"
    username: str = ""
    password: str = ""
    client_id: str = "oasis-agent"
    qos: Annotated[int, Field(ge=0, le=2)] = 1
    topics: list[MqttTopicMapping] = Field(default_factory=list)


# -- Ingestion: HA WebSocket ------------------------------------------------


class StateChangesSubscription(BaseModel):
    """Config for HA state_changed event filtering."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    trigger_states: list[str] = Field(default_factory=lambda: ["unavailable", "unknown"])
    ignore_entities: list[str] = Field(default_factory=list)
    min_duration: Annotated[int, Field(ge=0)] = 60


class AutomationFailuresSubscription(BaseModel):
    """Config for HA automation failure tracking."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class ServiceCallErrorsSubscription(BaseModel):
    """Config for HA service call error tracking."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class HaWebSocketSubscriptions(BaseModel):
    """HA WebSocket subscription configuration."""

    model_config = ConfigDict(extra="forbid")

    state_changes: StateChangesSubscription = Field(default_factory=StateChangesSubscription)
    automation_failures: AutomationFailuresSubscription = Field(
        default_factory=AutomationFailuresSubscription
    )
    service_call_errors: ServiceCallErrorsSubscription = Field(
        default_factory=ServiceCallErrorsSubscription
    )


class HaWebSocketConfig(BaseModel):
    """Home Assistant WebSocket ingestion configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    url: str = "ws://localhost:8123/api/websocket"
    token: str = ""
    subscriptions: HaWebSocketSubscriptions = Field(default_factory=HaWebSocketSubscriptions)


# -- Ingestion: HA Log Poller -----------------------------------------------


class LogPattern(BaseModel):
    """Pattern for matching HA log entries."""

    model_config = ConfigDict(extra="forbid")

    regex: str
    event_type: str
    severity: str = "warning"


class HaLogPollerConfig(BaseModel):
    """Home Assistant log poller configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    url: str = "http://localhost:8123"
    token: str = ""
    poll_interval: Annotated[int, Field(ge=1)] = 30
    patterns: list[LogPattern] = Field(default_factory=list)
    dedup_window: Annotated[int, Field(ge=0)] = 300


# -- Ingestion: Webhook Receiver --------------------------------------------


class WebhookEventMapping(BaseModel):
    """Maps a source event name to canonical event_type and severity."""

    model_config = ConfigDict(extra="forbid")

    source_event: str
    event_type: str
    severity: str = "warning"


class WebhookSourceConfig(BaseModel):
    """Per-source webhook receiver configuration.

    Stored as a connector in SQLite. The connector ``name`` becomes the
    URL slug: ``POST /ingest/webhook/{name}``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    auth_mode: str = "none"  # "none" | "header_secret" | "api_key"
    auth_header: str = ""
    auth_secret: str = ""
    system: str = ""
    event_type_field: str = "eventType"
    entity_id_field: str = ""
    default_severity: str = "warning"
    event_mappings: list[WebhookEventMapping] = Field(default_factory=list)


# -- Ingestion (top-level) --------------------------------------------------


class IngestionConfig(BaseModel):
    """All ingestion adapter configurations."""

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttIngestionConfig = Field(default_factory=MqttIngestionConfig)
    ha_websocket: HaWebSocketConfig = Field(default_factory=HaWebSocketConfig)
    ha_log_poller: HaLogPollerConfig = Field(default_factory=HaLogPollerConfig)


# -- LLM -------------------------------------------------------------------


class LlmEndpointConfig(BaseModel):
    """Configuration for a single LLM endpoint (triage or reasoning)."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    api_key: str = ""
    timeout: Annotated[int, Field(ge=1)] = 30
    max_tokens: Annotated[int, Field(ge=1)] = 2048
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] = 0.1


class LlmOptionsConfig(BaseModel):
    """Shared LLM client options."""

    model_config = ConfigDict(extra="forbid")

    cost_tracking: bool = True
    retry_attempts: Annotated[int, Field(ge=0)] = 2
    fallback_to_triage: bool = True
    log_prompts: bool = False


class LlmConfig(BaseModel):
    """LLM configuration — triage (T1) and reasoning (T2) endpoints."""

    model_config = ConfigDict(extra="forbid")

    triage: LlmEndpointConfig = Field(
        default_factory=lambda: LlmEndpointConfig(
            base_url="http://localhost:11434/v1",
            model="qwen2.5:7b",
            api_key="not-needed",
            timeout=5,
            max_tokens=1024,
            temperature=0.1,
        )
    )
    reasoning: LlmEndpointConfig = Field(
        default_factory=lambda: LlmEndpointConfig(
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-5-20250929",
            api_key="",
            timeout=45,
            max_tokens=4096,
            temperature=0.2,
        )
    )
    options: LlmOptionsConfig = Field(default_factory=LlmOptionsConfig)


# -- Handlers ---------------------------------------------------------------


class HaHandlerConfig(BaseModel):
    """Home Assistant handler configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    url: str = "http://localhost:8123"
    token: str = ""
    verify_timeout: Annotated[int, Field(ge=1)] = 30
    verify_poll_interval: Annotated[float, Field(gt=0.0)] = 2.0


class DockerHandlerConfig(BaseModel):
    """Docker handler configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    socket: str = "unix:///var/run/docker.sock"
    url: str | None = None
    tls_verify: bool = True
    verify_timeout: Annotated[int, Field(ge=1)] = 30
    verify_poll_interval: Annotated[float, Field(gt=0.0)] = 2.0


class ProxmoxHandlerConfig(BaseModel):
    """Proxmox handler configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = "https://localhost:8006"
    user: str = ""
    token_name: str = ""
    token_value: str = ""
    verify_ssl: bool = False


class HandlersConfig(BaseModel):
    """All handler configurations."""

    model_config = ConfigDict(extra="forbid")

    homeassistant: HaHandlerConfig = Field(default_factory=HaHandlerConfig)
    docker: DockerHandlerConfig = Field(default_factory=DockerHandlerConfig)
    proxmox: ProxmoxHandlerConfig = Field(default_factory=ProxmoxHandlerConfig)


# -- Guardrails -------------------------------------------------------------


class CircuitBreakerConfig(BaseModel):
    """Circuit breaker settings to prevent remediation loops."""

    model_config = ConfigDict(extra="forbid")

    max_attempts_per_entity: Annotated[int, Field(ge=1)] = 3
    window_minutes: Annotated[int, Field(ge=1)] = 60
    cooldown_minutes: Annotated[int, Field(ge=0)] = 15
    global_failure_rate_threshold: Annotated[float, Field(gt=0.0, le=1.0)] = 0.3
    global_pause_minutes: Annotated[int, Field(ge=1)] = 30


class GuardrailsConfig(BaseModel):
    """Safety guardrails — enforced in code, not model prompts."""

    model_config = ConfigDict(extra="forbid")

    blocked_domains: list[str] = Field(
        default_factory=lambda: [
            "lock.*",
            "alarm_control_panel.*",
            "camera.*",
            "cover.*",
        ]
    )
    blocked_entities: list[str] = Field(default_factory=list)
    kill_switch: bool = False
    dry_run: bool = False
    approval_timeout_minutes: Annotated[int, Field(ge=1)] = 30
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


# -- Audit ------------------------------------------------------------------


class InfluxDbConfig(BaseModel):
    """InfluxDB v2 connection configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "myorg"
    bucket: str = "oasisagent"


class AuditConfig(BaseModel):
    """Audit logging configuration."""

    model_config = ConfigDict(extra="forbid")

    influxdb: InfluxDbConfig = Field(default_factory=InfluxDbConfig)
    retention_days: Annotated[int, Field(ge=1)] = 90


# -- Notifications ----------------------------------------------------------


class MqttNotificationConfig(BaseModel):
    """MQTT notification channel configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    broker: str = "mqtt://localhost:1883"
    topic_prefix: str = "oasis/notifications"
    username: str = ""
    password: str = ""
    qos: Annotated[int, Field(ge=0, le=2)] = 1
    retain: bool = False


class EmailNotificationConfig(BaseModel):
    """Email (SMTP) notification channel configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 587
    username: str = ""
    password: str = ""
    starttls: bool = True
    from_address: str = Field(default="oasis-agent@example.com", alias="from")
    to: list[str] = Field(default_factory=list)


class WebhookNotificationConfig(BaseModel):
    """Webhook notification channel configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    urls: list[str] = Field(default_factory=list)


class NotificationsConfig(BaseModel):
    """All notification channel configurations."""

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttNotificationConfig = Field(default_factory=MqttNotificationConfig)
    email: EmailNotificationConfig = Field(default_factory=EmailNotificationConfig)
    webhook: WebhookNotificationConfig = Field(default_factory=WebhookNotificationConfig)


# -- Top-level config -------------------------------------------------------


class OasisAgentConfig(BaseModel):
    """Top-level OasisAgent configuration.

    Represents the complete config.yaml schema as defined in
    ARCHITECTURE.md §11.
    """

    model_config = ConfigDict(extra="forbid")

    agent: AgentConfig = Field(default_factory=AgentConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    handlers: HandlersConfig = Field(default_factory=HandlersConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
