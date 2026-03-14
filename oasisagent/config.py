"""Configuration loading and validation for OasisAgent.

Loads config.yaml, interpolates ${VAR} environment variable references,
and validates against Pydantic models matching ARCHITECTURE.md §11.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    max_consecutive_identical: Annotated[int, Field(ge=1)] = 3


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


_SeverityLiteral = Literal["info", "warning", "error", "critical"]


class WebhookEventMapping(BaseModel):
    """Maps a source event name to canonical event_type and severity."""

    model_config = ConfigDict(extra="forbid")

    source_event: str
    event_type: str
    severity: _SeverityLiteral = "warning"


class WebhookSourceConfig(BaseModel):
    """Per-source webhook receiver configuration.

    Stored as a connector in SQLite. The connector ``name`` becomes the
    URL slug: ``POST /ingest/webhook/{name}``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    auth_mode: Literal["none", "header_secret", "api_key"] = "none"
    auth_header: str = ""
    auth_secret: str = ""
    system: str = ""
    event_type_field: str = "eventType"
    entity_id_field: str = ""
    default_severity: _SeverityLiteral = "warning"
    event_mappings: list[WebhookEventMapping] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_auth_secret(self) -> WebhookSourceConfig:
        if self.auth_mode != "none" and not self.auth_secret:
            msg = f"auth_secret is required when auth_mode is '{self.auth_mode}'"
            raise ValueError(msg)
        return self


# -- Ingestion: HTTP Poller -------------------------------------------------


class ExtractMapping(BaseModel):
    """JMESPath extraction rules for ``mode=extract``."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    event_type: str
    severity_expr: str | None = None
    payload_expr: str | None = None


class ThresholdConfig(BaseModel):
    """Threshold crossing rules for ``mode=threshold``."""

    model_config = ConfigDict(extra="forbid")

    value_expr: str
    warning: float
    critical: float
    entity_id: str
    event_type: str = "threshold_exceeded"

    @model_validator(mode="after")
    def _check_threshold_order(self) -> ThresholdConfig:
        if self.critical <= self.warning:
            msg = f"critical ({self.critical}) must be greater than warning ({self.warning})"
            raise ValueError(msg)
        return self


class HttpPollerTargetConfig(BaseModel):
    """Per-target HTTP polling configuration.

    Stored as a connector in SQLite. Supports three response modes:
    ``health_check``, ``extract``, and ``threshold``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    url: str
    system: str
    mode: Literal["health_check", "extract", "threshold"] = "health_check"
    interval: Annotated[int, Field(ge=5)] = 60
    timeout: Annotated[int, Field(ge=1)] = 10
    auth_mode: Literal["none", "basic", "token"] = "none"
    auth_username: str = ""
    auth_password: str = ""
    auth_header: str = "Authorization"
    auth_value: str = ""
    extract: ExtractMapping | None = None
    threshold: ThresholdConfig | None = None

    @model_validator(mode="after")
    def _check_mode_config(self) -> HttpPollerTargetConfig:
        if self.mode == "extract" and self.extract is None:
            msg = "extract config is required when mode is 'extract'"
            raise ValueError(msg)
        if self.mode == "threshold" and self.threshold is None:
            msg = "threshold config is required when mode is 'threshold'"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_auth_credentials(self) -> HttpPollerTargetConfig:
        if self.auth_mode == "basic" and not (self.auth_username and self.auth_password):
            msg = "auth_username and auth_password required when auth_mode is 'basic'"
            raise ValueError(msg)
        if self.auth_mode == "token" and not self.auth_value:
            msg = "auth_value required when auth_mode is 'token'"
            raise ValueError(msg)
        return self


# -- UniFi ------------------------------------------------------------------


class UnifiAdapterConfig(BaseModel):
    """UniFi Network controller polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = "https://192.168.1.1"
    username: str = ""
    password: str = ""
    site: str = "default"
    verify_ssl: bool = False
    is_udm: bool = True
    poll_interval: int = 30
    poll_alarms: bool = True
    poll_health: bool = True
    poll_ips: bool = True
    poll_rogue_ap: bool = True
    poll_clients: bool = False
    poll_anomalies: bool = True
    poll_events: bool = True
    poll_dpi: bool = False
    client_spike_threshold: float = 20.0
    dpi_bandwidth_threshold_mbps: float = 100.0
    timeout: int = 10
    cpu_threshold: float = 90.0
    memory_threshold: float = 90.0


# -- Cloudflare -------------------------------------------------------------


class CloudflareAdapterConfig(BaseModel):
    """Cloudflare API polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_token: str = ""
    account_id: str = ""
    zone_id: str = ""
    poll_interval: int = 300
    poll_tunnels: bool = True
    poll_waf: bool = True
    poll_ssl: bool = True
    waf_lookback_minutes: int = 10
    waf_spike_threshold: int = 50
    timeout: int = 30


# -- Uptime Kuma ------------------------------------------------------------


class UptimeKumaAdapterConfig(BaseModel):
    """Uptime Kuma Prometheus metrics polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    api_key: str = ""
    poll_interval: int = 60
    timeout: int = 10
    response_time_threshold_ms: int = 5000
    cert_warning_days: int = 30
    cert_critical_days: int = 7


# -- Servarr (Sonarr/Radarr/Prowlarr/Bazarr) --------------------------------


class ServarrAdapterConfig(BaseModel):
    """Servarr app polling adapter configuration.

    A single config model for Sonarr, Radarr, Prowlarr, and Bazarr since
    they all share the same API pattern (differing only in API version).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    api_key: str = ""
    app_type: Literal["sonarr", "radarr", "prowlarr", "bazarr"] = "sonarr"
    poll_interval: int = 60
    timeout: int = 10


# -- qBittorrent ------------------------------------------------------------


class QBittorrentAdapterConfig(BaseModel):
    """qBittorrent Web API polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    username: str = "admin"
    password: str = ""
    poll_interval: int = 60
    timeout: int = 10


# -- Plex -------------------------------------------------------------------


class PlexAdapterConfig(BaseModel):
    """Plex Media Server polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    token: str = ""
    poll_interval: int = 60
    timeout: int = 10


# -- Tautulli ---------------------------------------------------------------


class TautulliAdapterConfig(BaseModel):
    """Tautulli API polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    api_key: str = ""
    poll_interval: int = 60
    timeout: int = 10
    bandwidth_threshold_kbps: int = 100_000


# -- Tdarr ------------------------------------------------------------------


class TdarrAdapterConfig(BaseModel):
    """Tdarr API polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    poll_interval: int = 60
    timeout: int = 10


# -- Overseerr ---------------------------------------------------------------


class OverseerrAdapterConfig(BaseModel):
    """Overseerr API polling adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    api_key: str = ""
    poll_interval: int = 60
    timeout: int = 10


# -- Vaultwarden -------------------------------------------------------------


class VaultwardenAdapterConfig(BaseModel):
    """Vaultwarden (Bitwarden) health-check adapter configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = ""
    poll_interval: int = 60
    timeout: int = 10


# -- Scanner ----------------------------------------------------------------


class CertExpiryCheckConfig(BaseModel):
    """TLS certificate expiry scanner configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    endpoints: list[str] = Field(default_factory=list)
    warning_days: int = 30
    critical_days: int = 7
    interval: Annotated[int, Field(ge=60)] = 900


class DiskSpaceCheckConfig(BaseModel):
    """Disk space usage scanner configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    paths: list[str] = Field(default_factory=list)
    warning_threshold_pct: int = 85
    critical_threshold_pct: int = 95
    interval: Annotated[int, Field(ge=60)] = 900


class HaHealthCheckConfig(BaseModel):
    """Home Assistant integration health scanner configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval: Annotated[int, Field(ge=60)] = 900


class DockerHealthCheckConfig(BaseModel):
    """Docker container health scanner configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ignore_containers: list[str] = Field(default_factory=list)
    interval: Annotated[int, Field(ge=60)] = 900


class BackupSourceConfig(BaseModel):
    """Configuration for a single backup source to monitor."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["pbs", "file"]
    # PBS fields
    url: str = ""
    token_id: str = ""
    token_secret: str = ""
    datastore: str = ""
    verify_ssl: bool = True
    # File fields
    path: str = ""  # glob pattern, e.g. "/backup/daily/*.tar.gz"
    # Common
    max_age_hours: int = 26  # just over 1 day


class BackupFreshnessCheckConfig(BaseModel):
    """Backup freshness scanner configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval: Annotated[int, Field(ge=60)] = 3600
    sources: list[BackupSourceConfig] = Field(default_factory=list)


class ScannerConfig(BaseModel):
    """Preventive scanning framework configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval: Annotated[int, Field(ge=60)] = 900
    adaptive_enabled: bool = True
    adaptive_fast_factor: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.25
    adaptive_recovery_scans: Annotated[int, Field(ge=1)] = 3
    certificate_expiry: CertExpiryCheckConfig = Field(default_factory=CertExpiryCheckConfig)
    disk_space: DiskSpaceCheckConfig = Field(default_factory=DiskSpaceCheckConfig)
    ha_health: HaHealthCheckConfig = Field(default_factory=HaHealthCheckConfig)
    docker_health: DockerHealthCheckConfig = Field(default_factory=DockerHealthCheckConfig)
    backup_freshness: BackupFreshnessCheckConfig = Field(
        default_factory=BackupFreshnessCheckConfig,
    )


# -- Ingestion (top-level) --------------------------------------------------


class IngestionConfig(BaseModel):
    """All ingestion adapter configurations."""

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttIngestionConfig = Field(default_factory=MqttIngestionConfig)
    ha_websocket: HaWebSocketConfig = Field(default_factory=HaWebSocketConfig)
    ha_log_poller: HaLogPollerConfig = Field(default_factory=HaLogPollerConfig)
    http_poller_targets: list[HttpPollerTargetConfig] = Field(default_factory=list)
    unifi: UnifiAdapterConfig = Field(default_factory=UnifiAdapterConfig)
    cloudflare: CloudflareAdapterConfig = Field(
        default_factory=CloudflareAdapterConfig,
    )
    uptime_kuma: UptimeKumaAdapterConfig = Field(
        default_factory=UptimeKumaAdapterConfig,
    )
    servarr: list[ServarrAdapterConfig] = Field(default_factory=list)
    qbittorrent: QBittorrentAdapterConfig = Field(
        default_factory=QBittorrentAdapterConfig,
    )
    plex: PlexAdapterConfig = Field(default_factory=PlexAdapterConfig)
    tautulli: TautulliAdapterConfig = Field(
        default_factory=TautulliAdapterConfig,
    )
    tdarr: TdarrAdapterConfig = Field(default_factory=TdarrAdapterConfig)
    overseerr: OverseerrAdapterConfig = Field(
        default_factory=OverseerrAdapterConfig,
    )
    vaultwarden: VaultwardenAdapterConfig = Field(
        default_factory=VaultwardenAdapterConfig,
    )


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
            timeout=30,
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


class PortainerHandlerConfig(BaseModel):
    """Portainer handler configuration.

    Proxies Docker operations through the Portainer REST API, which
    routes requests to a specific environment (endpoint) via
    ``/api/endpoints/{endpoint_id}/docker/...``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = "https://localhost:9443"
    api_key: str = ""
    endpoint_id: Annotated[int, Field(ge=1)] = 1
    verify_ssl: bool = False
    verify_timeout: Annotated[int, Field(ge=1)] = 30
    verify_poll_interval: Annotated[float, Field(gt=0.0)] = 2.0


class UnifiHandlerConfig(BaseModel):
    """UniFi Network handler configuration.

    Executes actions against the UniFi controller: restart devices,
    block/unblock clients, gather device context.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = "https://192.168.1.1"
    username: str = ""
    password: str = ""
    site: str = "default"
    verify_ssl: bool = False
    is_udm: bool = True
    timeout: int = 10
    verify_timeout: Annotated[int, Field(ge=1)] = 30
    verify_poll_interval: Annotated[float, Field(gt=0.0)] = 2.0


class CloudflareHandlerConfig(BaseModel):
    """Cloudflare handler configuration.

    Executes actions against the Cloudflare API: purge cache,
    block/unblock IPs via firewall rules, gather zone context.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_token: str = ""
    zone_id: str = ""
    account_id: str = ""
    timeout: int = 30


class HandlersConfig(BaseModel):
    """All handler configurations."""

    model_config = ConfigDict(extra="forbid")

    homeassistant: HaHandlerConfig = Field(default_factory=HaHandlerConfig)
    docker: DockerHandlerConfig = Field(default_factory=DockerHandlerConfig)
    portainer: PortainerHandlerConfig = Field(default_factory=PortainerHandlerConfig)
    proxmox: ProxmoxHandlerConfig = Field(default_factory=ProxmoxHandlerConfig)
    unifi: UnifiHandlerConfig = Field(default_factory=UnifiHandlerConfig)
    cloudflare: CloudflareHandlerConfig = Field(
        default_factory=CloudflareHandlerConfig,
    )


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


class TelegramNotificationConfig(BaseModel):
    """Telegram bot notification channel configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    parse_mode: str = "HTML"


class DiscordNotificationConfig(BaseModel):
    """Discord webhook notification channel configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    webhook_url: str = ""
    username: str = "OasisAgent"
    avatar_url: str = ""


class SlackNotificationConfig(BaseModel):
    """Slack Incoming Webhook notification channel configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    webhook_url: str = ""
    channel: str = ""
    username: str = "OasisAgent"
    icon_emoji: str = ":robot_face:"


class NotificationsConfig(BaseModel):
    """All notification channel configurations."""

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttNotificationConfig = Field(default_factory=MqttNotificationConfig)
    email: EmailNotificationConfig = Field(default_factory=EmailNotificationConfig)
    webhook: WebhookNotificationConfig = Field(default_factory=WebhookNotificationConfig)
    telegram: TelegramNotificationConfig = Field(default_factory=TelegramNotificationConfig)
    discord: DiscordNotificationConfig = Field(default_factory=DiscordNotificationConfig)
    slack: SlackNotificationConfig = Field(default_factory=SlackNotificationConfig)


# -- Top-level config -------------------------------------------------------


class OasisAgentConfig(BaseModel):
    """Top-level OasisAgent configuration.

    Represents the complete config.yaml schema as defined in
    ARCHITECTURE.md §11.
    """

    model_config = ConfigDict(extra="forbid")

    agent: AgentConfig = Field(default_factory=AgentConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    handlers: HandlersConfig = Field(default_factory=HandlersConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
