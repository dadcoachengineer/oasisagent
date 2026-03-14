"""Nginx Proxy Manager polling ingestion adapter.

Polls the NPM API for proxy host status, certificate expiry, and dead
hosts. Emits events on state transitions or threshold crossings.

Three separate polling paths:
- Proxy hosts: state-based dedup (host_id -> enabled + forwarding status)
- Certificates: threshold-based (days until expiry)
- Dead hosts: point-in-time dedup (set of dead host IDs)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.clients.npm import NpmClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import NpmAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class NpmAdapter(IngestAdapter):
    """Polls Nginx Proxy Manager for proxy host health, certs, and dead hosts.

    Per-endpoint health isolation ensures one failing endpoint does not
    mark the entire adapter as unhealthy.
    """

    def __init__(
        self, config: NpmAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._client = NpmClient(
            url=config.url,
            email=config.email,
            password=config.password,
            timeout=config.timeout,
        )
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

        # Per-endpoint health tracking
        self._endpoint_health: dict[str, bool] = {}
        if self._config.poll_proxy_hosts:
            self._endpoint_health["proxy_hosts"] = False
        if self._config.poll_certificates:
            self._endpoint_health["certificates"] = False
        if self._config.poll_dead_hosts:
            self._endpoint_health["dead_hosts"] = False

        # Dedup trackers
        # host_id -> "online" | "disabled"
        self._host_states: dict[int, str] = {}
        # cert_id -> "ok" | "warning" | "critical"
        self._cert_alert_state: dict[int, str] = {}
        # set of dead host IDs currently tracked
        self._seen_dead_hosts: set[int] = set()

    @property
    def name(self) -> str:
        return "npm"

    async def start(self) -> None:
        """Connect to NPM API and start the polling loop.

        Uses exponential backoff on connection failure (5s -> 300s max).
        """
        backoff = 5
        max_backoff = 300
        while not self._stopping:
            try:
                await self._client.connect()
                break
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    "NPM adapter: connection failed: %s "
                    "(retrying in %ds)", exc, backoff,
                )
                for _ in range(backoff):
                    if self._stopping:
                        return
                    await asyncio.sleep(1)
                backoff = min(backoff * 2, max_backoff)

        self._task = asyncio.create_task(
            self._poll_loop(), name="npm-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
        await self._client.close()
        for key in self._endpoint_health:
            self._endpoint_health[key] = False

    async def healthy(self) -> bool:
        if not self._endpoint_health:
            return False
        return any(self._endpoint_health.values())

    def health_detail(self) -> dict[str, str]:
        """Per-endpoint health for dashboard display."""
        return {
            endpoint: ("connected" if ok else "disconnected")
            for endpoint, ok in self._endpoint_health.items()
        }

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main polling loop -- polls all enabled endpoints each interval."""
        while not self._stopping:
            if self._config.poll_proxy_hosts:
                try:
                    await self._poll_proxy_hosts()
                    self._endpoint_health["proxy_hosts"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("NPM proxy hosts poll error: %s", exc)
                    self._endpoint_health["proxy_hosts"] = False

            if self._config.poll_certificates:
                try:
                    await self._poll_certificates()
                    self._endpoint_health["certificates"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("NPM certificates poll error: %s", exc)
                    self._endpoint_health["certificates"] = False

            if self._config.poll_dead_hosts:
                try:
                    await self._poll_dead_hosts()
                    self._endpoint_health["dead_hosts"] = True
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("NPM dead hosts poll error: %s", exc)
                    self._endpoint_health["dead_hosts"] = False

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Proxy host polling
    # -----------------------------------------------------------------

    async def _poll_proxy_hosts(self) -> None:
        """Poll proxy host status for state transitions."""
        data = await self._client.get("/api/nginx/proxy-hosts")
        hosts: list[dict[str, Any]] = data if isinstance(data, list) else []

        for host in hosts:
            host_id = host.get("id")
            if host_id is None:
                continue

            enabled = host.get("enabled", 1)
            # NPM sets forward_host/forward_port; online_status is not a
            # native field, but enabled=0 means disabled, and error fields
            # or certificate_id=0 may indicate issues.
            domain_names = host.get("domain_names", [])
            entity = domain_names[0] if domain_names else str(host_id)

            # Determine current status
            current = "disabled" if not enabled else "online"

            prev = self._host_states.get(host_id)
            self._host_states[host_id] = current

            if prev is not None and prev != current:
                if current == "disabled":
                    self._enqueue(Event(
                        source=self.name,
                        system="npm",
                        event_type="npm_proxy_host_error",
                        entity_id=entity,
                        severity=Severity.WARNING,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "host_id": host_id,
                            "domain_names": domain_names,
                            "status": current,
                            "previous_status": prev,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"npm:proxy_host:{host_id}:status",
                        ),
                    ))
                elif prev != "online":
                    self._enqueue(Event(
                        source=self.name,
                        system="npm",
                        event_type="npm_proxy_host_recovered",
                        entity_id=entity,
                        severity=Severity.INFO,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "host_id": host_id,
                            "domain_names": domain_names,
                            "status": current,
                            "previous_status": prev,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"npm:proxy_host:{host_id}:status",
                        ),
                    ))
            elif prev is None and current != "online":
                # First poll, host already in error state
                self._enqueue(Event(
                    source=self.name,
                    system="npm",
                    event_type="npm_proxy_host_error",
                    entity_id=entity,
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "host_id": host_id,
                        "domain_names": domain_names,
                        "status": current,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"npm:proxy_host:{host_id}:status",
                    ),
                ))

        # Evict removed hosts to prevent unbounded growth
        current_ids = {h.get("id") for h in hosts} - {None}
        self._host_states = {
            k: v for k, v in self._host_states.items() if k in current_ids
        }

    # -----------------------------------------------------------------
    # Certificate polling
    # -----------------------------------------------------------------

    async def _poll_certificates(self) -> None:
        """Poll certificates for expiry threshold crossings."""
        data = await self._client.get("/api/nginx/certificates")
        certs: list[dict[str, Any]] = data if isinstance(data, list) else []

        for cert in certs:
            cert_id = cert.get("id")
            if cert_id is None:
                continue

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
            domain_names = cert.get("domain_names", [])
            entity = domain_names[0] if domain_names else str(cert_id)
            nice_name = cert.get("nice_name", entity)

            self._check_cert_expiry(cert_id, entity, nice_name, days_remaining, domain_names)

    def _check_cert_expiry(
        self,
        cert_id: int,
        entity: str,
        nice_name: str,
        days_remaining: int,
        domain_names: list[str],
    ) -> None:
        """Emit events on certificate expiry threshold transitions."""
        if days_remaining <= self._config.cert_critical_days:
            new_state = "critical"
            severity = Severity.ERROR
        elif days_remaining <= self._config.cert_warning_days:
            new_state = "warning"
            severity = Severity.WARNING
        else:
            new_state = "ok"
            severity = Severity.INFO  # only used if recovering

        prev_state = self._cert_alert_state.get(cert_id, "ok")
        self._cert_alert_state[cert_id] = new_state

        if new_state == prev_state:
            return

        if new_state in ("warning", "critical"):
            self._enqueue(Event(
                source=self.name,
                system="npm",
                event_type="npm_certificate_expiring",
                entity_id=entity,
                severity=severity,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "cert_id": cert_id,
                    "nice_name": nice_name,
                    "domain_names": domain_names,
                    "days_remaining": days_remaining,
                    "level": new_state,
                },
                metadata=EventMetadata(
                    dedup_key=f"npm:cert:{cert_id}:expiry",
                ),
            ))
        elif prev_state in ("warning", "critical"):
            # Certificate renewed
            self._enqueue(Event(
                source=self.name,
                system="npm",
                event_type="npm_certificate_renewed",
                entity_id=entity,
                severity=Severity.INFO,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "cert_id": cert_id,
                    "nice_name": nice_name,
                    "domain_names": domain_names,
                    "days_remaining": days_remaining,
                },
                metadata=EventMetadata(
                    dedup_key=f"npm:cert:{cert_id}:expiry",
                ),
            ))

    # -----------------------------------------------------------------
    # Dead host polling
    # -----------------------------------------------------------------

    async def _poll_dead_hosts(self) -> None:
        """Poll dead hosts for new entries."""
        data = await self._client.get("/api/nginx/dead-hosts")
        dead_hosts: list[dict[str, Any]] = data if isinstance(data, list) else []

        current_dead_ids: set[int] = set()

        for host in dead_hosts:
            host_id = host.get("id")
            if host_id is None:
                continue

            current_dead_ids.add(host_id)

            if host_id in self._seen_dead_hosts:
                continue

            domain_names = host.get("domain_names", [])
            entity = domain_names[0] if domain_names else str(host_id)

            self._enqueue(Event(
                source=self.name,
                system="npm",
                event_type="npm_dead_host_detected",
                entity_id=entity,
                severity=Severity.WARNING,
                timestamp=datetime.now(tz=UTC),
                payload={
                    "host_id": host_id,
                    "domain_names": domain_names,
                },
                metadata=EventMetadata(
                    dedup_key=f"npm:dead_host:{host_id}",
                ),
            ))

        # Evict cleared dead hosts to prevent unbounded growth
        self._seen_dead_hosts = current_dead_ids

