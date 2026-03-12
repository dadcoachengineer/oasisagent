"""Docker container health scanner.

Proactively checks the health of Docker containers by listing all
containers and detecting unhealthy, restarting, or exited states.

Requires the Docker handler to be configured (uses its socket/URL for
API access). If the Docker handler is not enabled, the scanner is not
instantiated.

Uses the Docker Engine API ``GET /containers/json?all=true`` to list
all containers. State-based dedup per container name ensures events
only fire on transitions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.models import Event, EventMetadata, Severity
from oasisagent.scanner.base import ScannerIngestAdapter

if TYPE_CHECKING:
    from oasisagent.config import DockerHandlerConfig, DockerHealthCheckConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Container states that indicate a problem
_ALERT_STATES = frozenset({
    "unhealthy",
    "restarting",
    "exited",
    "dead",
})


class DockerHealthScannerAdapter(ScannerIngestAdapter):
    """Scanner that checks Docker container health via the Engine API.

    Calls ``GET /containers/json?all=true`` to list all containers and
    emits events when containers transition to unhealthy/exited states.
    """

    def __init__(
        self,
        config: DockerHealthCheckConfig,
        queue: EventQueue,
        interval: int,
        docker_config: DockerHandlerConfig,
    ) -> None:
        super().__init__(queue, interval)
        self._config = config
        self._docker_config = docker_config
        self._session: aiohttp.ClientSession | None = None
        # State tracking: container_name -> "ok" | alert_state
        self._states: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "scanner.docker_health"

    async def start(self) -> None:
        """Create HTTP session (Unix socket or TCP) then start poll loop."""
        connector: aiohttp.BaseConnector
        if self._docker_config.url:
            connector = aiohttp.TCPConnector(
                ssl=self._docker_config.tls_verify,
            )
            base_url = self._docker_config.url
        else:
            connector = aiohttp.UnixConnector(
                path=self._docker_config.socket.replace("unix://", ""),
            )
            base_url = "http://localhost"

        timeout = aiohttp.ClientTimeout(total=10)
        self._session = aiohttp.ClientSession(
            base_url=base_url,
            connector=connector,
            timeout=timeout,
        )
        await super().start()

    async def stop(self) -> None:
        """Close HTTP session and stop the poll loop."""
        await super().stop()
        if self._session:
            await self._session.close()
            self._session = None

    async def _scan(self) -> list[Event]:
        """Fetch container list and check for unhealthy states."""
        assert self._session is not None
        containers = await self._fetch_containers()
        return self._evaluate_containers(containers)

    async def _fetch_containers(self) -> list[dict[str, Any]]:
        """Fetch all containers from Docker Engine API."""
        assert self._session is not None
        async with self._session.get(
            "/containers/json", params={"all": "true"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()  # type: ignore[no-any-return]

    def _evaluate_containers(
        self, containers: list[dict[str, Any]],
    ) -> list[Event]:
        """Check each container's state and emit events on transitions."""
        events: list[Event] = []

        for container in containers:
            # Container names have a leading /
            names = container.get("Names", [])
            container_name = names[0].lstrip("/") if names else container.get("Id", "unknown")[:12]
            state = container.get("State", "unknown").lower()
            status = container.get("Status", "")

            # Check health status from the Status field (e.g., "Up 2 hours (unhealthy)")
            health_state = ""
            if "(unhealthy)" in status.lower():
                health_state = "unhealthy"
            elif "(healthy)" in status.lower():
                health_state = "healthy"

            # Determine effective state for alerting
            if health_state == "unhealthy":
                effective_state = "unhealthy"
            elif state in _ALERT_STATES:
                effective_state = state
            else:
                effective_state = "ok"

            # Apply ignore list
            if container_name in self._config.ignore_containers:
                continue

            old_state = self._states.get(container_name)
            self._states[container_name] = effective_state

            if old_state == effective_state:
                continue

            now = datetime.now(tz=UTC)

            # Transition to alert state
            if effective_state != "ok" and old_state != effective_state:
                is_fatal = effective_state in ("exited", "dead")
                severity = Severity.ERROR if is_fatal else Severity.WARNING
                events.append(Event(
                    source=self.name,
                    system="docker",
                    event_type=f"container_{effective_state}",
                    entity_id=container_name,
                    severity=severity,
                    timestamp=now,
                    payload={
                        "container_name": container_name,
                        "state": state,
                        "status": status,
                        "health_state": health_state,
                        "image": container.get("Image", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"scanner.docker_health:{container_name}",
                    ),
                ))

            # Recovery: alert -> ok
            elif effective_state == "ok" and old_state is not None and old_state != "ok":
                events.append(Event(
                    source=self.name,
                    system="docker",
                    event_type="container_recovered",
                    entity_id=container_name,
                    severity=Severity.INFO,
                    timestamp=now,
                    payload={
                        "container_name": container_name,
                        "state": state,
                        "status": status,
                        "previous_state": old_state,
                        "image": container.get("Image", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"scanner.docker_health:{container_name}",
                    ),
                ))

        return events
