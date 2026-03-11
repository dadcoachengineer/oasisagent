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
    DockerHandlerConfig,
    EmailNotificationConfig,
    GuardrailsConfig,
    HaHandlerConfig,
    HaLogPollerConfig,
    HaWebSocketConfig,
    InfluxDbConfig,
    LlmEndpointConfig,
    LlmOptionsConfig,
    MqttIngestionConfig,
    MqttNotificationConfig,
    ProxmoxHandlerConfig,
    WebhookNotificationConfig,
    WebhookSourceConfig,
)


@dataclass(frozen=True)
class TypeMeta:
    """Metadata for a stored config type."""

    model: type[BaseModel]
    secret_fields: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Connectors (ingestion adapters)
# ---------------------------------------------------------------------------

CONNECTOR_TYPES: dict[str, TypeMeta] = {
    "mqtt": TypeMeta(
        model=MqttIngestionConfig,
        secret_fields=frozenset({"password"}),
    ),
    "ha_websocket": TypeMeta(
        model=HaWebSocketConfig,
        secret_fields=frozenset({"token"}),
    ),
    "ha_log_poller": TypeMeta(
        model=HaLogPollerConfig,
        secret_fields=frozenset({"token"}),
    ),
    "webhook_receiver": TypeMeta(
        model=WebhookSourceConfig,
        secret_fields=frozenset({"auth_secret"}),
    ),
}


# ---------------------------------------------------------------------------
# Core services (handlers, LLM endpoints, audit, guardrails)
# ---------------------------------------------------------------------------

CORE_SERVICE_TYPES: dict[str, TypeMeta] = {
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
    "ha_handler": TypeMeta(
        model=HaHandlerConfig,
        secret_fields=frozenset({"token"}),
    ),
    "docker_handler": TypeMeta(
        model=DockerHandlerConfig,
    ),
    "proxmox_handler": TypeMeta(
        model=ProxmoxHandlerConfig,
        secret_fields=frozenset({"token_value"}),
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
}


# ---------------------------------------------------------------------------
# Notification channels
# ---------------------------------------------------------------------------

NOTIFICATION_TYPES: dict[str, TypeMeta] = {
    "mqtt_notification": TypeMeta(
        model=MqttNotificationConfig,
        secret_fields=frozenset({"password"}),
    ),
    "email": TypeMeta(
        model=EmailNotificationConfig,
        secret_fields=frozenset({"password"}),
    ),
    "webhook": TypeMeta(
        model=WebhookNotificationConfig,
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
