"""Backup freshness scanner.

Checks backup sources (Proxmox Backup Server API and local file timestamps)
to detect stale backups. Emits events on state transitions: fresh -> stale,
stale -> fresh, and on check errors.

Supports two source types:
- **pbs**: Queries ``GET /api2/json/admin/datastore/{store}/snapshots``
  on a Proxmox Backup Server instance.
- **file**: Checks newest file matching a glob pattern against max_age_hours.

Backblaze B2 support is deferred to a future PR.
"""

from __future__ import annotations

import logging
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from oasisagent.models import Event, EventMetadata, Severity
from oasisagent.scanner.base import ScannerIngestAdapter

if TYPE_CHECKING:
    from oasisagent.config import BackupFreshnessCheckConfig, BackupSourceConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class _PbsChecker:
    """Checks backup freshness against a Proxmox Backup Server API."""

    def __init__(self, source: BackupSourceConfig) -> None:
        self._url = source.url.rstrip("/")
        self._token_id = source.token_id
        self._token_secret = source.token_secret
        self._datastore = source.datastore
        self._verify_ssl = source.verify_ssl

    async def newest_backup_age_hours(self) -> float:
        """Return hours since the newest backup in the datastore.

        Raises on API errors so the caller can emit check_error events.
        """
        ssl_ctx: ssl.SSLContext | bool = (
            ssl.create_default_context() if self._verify_ssl else False
        )
        url = (
            f"{self._url}/api2/json/admin/datastore"
            f"/{self._datastore}/snapshots"
        )
        headers = {
            "Authorization": (
                f"PBSAPIToken={self._token_id}:{self._token_secret}"
            ),
        }
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, headers=headers, ssl=ssl_ctx) as resp,
        ):
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json()

        snapshots = data.get("data", [])
        if not snapshots:
            msg = f"No snapshots found in datastore '{self._datastore}'"
            raise ValueError(msg)

        newest_epoch = max(s["backup-time"] for s in snapshots)
        age_seconds = time.time() - newest_epoch
        return age_seconds / 3600.0


class _FileChecker:
    """Checks backup freshness by file modification time."""

    def __init__(self, source: BackupSourceConfig) -> None:
        self._pattern = source.path

    def newest_backup_age_hours(self) -> float:
        """Return hours since the newest file matching the glob pattern.

        Raises FileNotFoundError if no files match.
        """
        p = Path(self._pattern)
        parent = p.parent
        pattern = p.name
        matches = list(parent.glob(pattern))
        if not matches:
            msg = f"No files match pattern '{self._pattern}'"
            raise FileNotFoundError(msg)

        newest_mtime = max(f.stat().st_mtime for f in matches)
        age_seconds = time.time() - newest_mtime
        return age_seconds / 3600.0


class BackupFreshnessScannerAdapter(ScannerIngestAdapter):
    """Scanner that checks backup freshness across configured sources.

    State-based dedup: only emits events on transitions (fresh -> stale,
    stale -> fresh). API errors do not clear stale state.
    """

    def __init__(
        self,
        config: BackupFreshnessCheckConfig,
        queue: EventQueue,
        interval: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(queue, interval, **kwargs)
        self._config = config
        self._stale_sources: set[str] = set()
        self._error_sources: set[str] = set()

    @property
    def name(self) -> str:
        return "scanner.backup_freshness"

    async def _scan(self) -> list[Event]:
        """Check all backup sources and return events for state transitions."""
        events: list[Event] = []
        for source in self._config.sources:
            try:
                age_hours = await self._check_source(source)
                events.extend(self._evaluate(source, age_hours))
                self._error_sources.discard(source.name)
            except Exception as exc:
                logger.warning(
                    "Backup check failed for %s: %s", source.name, exc,
                )
                events.extend(self._emit_check_error(source, exc))
        return events

    async def _check_source(self, source: BackupSourceConfig) -> float:
        """Return hours since newest backup for the given source."""
        if source.type == "pbs":
            checker = _PbsChecker(source)
            return await checker.newest_backup_age_hours()
        # file
        checker_file = _FileChecker(source)
        return checker_file.newest_backup_age_hours()

    def _evaluate(
        self, source: BackupSourceConfig, age_hours: float,
    ) -> list[Event]:
        """Evaluate age against threshold, emit on state change."""
        is_stale = age_hours > source.max_age_hours
        was_stale = source.name in self._stale_sources
        now = datetime.now(tz=UTC)

        if is_stale and not was_stale:
            self._stale_sources.add(source.name)
            return [Event(
                source=self.name,
                system="backup",
                event_type="backup_stale",
                entity_id=source.name,
                severity=Severity.WARNING,
                timestamp=now,
                payload={
                    "source_type": source.type,
                    "age_hours": round(age_hours, 1),
                    "max_age_hours": source.max_age_hours,
                },
                metadata=EventMetadata(
                    dedup_key=(
                        f"scanner.backup_freshness:{source.name}"
                    ),
                ),
            )]

        if not is_stale and was_stale:
            self._stale_sources.discard(source.name)
            return [Event(
                source=self.name,
                system="backup",
                event_type="backup_recovered",
                entity_id=source.name,
                severity=Severity.INFO,
                timestamp=now,
                payload={
                    "source_type": source.type,
                    "age_hours": round(age_hours, 1),
                    "max_age_hours": source.max_age_hours,
                },
                metadata=EventMetadata(
                    dedup_key=(
                        f"scanner.backup_freshness:{source.name}"
                    ),
                ),
            )]

        return []

    def _emit_check_error(
        self, source: BackupSourceConfig, exc: Exception,
    ) -> list[Event]:
        """Emit an error event when the backup check itself fails.

        API failures must NOT clear stale state (D4).
        """
        if source.name in self._error_sources:
            return []
        self._error_sources.add(source.name)

        return [Event(
            source=self.name,
            system="backup",
            event_type="backup_check_error",
            entity_id=source.name,
            severity=Severity.ERROR,
            timestamp=datetime.now(tz=UTC),
            payload={
                "source_type": source.type,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            metadata=EventMetadata(
                dedup_key=(
                    f"scanner.backup_freshness:{source.name}:error"
                ),
            ),
        )]
