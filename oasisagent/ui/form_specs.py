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
    "frigate": "Frigate NVR",
    "npm": "Nginx Proxy Manager",
    "servarr": "Servarr (Sonarr/Radarr/Prowlarr/Bazarr)",
    "qbittorrent": "qBittorrent",
    "plex": "Plex Media Server",
    "tautulli": "Tautulli",
    "tdarr": "Tdarr",
    "n8n": "N8N",
    "overseerr": "Overseerr",
    "vaultwarden": "Vaultwarden",
    "proxmox": "Proxmox VE",
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
    "discord": "Discord",
    "slack": "Slack",
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
    "frigate": (
        "Monitor Frigate NVR camera health"
        " and detection events"
    ),
    "npm": (
        "Monitor Nginx Proxy Manager proxy hosts,"
        " certificates, and dead hosts"
    ),
    "servarr": (
        "Monitor Sonarr, Radarr, Prowlarr, or Bazarr"
        " health and download queue"
    ),
    "qbittorrent": (
        "Monitor qBittorrent for errored, stalled,"
        " and disconnected torrents"
    ),
    "plex": (
        "Monitor Plex Media Server reachability"
        " and library health"
    ),
    "tautulli": (
        "Monitor Plex via Tautulli — server status"
        " and bandwidth"
    ),
    "tdarr": (
        "Monitor Tdarr transcoding workers"
        " and queue progress"
    ),
    "n8n": (
        "Monitor N8N workflow execution health"
        " and failed runs"
    ),
    "overseerr": (
        "Monitor Overseerr server connectivity"
    ),
    "vaultwarden": (
        "Monitor Vaultwarden (Bitwarden) service health"
    ),
    "proxmox": (
        "Poll Proxmox VE for node status, VM states,"
        " tasks, and replication"
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
    "discord": "Send notifications via Discord webhook",
    "slack": "Send notifications via Slack Incoming Webhook",
}

# Types that allow multiple instances (e.g., multiple Servarr apps)
MULTI_INSTANCE_TYPES: frozenset[str] = frozenset({
    "servarr",
    "http_poller",
    "webhook_receiver",
})

# Types that only allow a single instance (everything else)
SINGLE_INSTANCE_TYPES: frozenset[str] = frozenset(
    TYPE_DISPLAY_NAMES.keys() - MULTI_INSTANCE_TYPES
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
            help_text="Broker username — leave blank if anonymous",
            group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            help_text="Broker password — leave blank if anonymous",
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
            help_text=(
                "HA → Profile → Long-Lived Access Tokens"
                " → Create Token"
            ),
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
            help_text=(
                "HA → Profile → Long-Lived Access Tokens"
                " → Create Token"
            ),
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
            help_text="Shared secret the sender includes in the header",
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
            help_text="HTTP Basic Auth username",
            group="Authentication",
            show_when=("auth_mode", "basic"),
        ),
        FieldSpec(
            "auth_password", "Password", "password",
            help_text="HTTP Basic Auth password",
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
            help_text="e.g. Bearer <token> or raw API key",
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
            help_text="Local admin account — avoid using SSO credentials",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            help_text="Password for the local admin account",
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
            "poll_ips", "Poll IDS/IPS Events", "checkbox",
            help_text="Monitor intrusion detection/prevention alerts",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_rogue_ap", "Poll Rogue APs", "checkbox",
            help_text="Detect unauthorized access points",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_clients", "Poll Clients", "checkbox",
            help_text="Track client count spikes (high volume, disabled by default)",
            default=False, group="Polling",
        ),
        FieldSpec(
            "poll_anomalies", "Poll Anomalies", "checkbox",
            help_text="Monitor network traffic anomalies",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_events", "Poll Controller Events", "checkbox",
            help_text="Monitor actionable controller events",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_dpi", "Poll DPI Stats", "checkbox",
            help_text="Monitor bandwidth by app category (disabled by default)",
            default=False, group="Polling",
        ),
        FieldSpec(
            "client_spike_threshold",
            "Client Spike Threshold (%)", "float",
            help_text="Percent change in client count to trigger alert",
            default=20.0, min_val=1, max_val=100,
            group="Thresholds",
        ),
        FieldSpec(
            "dpi_bandwidth_threshold_mbps",
            "DPI Bandwidth Threshold (Mbps)", "float",
            help_text="Bandwidth per app category to trigger alert",
            default=100.0, min_val=1,
            group="Thresholds",
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
        FieldSpec(
            "cpu_threshold", "CPU Alert Threshold (%)",
            "float",
            default=90.0, min_val=0, max_val=100,
            group="Thresholds",
        ),
        FieldSpec(
            "memory_threshold",
            "Memory Alert Threshold (%)", "float",
            default=90.0, min_val=0, max_val=100,
            group="Thresholds",
        ),
    ],
    "cloudflare": [
        FieldSpec(
            "api_token", "API Token", "password",
            help_text=(
                "Cloudflare dashboard → My Profile"
                " → API Tokens → Create Token"
            ),
            required=True, group="Authentication",
        ),
        FieldSpec(
            "account_id", "Account ID", "text",
            help_text=(
                "Dashboard → any domain → Overview sidebar"
                " → Account ID"
            ),
            group="Authentication",
        ),
        FieldSpec(
            "zone_id", "Zone ID", "text",
            help_text=(
                "Dashboard → domain → Overview sidebar"
                " → Zone ID"
            ),
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

    "frigate": [
        FieldSpec(
            "url", "Frigate URL", "text",
            help_text="e.g. https://192.168.1.130:8971",
            required=True,
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=60, min_val=5,
        ),
        FieldSpec(
            "poll_events", "Poll Detection Events",
            "checkbox",
            help_text=(
                "Track detection event spikes"
                " (disabled by default)"
            ),
            default=False, group="Polling",
        ),
        FieldSpec(
            "detector_fps_threshold",
            "Detector FPS Threshold", "float",
            help_text=(
                "Alert when detector inference FPS"
                " drops below this value"
            ),
            default=5.0, min_val=0.1,
            group="Thresholds",
        ),
        FieldSpec(
            "detection_spike_threshold",
            "Detection Spike Threshold", "number",
            help_text=(
                "Detections per poll interval to"
                " trigger a spike alert"
            ),
            default=20, min_val=1,
            group="Thresholds",
        ),
        FieldSpec(
            "verify_ssl", "Verify SSL Certificate",
            "checkbox",
            help_text="Disable for self-signed certificates",
            default=False,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
    ],
    "npm": [
        FieldSpec(
            "url", "NPM URL", "text",
            help_text="e.g. http://192.168.1.50:81",
            required=True,
        ),
        FieldSpec(
            "email", "Admin Email", "text",
            help_text="NPM admin login email",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            help_text="NPM admin login password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=300,
        ),
        FieldSpec(
            "poll_proxy_hosts", "Poll Proxy Hosts",
            "checkbox", default=True, group="Polling",
        ),
        FieldSpec(
            "poll_certificates", "Poll Certificates",
            "checkbox", default=True, group="Polling",
        ),
        FieldSpec(
            "poll_dead_hosts", "Poll Dead Hosts",
            "checkbox", default=True, group="Polling",
        ),
        FieldSpec(
            "cert_warning_days",
            "Cert Warning (days)", "number",
            help_text=(
                "Warn when certificate expires"
                " within N days"
            ),
            default=14, min_val=1,
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
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
    ],

    "servarr": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. http://localhost:8989",
            required=True,
        ),
        FieldSpec(
            "api_key", "API Key", "password",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "app_type", "Application", "select",
            options=[
                ("sonarr", "Sonarr"),
                ("radarr", "Radarr"),
                ("prowlarr", "Prowlarr"),
                ("bazarr", "Bazarr"),
            ],
            default="sonarr",
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=60,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
    ],
    "qbittorrent": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. http://localhost:8080",
            required=True,
        ),
        FieldSpec(
            "username", "Username", "text",
            required=True, group="Authentication",
            default="admin",
        ),
        FieldSpec(
            "password", "Password", "password",
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
    ],
    "plex": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. http://localhost:32400",
            required=True,
        ),
        FieldSpec(
            "token", "X-Plex-Token", "password",
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
    ],
    "tautulli": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. http://localhost:8181",
            required=True,
        ),
        FieldSpec(
            "api_key", "API Key", "password",
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
            "bandwidth_threshold_kbps",
            "Bandwidth Threshold (kbps)", "number",
            help_text=(
                "Alert when total bandwidth exceeds"
                " this value (default: 100000 = 100 Mbps)"
            ),
            default=100_000, min_val=1,
        ),
    ],
    "tdarr": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. http://localhost:8265",
            required=True,
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=60,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
    ],
    "n8n": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. http://localhost:5678",
            required=True,
        ),
        FieldSpec(
            "api_key", "API Key", "password",
            help_text=(
                "N8N Settings → API → Create API Key"
            ),
            required=True, group="Authentication",
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=300,
        ),
        FieldSpec(
            "poll_executions",
            "Poll Failed Executions", "checkbox",
            help_text=(
                "Monitor for failed workflow executions"
            ),
            default=True, group="Polling",
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
    ],
    "overseerr": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. http://localhost:5055",
            required=True,
        ),
        FieldSpec(
            "api_key", "API Key", "password",
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
    ],
    "vaultwarden": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. https://192.168.2.100:8000",
            required=True,
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=60,
        ),
        FieldSpec(
            "verify_ssl", "Verify SSL Certificate",
            "checkbox",
            help_text="Disable for self-signed certificates",
            default=False,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
    ],

    "proxmox": [
        FieldSpec(
            "url", "Server URL", "text",
            help_text="e.g. https://192.168.1.106:8006",
            required=True,
            default="https://localhost:8006",
        ),
        FieldSpec(
            "user", "API User", "text",
            help_text="e.g. root@pam",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "token_name", "Token Name", "text",
            help_text="API token name (Datacenter → Permissions → API Tokens)",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "token_value", "Token Value", "password",
            help_text="API token secret value",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "verify_ssl", "Verify SSL", "checkbox",
            help_text="Disable for self-signed certificates",
            default=False,
        ),
        FieldSpec(
            "poll_interval", "Poll Interval (seconds)",
            "number", default=30,
        ),
        FieldSpec(
            "timeout", "Request Timeout (seconds)",
            "number", default=10, min_val=1,
        ),
        FieldSpec(
            "poll_nodes", "Poll Nodes", "checkbox",
            help_text="Monitor node online/offline status and resources",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_vms", "Poll VMs/CTs", "checkbox",
            help_text="Monitor VM and container state transitions",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_tasks", "Poll Tasks", "checkbox",
            help_text="Detect failed backups and other task errors",
            default=True, group="Polling",
        ),
        FieldSpec(
            "poll_replication", "Poll Replication", "checkbox",
            help_text="Monitor replication job failures",
            default=True, group="Polling",
        ),
        FieldSpec(
            "cpu_threshold", "CPU Alert Threshold (%)",
            "number", default=90.0, min_val=0, max_val=100,
            group="Thresholds",
        ),
        FieldSpec(
            "memory_threshold", "Memory Alert Threshold (%)",
            "number", default=90.0, min_val=0, max_val=100,
            group="Thresholds",
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
            help_text=(
                "Optional for local Ollama; required for"
                " OpenRouter or other hosted providers"
            ),
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
            help_text=(
                "Provider API key — e.g. OpenRouter,"
                " Anthropic, or OpenAI"
            ),
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
            help_text=(
                "HA → Profile → Long-Lived Access Tokens"
                " → Create Token"
            ),
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
            help_text=(
                "Portainer → My Account → Access Tokens"
                " → Add access token"
            ),
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
            help_text=(
                "Datacenter → Permissions → API Tokens"
                " → token ID (without user@realm!)"
            ),
            required=True, group="Authentication",
        ),
        FieldSpec(
            "token_value", "Token Value", "password",
            help_text="Secret value shown once at token creation",
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
            help_text="Local admin account — avoid using SSO credentials",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            help_text="Password for the local admin account",
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
            help_text=(
                "Cloudflare dashboard → My Profile"
                " → API Tokens → Create Token"
            ),
            required=True, group="Authentication",
        ),
        FieldSpec(
            "zone_id", "Zone ID", "text",
            help_text=(
                "Dashboard → domain → Overview sidebar"
                " → Zone ID"
            ),
            group="Authentication",
        ),
        FieldSpec(
            "account_id", "Account ID", "text",
            help_text=(
                "Dashboard → any domain → Overview sidebar"
                " → Account ID"
            ),
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
            help_text=(
                "InfluxDB → Data → API Tokens"
                " → Generate Token"
            ),
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
            help_text="Broker username — leave blank if anonymous",
            group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            help_text="Broker password — leave blank if anonymous",
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
            help_text="SMTP login — often your email address",
            group="Authentication",
        ),
        FieldSpec(
            "password", "Password", "password",
            help_text="SMTP password or app-specific password",
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
            help_text="Message @BotFather on Telegram → /newbot",
            required=True, group="Authentication",
        ),
        FieldSpec(
            "chat_id", "Chat ID", "text",
            help_text=(
                "Numeric chat/group ID — message your bot"
                " then check /getUpdates"
            ),
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
    "discord": [
        FieldSpec(
            "webhook_url", "Webhook URL", "password",
            help_text=(
                "Server Settings → Integrations → Webhooks"
                " → Copy Webhook URL"
            ),
            required=True,
        ),
        FieldSpec(
            "username", "Bot Username", "text",
            help_text="Display name for the webhook messages",
            default="OasisAgent",
        ),
        FieldSpec(
            "avatar_url", "Avatar URL", "text",
            help_text="URL for the webhook avatar image (optional)",
        ),
    ],
    "slack": [
        FieldSpec(
            "webhook_url", "Webhook URL", "password",
            help_text=(
                "Slack Incoming Webhook URL — create one at"
                " api.slack.com/apps → Incoming Webhooks"
            ),
            required=True,
        ),
        FieldSpec(
            "channel", "Channel Override", "text",
            help_text=(
                "Override the default channel (e.g. #alerts)."
                " Leave blank to use the webhook default"
            ),
        ),
        FieldSpec(
            "username", "Bot Username", "text",
            help_text="Display name for the bot in Slack",
            default="OasisAgent",
        ),
        FieldSpec(
            "icon_emoji", "Icon Emoji", "text",
            help_text="Emoji for the bot avatar (e.g. :robot_face:)",
            default=":robot_face:",
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
        "ha_health", "docker_health", "backup_freshness",
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
