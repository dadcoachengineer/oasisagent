"""Frigate NVR polling ingestion adapter.

Polls the Frigate API for camera health (FPS stats) and optionally
for detection event spikes. Emits events on state transitions.

Two polling paths:
- Camera stats: state-based dedup per camera (FPS → online/offline)
- Detection events: count-based spike detection per camera per poll
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oasisagent.clients.frigate import FrigateClient
from oasisagent.ingestion.base import IngestAdapter
from oasisagent.models import Event, EventMetadata, Severity

if TYPE_CHECKING:
    from oasisagent.config import FrigateAdapterConfig
    from oasisagent.engine.queue import EventQueue

logger = logging.getLogger(__name__)


class FrigateAdapter(IngestAdapter):
    """Polls Frigate NVR for camera health and detection event spikes.

    State-based dedup for camera online/offline transitions. Optional
    detection event spike alerting (disabled by default).
    """

    def __init__(
        self, config: FrigateAdapterConfig, queue: EventQueue,
    ) -> None:
        super().__init__(queue)
        self._config = config
        self._client = FrigateClient(
            url=config.url,
            timeout=config.timeout,
        )
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connected = False

        # State trackers
        self._camera_states: dict[str, bool] = {}  # camera_name → is_online
        self._detector_states: dict[str, bool] = {}  # detector_name → is_slow
        self._last_event_poll: float = 0.0

    @property
    def name(self) -> str:
        return "frigate"

    async def start(self) -> None:
        """Connect to Frigate API and start polling loop."""
        backoff = 5
        max_backoff = 300
        while not self._stopping:
            try:
                await self._client.start()
                self._connected = True
                break
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    "Frigate adapter: connection failed: %s "
                    "(retrying in %ds)", exc, backoff,
                )
                self._connected = False
                for _ in range(backoff):
                    if self._stopping:
                        return
                    await asyncio.sleep(1)
                backoff = min(backoff * 2, max_backoff)

        self._task = asyncio.create_task(
            self._poll_loop(), name="frigate-poller",
        )
        await self._task

    async def stop(self) -> None:
        """Stop polling and close the client."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self._client.close()
        self._connected = False

    async def healthy(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------
    # Poll loop
    # -----------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main polling loop — polls stats and optionally events."""
        while not self._stopping:
            try:
                await self._poll_stats()

                if self._config.poll_events:
                    await self._poll_events()

                self._connected = True
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Frigate poll error: %s", exc)
                self._connected = False

            for _ in range(self._config.poll_interval):
                if self._stopping:
                    return
                await asyncio.sleep(1)

    # -----------------------------------------------------------------
    # Stats polling (camera health + detector performance)
    # -----------------------------------------------------------------

    async def _poll_stats(self) -> None:
        """Poll /api/stats for camera FPS and detector performance."""
        stats = await self._client.get_stats()

        self._check_cameras(stats)
        self._check_detectors(stats)

    def _check_cameras(self, stats: dict[str, Any]) -> None:
        """Check each camera's FPS for online/offline transitions."""
        current_cameras: set[str] = set()

        for camera_name, camera_data in stats.items():
            # Skip non-camera top-level keys
            if not isinstance(camera_data, dict):
                continue
            if "camera_fps" not in camera_data:
                continue

            current_cameras.add(camera_name)
            camera_fps = camera_data.get("camera_fps", 0)
            is_online = camera_fps > 0

            prev_online = self._camera_states.get(camera_name)
            self._camera_states[camera_name] = is_online

            if prev_online is None:
                # First poll — only emit if offline
                if not is_online:
                    self._enqueue(Event(
                        source=self.name,
                        system="frigate",
                        event_type="frigate_camera_offline",
                        entity_id=camera_name,
                        severity=Severity.ERROR,
                        timestamp=datetime.now(tz=UTC),
                        payload={
                            "camera": camera_name,
                            "camera_fps": camera_fps,
                        },
                        metadata=EventMetadata(
                            dedup_key=f"frigate:camera:{camera_name}:status",
                        ),
                    ))
            elif prev_online and not is_online:
                # Was online, now offline
                self._enqueue(Event(
                    source=self.name,
                    system="frigate",
                    event_type="frigate_camera_offline",
                    entity_id=camera_name,
                    severity=Severity.ERROR,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "camera": camera_name,
                        "camera_fps": camera_fps,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"frigate:camera:{camera_name}:status",
                    ),
                ))
            elif not prev_online and is_online:
                # Was offline, now recovered
                self._enqueue(Event(
                    source=self.name,
                    system="frigate",
                    event_type="frigate_camera_recovered",
                    entity_id=camera_name,
                    severity=Severity.INFO,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "camera": camera_name,
                        "camera_fps": camera_fps,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"frigate:camera:{camera_name}:status",
                    ),
                ))

        # Evict cameras no longer in stats to prevent unbounded growth
        self._camera_states = {
            k: v for k, v in self._camera_states.items()
            if k in current_cameras
        }

    def _check_detectors(self, stats: dict[str, Any]) -> None:
        """Check detector inference FPS against threshold."""
        detectors = stats.get("detectors", {})
        if not isinstance(detectors, dict):
            return

        for det_name, det_data in detectors.items():
            if not isinstance(det_data, dict):
                continue

            inference_speed = det_data.get("inference_speed", 0)
            # inference_speed is in ms — convert to FPS (0 = not running)
            det_fps = 1000.0 / inference_speed if inference_speed > 0 else 0.0

            is_slow = det_fps < self._config.detector_fps_threshold
            was_slow = self._detector_states.get(det_name, False)
            self._detector_states[det_name] = is_slow

            if is_slow and not was_slow:
                self._enqueue(Event(
                    source=self.name,
                    system="frigate",
                    event_type="frigate_detector_slow",
                    entity_id=det_name,
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "detector": det_name,
                        "inference_speed_ms": inference_speed,
                        "detector_fps": round(det_fps, 2),
                        "threshold_fps": self._config.detector_fps_threshold,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"frigate:detector:{det_name}:slow",
                    ),
                ))
            elif was_slow and not is_slow:
                self._enqueue(Event(
                    source=self.name,
                    system="frigate",
                    event_type="frigate_detector_recovered",
                    entity_id=det_name,
                    severity=Severity.INFO,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "detector": det_name,
                        "inference_speed_ms": inference_speed,
                        "detector_fps": round(det_fps, 2),
                        "threshold_fps": self._config.detector_fps_threshold,
                    },
                    metadata=EventMetadata(
                        dedup_key=f"frigate:detector:{det_name}:slow",
                    ),
                ))

    # -----------------------------------------------------------------
    # Event polling (detection spike detection)
    # -----------------------------------------------------------------

    async def _poll_events(self) -> None:
        """Poll /api/events for detection count spikes."""
        now = time.time()
        if self._last_event_poll == 0.0:
            # First poll — set baseline, don't alert
            self._last_event_poll = now
            return

        events = await self._client.get_events(
            after=self._last_event_poll, limit=50,
        )
        self._last_event_poll = now

        if not events:
            return

        # Count detections per camera
        camera_counts: dict[str, int] = defaultdict(int)
        for ev in events:
            camera = ev.get("camera", "unknown")
            camera_counts[camera] += 1

        for camera, count in camera_counts.items():
            if count >= self._config.detection_spike_threshold:
                # Collect the labels detected
                labels: set[str] = set()
                for ev in events:
                    if ev.get("camera") == camera:
                        label = ev.get("label", "")
                        if label:
                            labels.add(label)

                self._enqueue(Event(
                    source=self.name,
                    system="frigate",
                    event_type="frigate_detection_spike",
                    entity_id=camera,
                    severity=Severity.WARNING,
                    timestamp=datetime.now(tz=UTC),
                    payload={
                        "camera": camera,
                        "detection_count": count,
                        "threshold": self._config.detection_spike_threshold,
                        "labels": sorted(labels),
                    },
                    metadata=EventMetadata(
                        dedup_key=f"frigate:events:{camera}:spike",
                    ),
                ))

