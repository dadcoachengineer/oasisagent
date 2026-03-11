"""Webhook ingestion processor — auth validation and event mapping.

This module handles the logic of converting an incoming webhook HTTP
request into a canonical Event. The FastAPI route handler in
``oasisagent.web.webhook`` delegates to this processor.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import WebhookSourceConfig

logger = logging.getLogger(__name__)

# Map string severity names to Severity enum (case-insensitive)
_SEVERITY_MAP: dict[str, Severity] = {
    "info": Severity.INFO,
    "warning": Severity.WARNING,
    "error": Severity.ERROR,
    "critical": Severity.CRITICAL,
}


def _extract_dotted(payload: dict[str, Any], path: str) -> str | None:
    """Extract a value from a nested dict using a dotted path.

    >>> _extract_dotted({"movie": {"title": "Inception"}}, "movie.title")
    'Inception'
    """
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return str(current) if current is not None else None


def validate_auth(
    config: WebhookSourceConfig,
    headers: dict[str, str],
    query_params: dict[str, str],
) -> bool:
    """Check whether the request passes the source's auth requirements.

    Uses constant-time comparison to prevent timing attacks.
    """
    if config.auth_mode == "none":
        return True

    if config.auth_mode == "header_secret":
        actual = headers.get(config.auth_header.lower(), "")
        return hmac.compare_digest(actual, config.auth_secret)

    if config.auth_mode == "api_key":
        # Check X-Api-Key header or ?apikey= query param
        from_header = headers.get("x-api-key", "")
        from_query = query_params.get("apikey", "")
        return (
            hmac.compare_digest(from_header, config.auth_secret)
            or hmac.compare_digest(from_query, config.auth_secret)
        )

    return False


def map_event(
    config: WebhookSourceConfig,
    source_name: str,
    payload: dict[str, Any],
) -> Event:
    """Convert a webhook payload into a canonical Event.

    Uses the source config's event mappings and field extraction rules
    to populate the Event fields.
    """
    # Extract source event name from payload
    raw_event_type = str(payload.get(config.event_type_field, "unknown"))

    # Look up in event mappings
    event_type = raw_event_type
    severity_str = config.default_severity
    for mapping in config.event_mappings:
        if mapping.source_event == raw_event_type:
            event_type = mapping.event_type
            severity_str = mapping.severity
            break

    severity = _SEVERITY_MAP.get(severity_str.lower(), Severity.WARNING)

    # Extract entity_id
    entity_id = None
    if config.entity_id_field:
        entity_id = _extract_dotted(payload, config.entity_id_field)
    if not entity_id:
        entity_id = f"{source_name}:{event_type}"

    system = config.system or source_name

    return Event(
        source=f"webhook:{source_name}",
        system=system,
        event_type=event_type,
        entity_id=entity_id,
        severity=severity,
        timestamp=datetime.now(tz=UTC),
        payload=payload,
        metadata=EventMetadata(
            dedup_key=f"webhook:{source_name}:{event_type}:{entity_id}",
        ),
    )
