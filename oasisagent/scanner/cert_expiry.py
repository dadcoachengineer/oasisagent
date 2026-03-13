"""TLS certificate expiry scanner.

Proactively checks TLS certificate expiry dates for configured endpoints.
Emits events on threshold transitions (ok -> warning -> critical) and when
certificates are renewed (warning/critical -> ok).

This scanner checks certificates from OasisAgent's vantage point using direct
TLS connections. It complements two other certificate monitoring sources:

- **Cloudflare adapter** (``ingestion/cloudflare.py``): Monitors certificates
  managed by Cloudflare via their API — covers Cloudflare-proxied domains.
- **Uptime Kuma adapter** (``ingestion/uptime_kuma.py``): Reports certificate
  data from Uptime Kuma's external monitoring — covers whatever monitors are
  configured there.

All three sources use different ``source`` fields and ``dedup_key`` prefixes,
so the pipeline can distinguish and correlate them.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.models import Event, EventMetadata, Severity
from oasisagent.scanner.base import ScannerIngestAdapter

if TYPE_CHECKING:
    from oasisagent.config import CertExpiryCheckConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class CertExpiryScannerAdapter(ScannerIngestAdapter):
    """Scanner that checks TLS certificate expiry for configured endpoints.

    Uses ``ssl.create_default_context()`` + ``asyncio.open_connection()``
    to fetch the peer certificate and extract ``notAfter``.

    State-based dedup: only emits events when the status for an endpoint
    changes (ok -> warning, warning -> critical, critical -> ok, etc.).
    """

    def __init__(
        self,
        config: CertExpiryCheckConfig,
        queue: EventQueue,
        interval: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(queue, interval, **kwargs)
        self._config = config
        # State tracking: endpoint -> "ok" | "warning" | "critical"
        self._states: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "scanner.cert_expiry"

    async def _scan(self) -> list[Event]:
        """Check certificate expiry for all configured endpoints."""
        events: list[Event] = []
        for endpoint in self._config.endpoints:
            try:
                days_remaining = await self._check_cert(endpoint)
                new_events = self._evaluate(endpoint, days_remaining)
                events.extend(new_events)
            except Exception as exc:
                logger.warning(
                    "Cert check failed for %s: %s", endpoint, exc,
                )
                events.extend(self._emit_check_error(endpoint, exc))
        return events

    async def _check_cert(self, endpoint: str) -> int:
        """Connect to endpoint via TLS and return days until cert expires.

        Args:
            endpoint: hostname or hostname:port string.

        Returns:
            Number of days until the certificate expires.
        """
        host, port = self._parse_endpoint(endpoint)
        ctx = ssl.create_default_context()

        _, writer = await asyncio.open_connection(host, port, ssl=ctx)
        try:
            ssl_object = writer.transport.get_extra_info("ssl_object")
            cert = ssl_object.getpeercert()
            not_after_str = cert["notAfter"]
            # SSL date format: "Mon DD HH:MM:SS YYYY GMT"
            not_after = datetime.strptime(
                not_after_str, "%b %d %H:%M:%S %Y %Z",
            ).replace(tzinfo=UTC)
            delta = not_after - datetime.now(tz=UTC)
            return delta.days
        finally:
            writer.close()
            await writer.wait_closed()

    def _evaluate(self, endpoint: str, days_remaining: int) -> list[Event]:
        """Evaluate days remaining against thresholds, emit on state change."""
        if days_remaining <= self._config.critical_days:
            new_state = "critical"
        elif days_remaining <= self._config.warning_days:
            new_state = "warning"
        else:
            new_state = "ok"

        old_state = self._states.get(endpoint)
        self._states[endpoint] = new_state

        if old_state == new_state:
            return []

        now = datetime.now(tz=UTC)

        # Transition to ok from warning/critical = certificate renewed
        if new_state == "ok" and old_state in ("warning", "critical"):
            return [Event(
                source=self.name,
                system="tls",
                event_type="certificate_renewed",
                entity_id=endpoint,
                severity=Severity.INFO,
                timestamp=now,
                payload={
                    "days_remaining": days_remaining,
                    "previous_state": old_state,
                },
                metadata=EventMetadata(
                    dedup_key=f"scanner.cert_expiry:{endpoint}",
                ),
            )]

        # Transition to warning or critical
        if new_state in ("warning", "critical"):
            severity = Severity.ERROR if new_state == "critical" else Severity.WARNING
            return [Event(
                source=self.name,
                system="tls",
                event_type="certificate_expiry",
                entity_id=endpoint,
                severity=severity,
                timestamp=now,
                payload={
                    "days_remaining": days_remaining,
                    "state": new_state,
                    "warning_days": self._config.warning_days,
                    "critical_days": self._config.critical_days,
                },
                metadata=EventMetadata(
                    dedup_key=f"scanner.cert_expiry:{endpoint}",
                ),
            )]

        # First scan with ok state — no event
        return []

    def _emit_check_error(self, endpoint: str, exc: Exception) -> list[Event]:
        """Emit an error event when the cert check itself fails."""
        # Only emit once per endpoint in error state
        old_state = self._states.get(endpoint)
        if old_state == "error":
            return []
        self._states[endpoint] = "error"

        return [Event(
            source=self.name,
            system="tls",
            event_type="certificate_check_error",
            entity_id=endpoint,
            severity=Severity.ERROR,
            timestamp=datetime.now(tz=UTC),
            payload={
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            metadata=EventMetadata(
                dedup_key=f"scanner.cert_expiry:{endpoint}:error",
            ),
        )]

    @staticmethod
    def _parse_endpoint(endpoint: str) -> tuple[str, int]:
        """Parse 'hostname' or 'hostname:port' into (host, port)."""
        if ":" in endpoint:
            host, port_str = endpoint.rsplit(":", 1)
            return host, int(port_str)
        return endpoint, 443
