"""Disk space usage scanner.

Proactively checks disk usage for configured filesystem paths. Emits events
on threshold transitions (ok -> warning -> critical) and recovery events
when usage drops below thresholds.

Uses ``shutil.disk_usage()`` — stdlib only, no external dependencies.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.models import Event, EventMetadata, Severity
from oasisagent.scanner.base import ScannerIngestAdapter

if TYPE_CHECKING:
    from oasisagent.config import DiskSpaceCheckConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class DiskSpaceScannerAdapter(ScannerIngestAdapter):
    """Scanner that checks disk usage for configured filesystem paths.

    State-based dedup: only emits events when the usage status for a path
    changes (ok -> warning, warning -> critical, critical -> ok, etc.).
    """

    def __init__(
        self,
        config: DiskSpaceCheckConfig,
        queue: EventQueue,
        interval: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(queue, interval, **kwargs)
        self._config = config
        # State tracking: path -> "ok" | "warning" | "critical"
        self._states: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "scanner.disk_space"

    async def _scan(self) -> list[Event]:
        """Check disk usage for all configured paths."""
        events: list[Event] = []
        for path in self._config.paths:
            try:
                usage = shutil.disk_usage(path)
                used_pct = (usage.used / usage.total) * 100
                new_events = self._evaluate(path, usage, used_pct)
                events.extend(new_events)
            except Exception as exc:
                logger.warning("Disk check failed for %s: %s", path, exc)
                events.extend(self._emit_check_error(path, exc))
        return events

    def _evaluate(
        self, path: str, usage: shutil.disk_usage_result, used_pct: float,
    ) -> list[Event]:
        """Evaluate usage against thresholds, emit on state change."""
        if used_pct >= self._config.critical_threshold_pct:
            new_state = "critical"
        elif used_pct >= self._config.warning_threshold_pct:
            new_state = "warning"
        else:
            new_state = "ok"

        old_state = self._states.get(path)
        self._states[path] = new_state

        if old_state == new_state:
            return []

        now = datetime.now(tz=UTC)
        total_gb = round(usage.total / (1024**3), 2)
        used_gb = round(usage.used / (1024**3), 2)
        free_gb = round(usage.free / (1024**3), 2)
        pct_rounded = round(used_pct, 1)

        payload = {
            "path": path,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "used_pct": pct_rounded,
        }

        # Recovery: warning/critical -> ok
        if new_state == "ok" and old_state in ("warning", "critical"):
            return [Event(
                source=self.name,
                system="host",
                event_type="disk_space_recovered",
                entity_id=path,
                severity=Severity.INFO,
                timestamp=now,
                payload={**payload, "previous_state": old_state},
                metadata=EventMetadata(
                    dedup_key=f"scanner.disk_space:{path}",
                ),
            )]

        # Threshold crossing
        if new_state == "warning":
            return [Event(
                source=self.name,
                system="host",
                event_type="disk_space_warning",
                entity_id=path,
                severity=Severity.WARNING,
                timestamp=now,
                payload={
                    **payload,
                    "threshold_pct": self._config.warning_threshold_pct,
                },
                metadata=EventMetadata(
                    dedup_key=f"scanner.disk_space:{path}",
                ),
            )]

        if new_state == "critical":
            return [Event(
                source=self.name,
                system="host",
                event_type="disk_space_critical",
                entity_id=path,
                severity=Severity.ERROR,
                timestamp=now,
                payload={
                    **payload,
                    "threshold_pct": self._config.critical_threshold_pct,
                },
                metadata=EventMetadata(
                    dedup_key=f"scanner.disk_space:{path}",
                ),
            )]

        # First scan with ok state — no event
        return []

    def _emit_check_error(self, path: str, exc: Exception) -> list[Event]:
        """Emit an error event when the disk check itself fails."""
        old_state = self._states.get(path)
        if old_state == "error":
            return []
        self._states[path] = "error"

        return [Event(
            source=self.name,
            system="host",
            event_type="disk_check_error",
            entity_id=path,
            severity=Severity.ERROR,
            timestamp=datetime.now(tz=UTC),
            payload={
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            metadata=EventMetadata(
                dedup_key=f"scanner.disk_space:{path}:error",
            ),
        )]
