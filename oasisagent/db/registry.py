"""Type registry mapping storage types to Pydantic models and secret fields.

Each connector, core service, and notification channel type has:
- A Pydantic model class (from ``oasisagent.config``)
- A set of field names that contain secrets (encrypted in ``secrets_json``)

The registry is the single source of truth for what gets encrypted. On write,
only fields in ``secret_fields`` are extracted to ``secrets_json``. On read,
``secrets_json`` keys win on conflict with ``config_json`` keys (defense in
depth against accidental plaintext leakage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel  # noqa: TC002 — used at runtime in TypeMeta

from oasisagent.config import (
    CircuitBreakerConfig,
    CloudflareAdapterConfig,
    CloudflareHandlerConfig,
    DiscordNotificationConfig,
    DockerHandlerConfig,
    EmailNotificationConfig,
    FrigateAdapterConfig,
    GuardrailsConfig,
    HaHandlerConfig,
    HaLogPollerConfig,
    HaWebSocketConfig,
    HttpPollerTargetConfig,
    InfluxDbConfig,
    LlmEndpointConfig,
    LlmOptionsConfig,
    MqttIngestionConfig,
    MqttNotificationConfig,
    N8nAdapterConfig,
    NpmAdapterConfig,
    OverseerrAdapterConfig,
    PlexAdapterConfig,
    PortainerAdapterConfig,
    PortainerHandlerConfig,
    ProxmoxAdapterConfig,
    ProxmoxHandlerConfig,
    QBittorrentAdapterConfig,
    ScannerConfig,
    ServarrAdapterConfig,
    SlackNotificationConfig,
    TautulliAdapterConfig,
    TdarrAdapterConfig,
    TelegramNotificationConfig,
    UnifiAdapterConfig,
    UnifiHandlerConfig,
    UptimeKumaAdapterConfig,
    VaultwardenAdapterConfig,
    WebhookNotificationConfig,
    WebhookSourceConfig,
)


@dataclass(frozen=True)
class TypeMeta:
    """Metadata for a stored config type.

    ``module_path`` and ``class_name`` locate the runtime class for
    types that can be instantiated directly from a DB row (adapters,
    handlers, notification channels).  Infrastructure singletons
    (LLM endpoints, guardrails, etc.) leave these empty.
    """

    model: type[BaseModel]
    secret_fields: frozenset[str] = field(default_factory=frozenset)
    module_path: str = ""
    class_name: str = ""


# ---------------------------------------------------------------------------
# Connectors (ingestion adapters)
# ---------------------------------------------------------------------------

CONNECTOR_TYPES: dict[str, TypeMeta] = {
    "mqtt": TypeMeta(
        model=MqttIngestionConfig,
        secret_fields=frozenset({"password"}),
        module_path="oasisagent.ingestion.mqtt",
        class_name="MqttAdapter",
    ),
    "ha_websocket": TypeMeta(
        model=HaWebSocketConfig,
        secret_fields=frozenset({"token"}),
        module_path="oasisagent.ingestion.ha_websocket",
        class_name="HaWebSocketAdapter",
    ),
    "ha_log_poller": TypeMeta(
        model=HaLogPollerConfig,
        secret_fields=frozenset({"token"}),
        module_path="oasisagent.ingestion.ha_log_poller",
        class_name="HaLogPollerAdapter",
    ),
    "webhook_receiver": TypeMeta(
        model=WebhookSourceConfig,
        secret_fields=frozenset({"auth_secret"}),
        # No adapter class — handled by the FastAPI webhook route.
    ),
    "http_poller": TypeMeta(
        model=HttpPollerTargetConfig,
        secret_fields=frozenset({"auth_password", "auth_value"}),
        module_path="oasisagent.ingestion.http_poller",
        class_name="HttpPollerAdapter",
    ),
    "unifi": TypeMeta(
        model=UnifiAdapterConfig,
        secret_fields=frozenset({"password"}),
        module_path="oasisagent.ingestion.unifi",
        class_name="UnifiAdapter",
    ),
    "cloudflare": TypeMeta(
        model=CloudflareAdapterConfig,
        secret_fields=frozenset({"api_token"}),
        module_path="oasisagent.ingestion.cloudflare",
        class_name="CloudflareAdapter",
    ),
    "uptime_kuma": TypeMeta(
        model=UptimeKumaAdapterConfig,
        secret_fields=frozenset({"api_key"}),
        module_path="oasisagent.ingestion.uptime_kuma",
        class_name="UptimeKumaAdapter",
    ),
    "frigate": TypeMeta(
        model=FrigateAdapterConfig,
        module_path="oasisagent.ingestion.frigate",
        class_name="FrigateAdapter",
    ),
    "npm": TypeMeta(
        model=NpmAdapterConfig,
        secret_fields=frozenset({"password"}),
        module_path="oasisagent.ingestion.npm",
        class_name="NpmAdapter",
    ),
    "servarr": TypeMeta(
        model=ServarrAdapterConfig,
        secret_fields=frozenset({"api_key"}),
        module_path="oasisagent.ingestion.servarr",
        class_name="ServarrAdapter",
    ),
    "qbittorrent": TypeMeta(
        model=QBittorrentAdapterConfig,
        secret_fields=frozenset({"password"}),
        module_path="oasisagent.ingestion.qbittorrent",
        class_name="QBittorrentAdapter",
    ),
    "plex": TypeMeta(
        model=PlexAdapterConfig,
        secret_fields=frozenset({"token"}),
        module_path="oasisagent.ingestion.plex",
        class_name="PlexAdapter",
    ),
    "tautulli": TypeMeta(
        model=TautulliAdapterConfig,
        secret_fields=frozenset({"api_key"}),
        module_path="oasisagent.ingestion.tautulli",
        class_name="TautulliAdapter",
    ),
    "tdarr": TypeMeta(
        model=TdarrAdapterConfig,
        module_path="oasisagent.ingestion.tdarr",
        class_name="TdarrAdapter",
    ),
    "n8n": TypeMeta(
        model=N8nAdapterConfig,
        secret_fields=frozenset({"api_key"}),
        module_path="oasisagent.ingestion.n8n",
        class_name="N8nAdapter",
    ),
    "overseerr": TypeMeta(
        model=OverseerrAdapterConfig,
        secret_fields=frozenset({"api_key"}),
        module_path="oasisagent.ingestion.overseerr",
        class_name="OverseerrAdapter",
    ),
    "vaultwarden": TypeMeta(
        model=VaultwardenAdapterConfig,
        secret_fields=frozenset({"admin_token"}),
        module_path="oasisagent.ingestion.vaultwarden",
        class_name="VaultwardenAdapter",
    ),
    "proxmox": TypeMeta(
        model=ProxmoxAdapterConfig,
        secret_fields=frozenset({"token_value"}),
        module_path="oasisagent.ingestion.proxmox",
        class_name="ProxmoxAdapter",
    ),
    "portainer": TypeMeta(
        model=PortainerAdapterConfig,
        secret_fields=frozenset({"api_key"}),
        module_path="oasisagent.ingestion.portainer",
        class_name="PortainerAdapter",
    ),
}


# ---------------------------------------------------------------------------
# Core services (handlers, LLM endpoints, audit, guardrails)
# ---------------------------------------------------------------------------

CORE_SERVICE_TYPES: dict[str, TypeMeta] = {
    # --- Infrastructure singletons (no module_path — built by _build_infrastructure) ---
    "llm_triage": TypeMeta(
        model=LlmEndpointConfig,
        secret_fields=frozenset({"api_key"}),
    ),
    "llm_reasoning": TypeMeta(
        model=LlmEndpointConfig,
        secret_fields=frozenset({"api_key"}),
    ),
    "llm_options": TypeMeta(
        model=LlmOptionsConfig,
    ),
    "influxdb": TypeMeta(
        model=InfluxDbConfig,
        secret_fields=frozenset({"token"}),
    ),
    "guardrails": TypeMeta(
        model=GuardrailsConfig,
    ),
    "circuit_breaker": TypeMeta(
        model=CircuitBreakerConfig,
    ),
    "scanner": TypeMeta(
        model=ScannerConfig,
    ),
    # --- Handlers (instantiable from DB rows) ---
    "ha_handler": TypeMeta(
        model=HaHandlerConfig,
        secret_fields=frozenset({"token"}),
        module_path="oasisagent.handlers.homeassistant",
        class_name="HomeAssistantHandler",
    ),
    "docker_handler": TypeMeta(
        model=DockerHandlerConfig,
        module_path="oasisagent.handlers.docker",
        class_name="DockerHandler",
    ),
    "portainer_handler": TypeMeta(
        model=PortainerHandlerConfig,
        secret_fields=frozenset({"api_key"}),
        module_path="oasisagent.handlers.portainer",
        class_name="PortainerHandler",
    ),
    "proxmox_handler": TypeMeta(
        model=ProxmoxHandlerConfig,
        secret_fields=frozenset({"token_value"}),
        module_path="oasisagent.handlers.proxmox",
        class_name="ProxmoxHandler",
    ),
    "unifi_handler": TypeMeta(
        model=UnifiHandlerConfig,
        secret_fields=frozenset({"password"}),
        module_path="oasisagent.handlers.unifi",
        class_name="UnifiHandler",
    ),
    "cloudflare_handler": TypeMeta(
        model=CloudflareHandlerConfig,
        secret_fields=frozenset({"api_token"}),
        module_path="oasisagent.handlers.cloudflare",
        class_name="CloudflareHandler",
    ),
}


# ---------------------------------------------------------------------------
# Notification channels
# ---------------------------------------------------------------------------

NOTIFICATION_TYPES: dict[str, TypeMeta] = {
    "mqtt_notification": TypeMeta(
        model=MqttNotificationConfig,
        secret_fields=frozenset({"password"}),
        module_path="oasisagent.notifications.mqtt",
        class_name="MqttNotificationChannel",
    ),
    "email": TypeMeta(
        model=EmailNotificationConfig,
        secret_fields=frozenset({"password"}),
        module_path="oasisagent.notifications.email",
        class_name="EmailNotificationChannel",
    ),
    "webhook": TypeMeta(
        model=WebhookNotificationConfig,
        module_path="oasisagent.notifications.webhook",
        class_name="WebhookNotificationChannel",
    ),
    "telegram": TypeMeta(
        model=TelegramNotificationConfig,
        secret_fields=frozenset({"bot_token"}),
        module_path="oasisagent.notifications.telegram",
        class_name="TelegramChannel",
    ),
    "discord": TypeMeta(
        model=DiscordNotificationConfig,
        secret_fields=frozenset({"webhook_url"}),
        module_path="oasisagent.notifications.discord",
        class_name="DiscordNotificationChannel",
    ),
    "slack": TypeMeta(
        model=SlackNotificationConfig,
        secret_fields=frozenset({"webhook_url"}),
        module_path="oasisagent.notifications.slack",
        class_name="SlackNotificationChannel",
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_REGISTRIES = {
    "connectors": CONNECTOR_TYPES,
    "core_services": CORE_SERVICE_TYPES,
    "notification_channels": NOTIFICATION_TYPES,
}


def get_type_meta(table: str, type_name: str) -> TypeMeta:
    """Look up type metadata by table name and type string.

    Raises:
        ValueError: If the type is not registered.
    """
    registry = _ALL_REGISTRIES.get(table)
    if registry is None:
        msg = f"Unknown table: {table}"
        raise ValueError(msg)
    meta = registry.get(type_name)
    if meta is None:
        valid = ", ".join(sorted(registry.keys()))
        msg = f"Unknown {table} type: {type_name!r}. Valid types: {valid}"
        raise ValueError(msg)
    return meta


def split_secrets(
    type_meta: TypeMeta, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a config dict into non-secret and secret portions.

    Returns:
        Tuple of ``(config_dict, secrets_dict)`` where secret fields have
        been removed from ``config_dict`` and placed in ``secrets_dict``.
    """
    if not type_meta.secret_fields:
        return dict(config), {}

    config_out: dict[str, Any] = {}
    secrets_out: dict[str, Any] = {}

    for key, value in config.items():
        if key in type_meta.secret_fields:
            secrets_out[key] = value
        else:
            config_out[key] = value

    return config_out, secrets_out


def merge_secrets(config_json: dict[str, Any], secrets: dict[str, Any]) -> dict[str, Any]:
    """Merge decrypted secrets back into the config dict.

    Secret keys win on conflict — this prevents accidental plaintext leakage
    if a secret field name somehow appears in ``config_json``.
    """
    merged = dict(config_json)
    merged.update(secrets)  # secrets win on conflict
    return merged
