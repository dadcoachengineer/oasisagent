"""Servarr (Sonarr/Radarr/Prowlarr/Bazarr) polling ingestion adapter.

Polls the Servarr v3/v1 API for health issues and download queue problems.
A single adapter class handles all Servarr-family apps since they share
the same API pattern, differing only in API version and port.

Events emitted on state transitions:
- ``servarr_health_issue`` (WARNING) when a new health check problem appears
- ``servarr_queue_stuck`` (WARNING) when a download is stuck for >2 hours
- ``servarr_queue_failed`` (ERROR) when a download has failed
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import ServarrAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)

# Prowlarr and Bazarr use API v1; Sonarr and Radarr use v3
_API_VERSION: dict[str, str] = {
    "sonarr": "v3",
    "radarr": "v3",
    "prowlarr": "v1",
    "bazarr": "v1",
}

# Stuck threshold: 2 hours in seconds
_STUCK_THRESHOLD_SECONDS = 7200


class ServarrAdapter(IngestAdapter):
    """Polls Servarr apps (Sonarr, Radarr, Prowlarr, Bazarr) for health and queue issues.

    Uses state-based dedup so events are only emitted on transitions.
    """

    def __init__(
        self, config: ServarrAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers for dedup
        self._health_issues: set[str] = set()  # active health issue messages
        self._failed_ids: set[str] = set()  # download IDs in failed state
        self._stuck_ids: set[str] = set()  # download IDs flagged as stuck

    @property
    def name(self) -> str:
        return f"servarr_{self._config.app_type}"

    @property
    def _api_version(self) -> str:
        return _API_VERSION.get(self._config.app_type, "v3")

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._config.api_key}

    async def start(self) -> None:
        """Start the polling loop."""
        self._task = asyncio.create_task(
            self._poll_loop(),
            name=f"servarr-{self._config.app_type}-poller",
        )
        await self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self._config.timeout)
        backoff = self._config.poll_interval
        max_backoff = 300

        while not self._stopping:
            try:
                async with aiohttp.ClientSession(
                    headers=self._headers(), timeout=timeout,
                ) as session:
                    await self._poll_health(session)
                    await self._poll_queue(session)
                    self._connected = True
                    backoff = self._config.poll_interval  # reset on success
            except asyncio.CancelledError:
                return
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self._connected:
                    logger.warning(
                        "Servarr %s: connection error: %s",
                        self._config.app_type, exc,
                    )
                self._connected = False
                backoff = min(backoff * 2, max_backoff)
            except Exception:
                self._connected = False
                logger.exception(
                    "Servarr %s: unexpected error",
                    self._config.app_type,
                )
                backoff = min(backoff * 2, max_backoff)

            wait = self._config.poll_interval if self._connected else backoff
            for _ in range(wait):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Health endpoint
    # -----------------------------------------------------------------

    async def _poll_health(self, session: aiohttp.ClientSession) -> None:
        """Poll /api/vX/health for health check issues."""
        url = f"{self._config.url}/api/{self._api_version}/health"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data: list[dict[str, Any]] = await resp.json(content_type=None)

        current_issues: set[str] = set()
        for issue in data:
            msg = issue.get("message", "")
            issue_type = issue.get("type", "warning")
            source_name = issue.get("source", "")
            issue_key = f"{source_name}:{msg}"
            current_issues.add(issue_key)

            if issue_key not in self._health_issues:
                severity = Severity.ERROR if issue_type == "error" else Severity.WARNING
                self._enqueue(Event(
                    source=self.name,
                    system=self._config.app_type,
                    event_type="servarr_health_issue",
                    entity_id=source_name or self._config.app_type,
                    severity=severity,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "app_type": self._config.app_type,
                        "message": msg,
                        "type": issue_type,
                        "source": source_name,
                        "wiki_url": issue.get("wikiUrl", ""),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"servarr:{self._config.app_type}:health:{issue_key}",
                    ),
                ))

        self._health_issues = current_issues

    # -----------------------------------------------------------------
    # Queue endpoint
    # -----------------------------------------------------------------

    async def _poll_queue(self, session: aiohttp.ClientSession) -> None:
        """Poll /api/vX/queue for stuck or failed downloads."""
        url = f"{self._config.url}/api/{self._api_version}/queue"
        params: dict[str, str] = {"pageSize": "50", "includeUnknownSeriesItems": "true"}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json(content_type=None)

        records: list[dict[str, Any]] = data.get("records", [])
        now = datetime.now(tz=UTC)

        current_failed: set[str] = set()
        current_stuck: set[str] = set()

        for record in records:
            record_id = str(record.get("id", ""))
            if not record_id:
                continue

            status = record.get("status", "").lower()
            title = record.get("title", "unknown")

            # Check for failed downloads
            tracked_status = record.get("trackedDownloadStatus", "").lower()
            if tracked_status == "warning" or status == "failed":
                current_failed.add(record_id)
                if record_id not in self._failed_ids:
                    self._enqueue(Event(
                        source=self.name,
                        system=self._config.app_type,
                        event_type="servarr_queue_failed",
                        entity_id=title,
                        severity=Severity.ERROR,
                        timestamp=now,
                        payload={
                            "app_type": self._config.app_type,
                            "record_id": record_id,
                            "title": title,
                            "status": status,
                            "tracked_status": tracked_status,
                            "error_message": record.get("statusMessages", []),
                        },
                        metadata=EventMetadata(
                            dedup_key=f"servarr:{self._config.app_type}:queue_failed:{record_id}",
                        ),
                    ))
                continue

            # Check for stuck downloads (time in queue > threshold)
            added_str = record.get("added", "")
            if added_str and status == "downloading":
                try:
                    added = datetime.fromisoformat(
                        added_str.replace("Z", "+00:00"),
                    )
                    age_seconds = (now - added).total_seconds()
                    if age_seconds > _STUCK_THRESHOLD_SECONDS:
                        current_stuck.add(record_id)
                        if record_id not in self._stuck_ids:
                            self._enqueue(Event(
                                source=self.name,
                                system=self._config.app_type,
                                event_type="servarr_queue_stuck",
                                entity_id=title,
                                severity=Severity.WARNING,
                                timestamp=now,
                                payload={
                                    "app_type": self._config.app_type,
                                    "record_id": record_id,
                                    "title": title,
                                    "age_hours": round(age_seconds / 3600, 1),
                                    "status": status,
                                },
                                metadata=EventMetadata(
                                    dedup_key=f"servarr:{self._config.app_type}:queue_stuck:{record_id}",
                                ),
                            ))
                except (ValueError, TypeError):
                    pass

        self._failed_ids = current_failed
        self._stuck_ids = current_stuck

