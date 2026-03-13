"""Form field specifications for dynamic config UI rendering.

Maps each config type to a list of ``FieldSpec`` entries that drive the
shared ``form.html`` template.  The ``enabled`` field is deliberately
excluded — enable/disable lives on the list-page toggle, not in the form.

Complex nested fields (topic mappings, log patterns, etc.) are also excluded
and preserved on edit via the ConfigStore's shallow-merge update path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    """Describes a single form field for a config type."""

    name: str
    label: str
    input_type: str  # text, password, number, checkbox, select, textarea, list_str, float
    help_text: str = ""
    required: bool = False
    default: Any = None
    group: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    min_val: float | None = None
    max_val: float | None = None
    show_when: tuple[str, Any] | None = None  # ("field_name", value)


# ---------------------------------------------------------------------------
# Human-readable metadata per type
# ---------------------------------------------------------------------------

TYPE_DISPLAY_NAMES: dict[str, str] = {
    # Connectors
    "mqtt": "MQTT Broker",
    "ha_websocket": "Home Assistant WebSocket",
    "ha_log_poller": "Home Assistant Log Poller",
    "webhook_receiver": "Webhook Receiver",
    "http_poller": "HTTP Poller",
    "unifi": "UniFi Network",
    "cloudflare": "Cloudflare",
    "uptime_kuma": "Uptime Kuma",
    # Services
    "llm_triage": "LLM Triage (T1)",
    "llm_reasoning": "LLM Reasoning (T2)",
    "llm_options": "LLM Options",
    "ha_handler": "Home Assistant Handler",
    "docker_handler": "Docker Handler",
    "portainer_handler": "Portainer Handler",
    "proxmox_handler": "Proxmox Handler",
    "unifi_handler": "UniFi Handler",
    "cloudflare_handler": "Cloudflare Handler",
    "influxdb": "InfluxDB",
    "guardrails": "Guardrails",
    "circuit_breaker": "Circuit Breaker",
    "scanner": "Preventive Scanners",
    # Notifications
    "mqtt_notification": "MQTT Notifications",
    "email": "Email (SMTP)",
    "webhook": "Webhook",
    "telegram": "Telegram",
}

TYPE_DESCRIPTIONS: dict[str, str] = {
    # Connectors
    "mqtt": "Subscribe to MQTT topics for event ingestion",
    "ha_websocket": (
        "Real-time state changes and automation failures"
        " from Home Assistant"
    ),
    "ha_log_poller": "Pattern-match against Home Assistant error logs",
    "webhook_receiver": "Receive events via HTTP POST webhooks",
    "http_poller": (
        "Poll HTTP endpoints for health checks"
        " and data extraction"
    ),
    "unifi": (
        "Monitor UniFi network devices, alarms, and health"
    ),
    "cloudflare": (
        "Monitor Cloudflare tunnels, WAF events,"
        " and SSL certificates"
    ),
    "uptime_kuma": (
        "Pull monitor status from Uptime Kuma's"
        " Prometheus endpoint"
    ),
    # Services
    "llm_triage": (
        "Local SLM for event classification"
        " and context packaging"
    ),
    "llm_reasoning": "Cloud model for novel failure diagnosis",
    "llm_options": (
        "Shared LLM client settings (retries, cost tracking)"
    ),
    "ha_handler": (
        "Execute actions against Home Assistant"
        " (restart, reload)"
    ),
    "docker_handler": (
        "Manage Docker containers (restart, health checks)"
    ),
    "portainer_handler": (
        "Docker management via Portainer REST API"
    ),
    "proxmox_handler": (
        "VM and container management on Proxmox VE"
    ),
    "unifi_handler": (
        "Execute actions against UniFi controller"
    ),
    "cloudflare_handler": (
        "Manage Cloudflare (cache purge, IP blocking)"
    ),
    "influxdb": "Audit log storage in InfluxDB v2",
    "guardrails": (
        "Safety controls — blocked domains,"
        " kill switch, dry run"
    ),
    "circuit_breaker": (
        "Prevent remediation loops with attempt limits"
    ),
    "scanner": (
        "Proactive health checks"
        " (certificates, disk, integrations)"
    ),
    # Notifications
    "mqtt_notification": "Publish notifications to MQTT topics",
    "email": "Send notifications via SMTP email",
    "webhook": "POST notifications to webhook URLs",
    "telegram": "Send notifications via Telegram bot",
}

# Types that only allow a single instance
SINGLE_INSTANCE_TYPES: frozenset[str] = frozenset(
    TYPE_DISPLAY_NAMES.keys()
)


# ---------------------------------------------------------------------------
# Field specifications per config type
# ---------------------------------------------------------------------------

FORM_SPECS: dict[str, list[FieldSpec]] = {
    # -----------------------------------------------------------------------
    # Connectors (ingestion adapters)
    # -----------------------------------------------------------------------
    "mqtt": [
        FieldSpec(
            "broker", "Broker URL", "text",
            help_text="e.g. mqtt://localhost:1883",
            required=True,
            default="mqtt://localhost:1883",
        ),
        FieldSpec(
            "username", "Username", "text",
            group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            group="Authentication",
        ),
        FieldSpec(
            "client_id", "Client ID", "text",
            default="oasis-agent",
        ),
        FieldSpec(
            "qos", "QoS Level", "select",
            options=[
                ("0", "0 — At most once"),
                ("1", "1 — At least once"),
                ("2", "2 — Exactly once"),
            ],
            default=1,
        ),
        # topics: deferred (list of nested MqttTopicMapping)
    ],
    "ha_websocket": [
        FieldSpec(
            "url", "WebSocket URL", "text",
            help_text=(
                "e.g. ws://localhost:8123/api/websocket"
            ),
            required=True,
            default="ws://localhost:8123/api/websocket",
        ),
        FieldSpec(
            "token", "Long-Lived Access Token", "password",
            required=True, group="Authentication",
        ),
        # subscriptions: deferred (nested model)
    ],
    "ha_log_poller": [
        FieldSpec(
            "url", "Home Assistant URL", "text",
            help_text="e.g. http://localhost:8123",
            required=True,
            default="http://localhost:8123",
        ),
        FieldSpec(
            "token", "Long-Lived Access Token", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=30, min_val=1,
        ),
        FieldSpec(
            "dedup_window", "Dedup Window (seconds)",
            "number",
            help_text=(
                "Same error within this window = one event"
            ),
            default=300, min_val=0,
        ),
        # patterns: deferred (list of nested LogPattern)
    ],
    "webhook_receiver": [
        FieldSpec(
            "auth_mode", "Auth Mode", "select",
            options=[
                ("none", "None"),
                ("header_secret", "Header Secret"),
                ("api_key", "API Key"),
            ],
            default="none",
        ),
        FieldSpec(
            "auth_header", "Auth Header Name", "text",
            default="",
            show_when=("auth_mode", "header_secret"),
        ),
        FieldSpec(
            "auth_secret", "Auth Secret", "password",
            show_when=("auth_mode", "header_secret"),
        ),
        FieldSpec(
            "system", "System Name", "text",
            help_text="Source system identifier",
        ),
        FieldSpec(
            "event_type_field", "Event Type Field", "text",
            help_text="JSON field containing event type",
            default="eventType",
        ),
        FieldSpec(
            "entity_id_field", "Entity ID Field", "text",
            help_text="JSON field containing entity ID",
        ),
        FieldSpec(
            "default_severity", "Default Severity", "select",
            options=[
                ("info", "Info"),
                ("warning", "Warning"),
                ("error", "Error"),
                ("critical", "Critical"),
            ],
            default="warning",
        ),
        # event_mappings: deferred (list of nested WebhookEventMapping)
    ],
    "http_poller": [
        FieldSpec(
            "url", "Target URL", "text", required=True,
        ),
        FieldSpec(
            "system", "System Name", "text",
            help_text="Source system identifier",
            required=True,
        ),
        FieldSpec(
            "mode", "Response Mode", "select",
            options=[
                ("health_check", "Health Check"),
                ("extract", "Extract (JMESPath)"),
                ("threshold", "Threshold"),
            ],
            default="health_check",
        ),
        FieldSpec(
            "interval", "Poll Interval (seconds)",
            "number", default=60, min_val=5,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
        FieldSpec(
            "auth_mode", "Auth Mode", "select",
            options=[
                ("none", "None"),
                ("basic", "Basic Auth"),
                ("token", "Token/Bearer"),
            ],
            default="none", group="Authentication",
        ),
        FieldSpec(
            "auth_username", "Username", "text",
            group="Authentication",
            show_when=("auth_mode", "basic"),
        ),
        FieldSpec(
            "auth_password", "Password", "password",
            group="Authentication",
            show_when=("auth_mode", "basic"),
        ),
        FieldSpec(
            "auth_header", "Auth Header", "text",
            default="Authorization",
            group="Authentication",
            show_when=("auth_mode", "token"),
        ),
        FieldSpec(
            "auth_value", "Auth Value", "password",
            group="Authentication",
            show_when=("auth_mode", "token"),
        ),
        # extract, threshold: deferred (nested models)
    ],
    "unifi": [
        FieldSpec(
            "url", "Controller URL", "text",
            help_text="e.g. https://192.168.1.1",
            required=True,
            default="https://192.168.1.1",
        ),
        FieldSpec(
            "username", "Username", "text",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "site", "Site", "text", default="default",
        ),
        FieldSpec(
            "verify_ssl", "Verify SSL", "checkbox",
            default=False,
        ),
        FieldSpec(
            "is_udm", "UniFi Dream Machine", "checkbox",
            help_text="Enable for UDM/UDM-Pro API paths",
            default=True,
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=30,
        ),
        FieldSpec(
            "poll_alarms", "Poll Alarms", "checkbox",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_health", "Poll Health", "checkbox",
            default=True, group="Polling",
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
        FieldSpec(
            "cpu_threshold", "CPU Alert Threshold (%)",
            "float",
            default=90.0, min_val=0, max_val=100,
        ),
        FieldSpec(
            "memory_threshold",
            "Memory Alert Threshold (%)", "float",
            default=90.0, min_val=0, max_val=100,
        ),
    ],
    "cloudflare": [
        FieldSpec(
            "api_token", "API Token", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "account_id", "Account ID", "text",
            group="Authentication",
        ),
        FieldSpec(
            "zone_id", "Zone ID", "text",
            group="Authentication",
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=300,
        ),
        FieldSpec(
            "poll_tunnels", "Poll Tunnels", "checkbox",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_waf", "Poll WAF Events", "checkbox",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_ssl", "Poll SSL Status", "checkbox",
            default=True, group="Polling",
        ),
        FieldSpec(
            "waf_lookback_minutes",
            "WAF Lookback (minutes)", "number",
            default=10, min_val=1,
        ),
        FieldSpec(
            "waf_spike_threshold",
            "WAF Spike Threshold", "number",
            help_text=(
                "Events per lookback window"
                " to trigger alert"
            ),
            default=50, min_val=1,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=30, min_val=1,
        ),
    ],
    "uptime_kuma": [
        FieldSpec(
            "url", "Uptime Kuma URL", "text",
            help_text="e.g. http://uptime-kuma:3001",
            required=True,
        ),
        FieldSpec(
            "api_key", "API Key", "password",
            help_text="Uptime Kuma API key (v1.21+)",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=60,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
        FieldSpec(
            "response_time_threshold_ms",
            "Slow Response Threshold (ms)", "number",
            default=5000, min_val=1,
        ),
        FieldSpec(
            "cert_warning_days",
            "Cert Warning (days)", "number",
            help_text=(
                "Warn when certificate expires"
                " within N days"
            ),
            default=30, min_val=1,
        ),
        FieldSpec(
            "cert_critical_days",
            "Cert Critical (days)", "number",
            help_text=(
                "Alert when certificate expires"
                " within N days"
            ),
            default=7, min_val=1,
        ),
    ],

    # -----------------------------------------------------------------------
    # Core services
    # -----------------------------------------------------------------------
    "llm_triage": [
        FieldSpec(
            "base_url", "Base URL", "text",
            required=True,
            default="http://localhost:11434/v1",
        ),
        FieldSpec(
            "model", "Model Name", "text",
            required=True, default="qwen2.5:7b",
        ),
        FieldSpec(
            "api_key", "API Key", "password",
            group="Authentication",
        ),
        FieldSpec(
            "timeout", "Timeout (seconds)", "number",
            default=5, min_val=1,
        ),
        FieldSpec(
            "max_tokens", "Max Tokens", "number",
            default=1024, min_val=1,
        ),
        FieldSpec(
            "temperature", "Temperature", "float",
            default=0.1, min_val=0.0, max_val=2.0,
        ),
    ],
    "llm_reasoning": [
        FieldSpec(
            "base_url", "Base URL", "text",
            required=True,
            default="https://api.anthropic.com",
        ),
        FieldSpec(
            "model", "Model Name", "text",
            required=True,
            default="claude-sonnet-4-5-20250929",
        ),
        FieldSpec(
            "api_key", "API Key", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "timeout", "Timeout (seconds)", "number",
            default=45, min_val=1,
        ),
        FieldSpec(
            "max_tokens", "Max Tokens", "number",
            default=4096, min_val=1,
        ),
        FieldSpec(
            "temperature", "Temperature", "float",
            default=0.2, min_val=0.0, max_val=2.0,
        ),
    ],
    "llm_options": [
        FieldSpec(
            "cost_tracking", "Cost Tracking", "checkbox",
            help_text=(
                "Track token usage and estimated cost"
            ),
            default=True,
        ),
        FieldSpec(
            "retry_attempts", "Retry Attempts", "number",
            help_text="Retries on transient failures",
            default=2, min_val=0,
        ),
        FieldSpec(
            "fallback_to_triage",
            "Fallback to Triage", "checkbox",
            help_text=(
                "Use T1 if reasoning endpoint is down"
            ),
            default=True,
        ),
        FieldSpec(
            "log_prompts", "Log Prompts", "checkbox",
            help_text=(
                "WARNING: may log secrets in prompts"
            ),
            default=False,
        ),
    ],
    "ha_handler": [
        FieldSpec(
            "url", "Home Assistant URL", "text",
            required=True,
            default="http://localhost:8123",
        ),
        FieldSpec(
            "token", "Long-Lived Access Token", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "verify_timeout",
            "Verify Timeout (seconds)", "number",
            help_text=(
                "Wait time after action to verify effect"
            ),
            default=30, min_val=1,
        ),
        FieldSpec(
            "verify_poll_interval",
            "Verify Poll Interval (seconds)", "float",
            default=2.0, min_val=0.1,
        ),
    ],
    "docker_handler": [
        FieldSpec(
            "socket", "Docker Socket", "text",
            help_text=(
                "e.g. unix:///var/run/docker.sock"
            ),
            default="unix:///var/run/docker.sock",
        ),
        FieldSpec(
            "url", "Docker TCP URL", "text",
            help_text=(
                "For remote Docker hosts"
                " (overrides socket)"
            ),
        ),
        FieldSpec(
            "tls_verify", "Verify TLS", "checkbox",
            default=True,
        ),
        FieldSpec(
            "verify_timeout",
            "Verify Timeout (seconds)", "number",
            default=30, min_val=1,
        ),
        FieldSpec(
            "verify_poll_interval",
            "Verify Poll Interval (seconds)", "float",
            default=2.0, min_val=0.1,
        ),
    ],
    "portainer_handler": [
        FieldSpec(
            "url", "Portainer URL", "text",
            required=True,
            default="https://localhost:9443",
        ),
        FieldSpec(
            "api_key", "API Key", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "endpoint_id", "Environment ID", "number",
            help_text=(
                "Portainer endpoint/environment ID"
            ),
            default=1, min_val=1,
        ),
        FieldSpec(
            "verify_ssl", "Verify SSL", "checkbox",
            default=False,
        ),
        FieldSpec(
            "verify_timeout",
            "Verify Timeout (seconds)", "number",
            default=30, min_val=1,
        ),
        FieldSpec(
            "verify_poll_interval",
            "Verify Poll Interval (seconds)", "float",
            default=2.0, min_val=0.1,
        ),
    ],
    "proxmox_handler": [
        FieldSpec(
            "url", "Proxmox URL", "text",
            required=True,
            default="https://localhost:8006",
        ),
        FieldSpec(
            "user", "User", "text",
            help_text="e.g. root@pam",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "token_name", "Token Name", "text",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "token_value", "Token Value", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "verify_ssl", "Verify SSL", "checkbox",
            default=False,
        ),
    ],
    "unifi_handler": [
        FieldSpec(
            "url", "Controller URL", "text",
            required=True,
            default="https://192.168.1.1",
        ),
        FieldSpec(
            "username", "Username", "text",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "site", "Site", "text", default="default",
        ),
        FieldSpec(
            "verify_ssl", "Verify SSL", "checkbox",
            default=False,
        ),
        FieldSpec(
            "is_udm", "UniFi Dream Machine", "checkbox",
            default=True,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
        FieldSpec(
            "verify_timeout",
            "Verify Timeout (seconds)", "number",
            default=30, min_val=1,
        ),
        FieldSpec(
            "verify_poll_interval",
            "Verify Poll Interval (seconds)", "float",
            default=2.0, min_val=0.1,
        ),
    ],
    "cloudflare_handler": [
        FieldSpec(
            "api_token", "API Token", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "zone_id", "Zone ID", "text",
            group="Authentication",
        ),
        FieldSpec(
            "account_id", "Account ID", "text",
            group="Authentication",
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=30, min_val=1,
        ),
    ],
    "influxdb": [
        FieldSpec(
            "url", "InfluxDB URL", "text",
            required=True,
            default="http://localhost:8086",
        ),
        FieldSpec(
            "token", "API Token", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "org", "Organization", "text",
            required=True, default="myorg",
        ),
        FieldSpec(
            "bucket", "Bucket", "text",
            required=True, default="oasisagent",
        ),
    ],
    "guardrails": [
        FieldSpec(
            "blocked_domains", "Blocked Domains",
            "list_str",
            help_text=(
                "Glob patterns — one per line"
                " (e.g. lock.*)"
            ),
        ),
        FieldSpec(
            "blocked_entities", "Blocked Entities",
            "list_str",
            help_text=(
                "Specific entity IDs — one per line"
            ),
        ),
        FieldSpec(
            "kill_switch", "Kill Switch", "checkbox",
            help_text=(
                "Disable ALL automated actions instantly"
            ),
            default=False,
        ),
        FieldSpec(
            "dry_run", "Dry Run", "checkbox",
            help_text=(
                "Log decisions without executing actions"
            ),
            default=False,
        ),
        FieldSpec(
            "approval_timeout_minutes",
            "Approval Timeout (minutes)", "number",
            default=30, min_val=1,
        ),
        # circuit_breaker: handled as inline fields below
    ],
    "circuit_breaker": [
        FieldSpec(
            "max_attempts_per_entity",
            "Max Attempts per Entity", "number",
            default=3, min_val=1,
        ),
        FieldSpec(
            "window_minutes", "Window (minutes)",
            "number",
            help_text=(
                "Rolling window for attempt counting"
            ),
            default=60, min_val=1,
        ),
        FieldSpec(
            "cooldown_minutes", "Cooldown (minutes)",
            "number",
            help_text=(
                "Minimum time between retries"
                " on same entity"
            ),
            default=15, min_val=0,
        ),
        FieldSpec(
            "global_failure_rate_threshold",
            "Global Failure Rate Threshold", "float",
            help_text=(
                "Fraction (0-1) triggering global pause"
            ),
            default=0.3, min_val=0.01, max_val=1.0,
        ),
        FieldSpec(
            "global_pause_minutes",
            "Global Pause (minutes)", "number",
            default=30, min_val=1,
        ),
    ],
    "scanner": [],  # Scanner uses custom page, no generic form specs

    # -----------------------------------------------------------------------
    # Notification channels
    # -----------------------------------------------------------------------
    "mqtt_notification": [
        FieldSpec(
            "broker", "Broker URL", "text",
            required=True,
            default="mqtt://localhost:1883",
        ),
        FieldSpec(
            "topic_prefix", "Topic Prefix", "text",
            default="oasis/notifications",
        ),
        FieldSpec(
            "username", "Username", "text",
            group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            group="Authentication",
        ),
        FieldSpec(
            "qos", "QoS Level", "select",
            options=[
                ("0", "0 — At most once"),
                ("1", "1 — At least once"),
                ("2", "2 — Exactly once"),
            ],
            default=1,
        ),
        FieldSpec(
            "retain", "Retain Messages", "checkbox",
            default=False,
        ),
    ],
    "email": [
        FieldSpec(
            "smtp_host", "SMTP Host", "text",
            required=True, default="localhost",
        ),
        FieldSpec(
            "smtp_port", "SMTP Port", "number",
            default=587, min_val=1, max_val=65535,
        ),
        FieldSpec(
            "username", "Username", "text",
            group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            group="Authentication",
        ),
        FieldSpec(
            "starttls", "STARTTLS", "checkbox",
            default=True,
        ),
        FieldSpec(
            "from_address", "From Address", "text",
            required=True,
            default="oasis-agent@example.com",
        ),
        FieldSpec(
            "to", "To Addresses", "list_str",
            help_text="One email address per line",
            required=True,
        ),
    ],
    "webhook": [
        FieldSpec(
            "urls", "Webhook URLs", "list_str",
            help_text="One URL per line",
            required=True,
        ),
    ],
    "telegram": [
        FieldSpec(
            "bot_token", "Bot Token", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "chat_id", "Chat ID", "text",
            required=True,
        ),
        FieldSpec(
            "parse_mode", "Parse Mode", "select",
            options=[
                ("HTML", "HTML"),
                ("MarkdownV2", "MarkdownV2"),
            ],
            default="HTML",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Helpers for form processing
# ---------------------------------------------------------------------------

# Fields that are deferred (complex nested types, not rendered in forms).
# These are preserved on edit via ConfigStore's shallow merge.
DEFERRED_FIELDS: dict[str, frozenset[str]] = {
    "mqtt": frozenset({"topics"}),
    "ha_websocket": frozenset({"subscriptions"}),
    "ha_log_poller": frozenset({"patterns"}),
    "webhook_receiver": frozenset({"event_mappings"}),
    "http_poller": frozenset({"extract", "threshold"}),
    "guardrails": frozenset({"circuit_breaker"}),
    # Scanner uses a custom page (PR 2) — all fields deferred
    "scanner": frozenset({
        "interval", "adaptive_enabled", "adaptive_fast_factor",
        "adaptive_recovery_scans", "certificate_expiry", "disk_space",
        "ha_health", "docker_health",
    }),
}


def get_form_specs(type_name: str) -> list[FieldSpec]:
    """Return the FieldSpec list for a config type, or empty list if unknown."""
    return FORM_SPECS.get(type_name, [])


def get_display_name(type_name: str) -> str:
    """Return the human-readable display name for a type."""
    return TYPE_DISPLAY_NAMES.get(type_name, type_name)


def get_description(type_name: str) -> str:
    """Return the one-line description for a type."""
    return TYPE_DESCRIPTIONS.get(type_name, "")
