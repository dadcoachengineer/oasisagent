"""Cloudflare polling ingestion adapter.

Polls the Cloudflare API v4 for tunnel status, WAF security events,
and SSL certificate expiry. Emits events on state transitions or
threshold crossings.

Three separate polling paths:
- Tunnel status: state-based dedup (tunnel_id → status)
- WAF events: time-based lookback window (no persistent cursor)
- SSL certificates: threshold-based (days until expiry)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from oasisagent.clients.cloudflare import CloudflareClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import CloudflareAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# SSL expiry thresholds (days)
_SSL_WARNING_DAYS = 30
_SSL_CRITICAL_DAYS = 7


class CloudflareAdapter(IngestAdapter):
    """Polls Cloudflare API for tunnel health, WAF events, and SSL status.

    State-based dedup for tunnels, time-window dedup for WAF events.
    """

    def __init__(
        self, config: CloudflareAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._client = CloudflareClient(
            api_token=config.api_token,
            timeout=config.timeout,
        )
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # Dedup trackers
        self._tunnel_states: dict[str, str] = {}  # tunnel_id → status
        self._last_waf_poll: datetime | None = None
        self._ssl_alert_state: dict[str, str] = {}  # hostname → "ok"|"warning"|"critical"

    @property
    def name(self) -> str:
        return "cloudflare"

    async def start(self) -> None:
        """Connect to Cloudflare API and start the polling loop."""
        try:
            await self._client.start()
            self._connected = True
        except Exception as exc:
            logger.error("Cloudflare adapter: connection failed: %s", exc)
            self._connected = False
            return

        self._task = asyncio.create_task(
            self._poll_loop(), name="cloudflare-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
        await self._client.close()
        self._connected = False

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main polling loop — polls all enabled endpoints each interval."""
        while not self._stopping:
            try:
                if self._config.poll_tunnels and self._config.account_id:
                    await self._poll_tunnels()

                if self._config.poll_waf and self._config.zone_id:
                    await self._poll_waf()

                if self._config.poll_ssl and self._config.zone_id:
                    await self._poll_ssl()

                self._connected = True
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Cloudflare poll error: %s", exc)
                self._connected = False

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Tunnel polling
    # -----------------------------------------------------------------

    async def _poll_tunnels(self) -> None:
        """Poll tunnel status for state transitions."""
        path = f"/accounts/{self._config.account_id}/cfd_tunnel"
        data = await self._client.get(path)
        tunnels: list[dict[str, Any]] = data.get("result", [])

        for tunnel in tunnels:
            tunnel_id = tunnel.get("id", "")
            if not tunnel_id:
                continue

            status = tunnel.get("status", "unknown")
            tunnel_name = tunnel.get("name", tunnel_id)
            prev_status = self._tunnel_states.get(tunnel_id)
            self._tunnel_states[tunnel_id] = status

            if prev_status is not None and prev_status != status:
                if status != "active":
                    self._enqueue(Event(
                        source=self.name,
                        system="cloudflare",
                        event_type="tunnel_disconnected",
                        entity_id=tunnel_name,
                        severity=Severity.ERROR,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "tunnel_id": tunnel_id,
                            "name": tunnel_name,
                            "status": status,
                            "previous_status": prev_status,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"cloudflare:tunnel:{tunnel_id}:status",
                        ),
                    ))
                elif prev_status != "active":
                    self._enqueue(Event(
                        source=self.name,
                        system="cloudflare",
                        event_type="tunnel_recovered",
                        entity_id=tunnel_name,
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "tunnel_id": tunnel_id,
                            "name": tunnel_name,
                            "status": status,
                            "previous_status": prev_status,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"cloudflare:tunnel:{tunnel_id}:status",
                        ),
                    ))
            elif prev_status is None and status != "active":
                # First poll, tunnel already down
                self._enqueue(Event(
                    source=self.name,
                    system="cloudflare",
                    event_type="tunnel_disconnected",
                    entity_id=tunnel_name,
                    severity=Severity.ERROR,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "tunnel_id": tunnel_id,
                        "name": tunnel_name,
                        "status": status,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"cloudflare:tunnel:{tunnel_id}:status",
                    ),
                ))

    # -----------------------------------------------------------------
    # WAF event polling
    # -----------------------------------------------------------------

    async def _poll_waf(self) -> None:
        """Poll WAF security events using a lookback window."""
        now = datetime.now(tz=UTC)
        lookback = timedelta(minutes=self._config.waf_lookback_minutes)

        since = self._last_waf_poll if self._last_waf_poll is not None else now - lookback

        self._last_waf_poll = now

        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        path = f"/zones/{self._config.zone_id}/security/events"
        data = await self._client.get(path, since=since_str)
        events: list[dict[str, Any]] = data.get("result", [])

        if not events:
            return

        # Count blocked events by rule for spike detection
        blocked_count = 0
        top_rules: dict[str, int] = {}
        source_ips: set[str] = set()

        for waf_event in events:
            action = waf_event.get("action", "")
            if action in ("block", "challenge", "managed_challenge"):
                blocked_count += 1
                rule_id = waf_event.get("ruleId", "unknown")
                top_rules[rule_id] = top_rules.get(rule_id, 0) + 1
                source_ip = waf_event.get("clientIP", "")
                if source_ip:
                    source_ips.add(source_ip)

        if blocked_count >= self._config.waf_spike_threshold:
            # Sort rules by count descending, take top 5
            sorted_rules = sorted(
                top_rules.items(), key=lambda x: x[1], reverse=True,
            )[:5]

            self._enqueue(Event(
                source=self.name,
                system="cloudflare",
                event_type="waf_spike",
                entity_id=self._config.zone_id,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "blocked_count": blocked_count,
                    "total_events": len(events),
                    "top_rules": dict(sorted_rules),
                    "unique_source_ips": len(source_ips),
                    "window_minutes": self._config.waf_lookback_minutes,
                },
                metadata=EventMetadata(
                    dedup_key=f"cloudflare:waf:{self._config.zone_id}:spike",
                ),
            ))

    # -----------------------------------------------------------------
    # SSL certificate polling
    # -----------------------------------------------------------------

    async def _poll_ssl(self) -> None:
        """Poll SSL certificate status for expiry warnings."""
        path = f"/zones/{self._config.zone_id}/ssl/certificate_packs"
        data = await self._client.get(path)
        packs: list[dict[str, Any]] = data.get("result", [])

        for pack in packs:
            hosts = pack.get("hosts", [])
            certs = pack.get("certificates", [])

            for cert in certs:
                expires_on = cert.get("expires_on", "")
                if not expires_on:
                    continue

                try:
                    expiry = datetime.fromisoformat(
                        expires_on.replace("Z", "+00:00"),
                    )
                except ValueError:
                    continue

                days_remaining = (expiry - datetime.now(tz=UTC)).days
                hostname = hosts[0] if hosts else "unknown"
                self._check_ssl_expiry(hostname, days_remaining, pack)

    def _check_ssl_expiry(
        self,
        hostname: str,
        days_remaining: int,
        pack: dict[str, Any],
    ) -> None:
        """Emit events on SSL certificate expiry threshold transitions."""
        if days_remaining <= _SSL_CRITICAL_DAYS:
            new_state = "critical"
            severity = Severity.ERROR
        elif days_remaining <= _SSL_WARNING_DAYS:
            new_state = "warning"
            severity = Severity.WARNING
        else:
            new_state = "ok"
            severity = Severity.INFO  # only used if recovering

        prev_state = self._ssl_alert_state.get(hostname, "ok")
        self._ssl_alert_state[hostname] = new_state

        if new_state == prev_state:
            return

        if new_state in ("warning", "critical"):
            self._enqueue(Event(
                source=self.name,
                system="cloudflare",
                event_type="certificate_expiry",
                entity_id=hostname,
                severity=severity,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "hostname": hostname,
                    "days_remaining": days_remaining,
                    "level": new_state,
                    "pack_type": pack.get("type", ""),
                    "status": pack.get("status", ""),
                },
                metadata=EventMetadata(
                    dedup_key=f"cloudflare:ssl:{hostname}",
                ),
            ))
        elif prev_state in ("warning", "critical"):
            # Certificate renewed
            self._enqueue(Event(
                source=self.name,
                system="cloudflare",
                event_type="certificate_renewed",
                entity_id=hostname,
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "hostname": hostname,
                    "days_remaining": days_remaining,
                },
                metadata=EventMetadata(
                    dedup_key=f"cloudflare:ssl:{hostname}",
                ),
            ))

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _enqueue(self, event: Event) -> None:
        """Enqueue an event, logging on failure."""
        try:
            self._queue.put_nowait(event)
        except Exception:
            logger.warning(
                "Cloudflare adapter: failed to enqueue event: %s/%s",
                event.system, event.event_type,
            )
