"""Tests for the Frigate NVR ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from oasisagent.config import FrigateAdapterConfig
from oasisagent.ingestion.frigate import FrigateAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> FrigateAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://frigate:8971",
        "poll_interval": 60,
        "poll_events": False,
        "detector_fps_threshold": 5.0,
        "detection_spike_threshold": 20,
        "timeout": 10,
    }
    defaults.update(overrides)
    return FrigateAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(
    **overrides: object,
) -> tuple[FrigateAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    with patch("oasisagent.ingestion.frigate.FrigateClient"):
        adapter = FrigateAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = FrigateAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 60
        assert config.poll_events is False
        assert config.detector_fps_threshold == 5.0
        assert config.detection_spike_threshold == 20
        assert config.timeout == 10

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "frigate"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        adapter._client = AsyncMock()
        await adapter.stop()
        assert adapter._stopping is True
        assert adapter._connected is False


# ---------------------------------------------------------------------------
# Camera stats polling
# ---------------------------------------------------------------------------


class TestCameraPolling:
    @pytest.mark.asyncio
    async def test_camera_offline_event(self) -> None:
        """Camera was online, FPS drops to 0 -> emit offline event."""
        adapter, queue = _make_adapter()
        adapter._camera_states["front_door"] = True  # was online

        adapter._client.get_stats = AsyncMock(return_value={
            "front_door": {
                "camera_fps": 0,
                "process_fps": 0,
            },
            "detectors": {},
        })

        await adapter._poll_stats()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "frigate_camera_offline"
        assert event.severity == Severity.ERROR
        assert event.entity_id == "front_door"
        assert event.payload["camera_fps"] == 0

    @pytest.mark.asyncio
    async def test_camera_recovery_event(self) -> None:
        """Camera was offline, FPS restored -> emit recovery event."""
        adapter, queue = _make_adapter()
        adapter._camera_states["front_door"] = False  # was offline

        adapter._client.get_stats = AsyncMock(return_value={
            "front_door": {
                "camera_fps": 15.0,
                "process_fps": 10.0,
            },
            "detectors": {},
        })

        await adapter._poll_stats()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "frigate_camera_recovered"
        assert event.severity == Severity.INFO
        assert event.entity_id == "front_door"
        assert event.payload["camera_fps"] == 15.0

    @pytest.mark.asyncio
    async def test_camera_same_state_no_event(self) -> None:
        """Camera stays online -> no event."""
        adapter, queue = _make_adapter()
        adapter._camera_states["front_door"] = True

        adapter._client.get_stats = AsyncMock(return_value={
            "front_door": {
                "camera_fps": 15.0,
            },
            "detectors": {},
        })

        await adapter._poll_stats()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_online_no_event(self) -> None:
        """First poll with healthy camera -> no event."""
        adapter, queue = _make_adapter()

        adapter._client.get_stats = AsyncMock(return_value={
            "front_door": {
                "camera_fps": 15.0,
            },
            "detectors": {},
        })

        await adapter._poll_stats()
        queue.put_nowait.assert_not_called()
        assert adapter._camera_states["front_door"] is True

    @pytest.mark.asyncio
    async def test_first_poll_offline_emits(self) -> None:
        """First poll with offline camera -> emit event."""
        adapter, queue = _make_adapter()

        adapter._client.get_stats = AsyncMock(return_value={
            "front_door": {
                "camera_fps": 0,
            },
            "detectors": {},
        })

        await adapter._poll_stats()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "frigate_camera_offline"

    @pytest.mark.asyncio
    async def test_camera_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._camera_states["backyard"] = True

        adapter._client.get_stats = AsyncMock(return_value={
            "backyard": {"camera_fps": 0},
            "detectors": {},
        })

        await adapter._poll_stats()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "frigate:camera:backyard:status"

    @pytest.mark.asyncio
    async def test_multiple_cameras_independent(self) -> None:
        """Each camera tracked independently."""
        adapter, queue = _make_adapter()
        adapter._camera_states["cam_a"] = True
        adapter._camera_states["cam_b"] = True

        adapter._client.get_stats = AsyncMock(return_value={
            "cam_a": {"camera_fps": 0},      # offline
            "cam_b": {"camera_fps": 15.0},    # still online
            "detectors": {},
        })

        await adapter._poll_stats()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "cam_a"

    @pytest.mark.asyncio
    async def test_non_camera_keys_skipped(self) -> None:
        """Top-level keys without camera_fps are skipped."""
        adapter, queue = _make_adapter()

        adapter._client.get_stats = AsyncMock(return_value={
            "service": {"uptime": 12345},
            "cpu_usages": {"1": 50.0},
            "detectors": {"coral": {"inference_speed": 10.0}},
            "front_door": {"camera_fps": 15.0},
        })

        await adapter._poll_stats()
        # Only front_door is a camera — no events on first poll (healthy)
        queue.put_nowait.assert_not_called()
        assert "front_door" in adapter._camera_states
        assert "service" not in adapter._camera_states
        assert "cpu_usages" not in adapter._camera_states

    @pytest.mark.asyncio
    async def test_deleted_camera_evicted(self) -> None:
        """Cameras removed from stats are evicted from tracker."""
        adapter, _ = _make_adapter()
        adapter._camera_states = {"cam_a": True, "cam_b": True}

        adapter._client.get_stats = AsyncMock(return_value={
            "cam_a": {"camera_fps": 15.0},
            "detectors": {},
        })

        await adapter._poll_stats()

        assert "cam_a" in adapter._camera_states
        assert "cam_b" not in adapter._camera_states

    @pytest.mark.asyncio
    async def test_event_source_and_system(self) -> None:
        adapter, queue = _make_adapter()

        adapter._client.get_stats = AsyncMock(return_value={
            "front_door": {"camera_fps": 0},
            "detectors": {},
        })

        await adapter._poll_stats()

        event = queue.put_nowait.call_args[0][0]
        assert event.source == "frigate"
        assert event.system == "frigate"


# ---------------------------------------------------------------------------
# Detector stats polling
# ---------------------------------------------------------------------------


class TestDetectorPolling:
    @pytest.mark.asyncio
    async def test_detector_slow_event(self) -> None:
        """Detector inference speed too slow -> emit warning."""
        adapter, queue = _make_adapter(detector_fps_threshold=5.0)

        adapter._client.get_stats = AsyncMock(return_value={
            "detectors": {
                "coral": {
                    "inference_speed": 500.0,  # 2 FPS, below 5.0 threshold
                    "pid": 123,
                },
            },
        })

        await adapter._poll_stats()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "frigate_detector_slow"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "coral"
        assert event.payload["inference_speed_ms"] == 500.0
        assert event.payload["detector_fps"] == 2.0

    @pytest.mark.asyncio
    async def test_detector_fast_no_event(self) -> None:
        """Detector inference speed above threshold -> no event."""
        adapter, queue = _make_adapter(detector_fps_threshold=5.0)

        adapter._client.get_stats = AsyncMock(return_value={
            "detectors": {
                "coral": {
                    "inference_speed": 10.0,  # 100 FPS
                },
            },
        })

        await adapter._poll_stats()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_detector_recovery_event(self) -> None:
        """Detector recovers from slow state -> emit recovery."""
        adapter, queue = _make_adapter(detector_fps_threshold=5.0)
        adapter._detector_states["coral"] = True  # was slow

        adapter._client.get_stats = AsyncMock(return_value={
            "detectors": {
                "coral": {
                    "inference_speed": 10.0,  # 100 FPS — recovered
                },
            },
        })

        await adapter._poll_stats()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "frigate_detector_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_detector_same_slow_state_dedup(self) -> None:
        """Detector stays slow -> no duplicate event."""
        adapter, queue = _make_adapter(detector_fps_threshold=5.0)
        adapter._detector_states["coral"] = True

        adapter._client.get_stats = AsyncMock(return_value={
            "detectors": {
                "coral": {"inference_speed": 500.0},
            },
        })

        await adapter._poll_stats()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_detector_dedup_key(self) -> None:
        adapter, queue = _make_adapter(detector_fps_threshold=5.0)

        adapter._client.get_stats = AsyncMock(return_value={
            "detectors": {
                "coral": {"inference_speed": 500.0},
            },
        })

        await adapter._poll_stats()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "frigate:detector:coral:slow"

    @pytest.mark.asyncio
    async def test_detector_zero_speed(self) -> None:
        """Zero inference speed means detector not running -> slow."""
        adapter, queue = _make_adapter(detector_fps_threshold=5.0)

        adapter._client.get_stats = AsyncMock(return_value={
            "detectors": {
                "coral": {"inference_speed": 0},
            },
        })

        await adapter._poll_stats()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "frigate_detector_slow"

    @pytest.mark.asyncio
    async def test_no_detectors_key_no_error(self) -> None:
        """Stats without detectors key should not error."""
        adapter, queue = _make_adapter()

        adapter._client.get_stats = AsyncMock(return_value={
            "front_door": {"camera_fps": 15.0},
        })

        await adapter._poll_stats()
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Event polling (detection spikes)
# ---------------------------------------------------------------------------


class TestEventPolling:
    @pytest.mark.asyncio
    async def test_first_poll_sets_baseline(self) -> None:
        """First event poll sets timestamp, doesn't alert."""
        adapter, queue = _make_adapter(poll_events=True)
        assert adapter._last_event_poll == 0.0

        await adapter._poll_events()

        assert adapter._last_event_poll > 0.0
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_spike_event(self) -> None:
        """Detection count above threshold -> emit spike event."""
        adapter, queue = _make_adapter(
            poll_events=True, detection_spike_threshold=3,
        )
        adapter._last_event_poll = 1000.0

        events = [
            {"camera": "front_door", "label": "person"},
            {"camera": "front_door", "label": "person"},
            {"camera": "front_door", "label": "car"},
        ]
        adapter._client.get_events = AsyncMock(return_value=events)

        await adapter._poll_events()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "frigate_detection_spike"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "front_door"
        assert event.payload["detection_count"] == 3
        assert set(event.payload["labels"]) == {"person", "car"}

    @pytest.mark.asyncio
    async def test_below_threshold_no_event(self) -> None:
        """Detection count below threshold -> no event."""
        adapter, queue = _make_adapter(
            poll_events=True, detection_spike_threshold=10,
        )
        adapter._last_event_poll = 1000.0

        events = [
            {"camera": "front_door", "label": "person"},
            {"camera": "front_door", "label": "car"},
        ]
        adapter._client.get_events = AsyncMock(return_value=events)

        await adapter._poll_events()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_spike_per_camera(self) -> None:
        """Spike is tracked per camera — only cameras above threshold alert."""
        adapter, queue = _make_adapter(
            poll_events=True, detection_spike_threshold=3,
        )
        adapter._last_event_poll = 1000.0

        events = [
            {"camera": "front_door", "label": "person"},
            {"camera": "front_door", "label": "person"},
            {"camera": "front_door", "label": "person"},
            {"camera": "backyard", "label": "cat"},
        ]
        adapter._client.get_events = AsyncMock(return_value=events)

        await adapter._poll_events()

        # Only front_door hit threshold
        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.entity_id == "front_door"

    @pytest.mark.asyncio
    async def test_empty_events_no_alert(self) -> None:
        """Empty event list -> no alert."""
        adapter, queue = _make_adapter(poll_events=True)
        adapter._last_event_poll = 1000.0

        adapter._client.get_events = AsyncMock(return_value=[])

        await adapter._poll_events()
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_spike_dedup_key(self) -> None:
        adapter, queue = _make_adapter(
            poll_events=True, detection_spike_threshold=1,
        )
        adapter._last_event_poll = 1000.0

        adapter._client.get_events = AsyncMock(return_value=[
            {"camera": "garage", "label": "person"},
        ])

        await adapter._poll_events()

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "frigate:events:garage:spike"


# ---------------------------------------------------------------------------
# Poll loop (events disabled by default)
# ---------------------------------------------------------------------------


class TestPollLoop:
    @pytest.mark.asyncio
    async def test_events_not_polled_by_default(self) -> None:
        """When poll_events=False, _poll_events should not be called."""
        adapter, _ = _make_adapter(poll_events=False)

        poll_count = 0
        original_get_stats = AsyncMock(return_value={"detectors": {}})

        async def _get_stats_then_stop() -> dict:
            nonlocal poll_count
            poll_count += 1
            result = await original_get_stats()
            adapter._stopping = True
            return result

        adapter._client.get_stats = _get_stats_then_stop  # type: ignore[assignment]
        adapter._poll_events = AsyncMock()  # type: ignore[method-assign]

        with patch("oasisagent.ingestion.frigate.asyncio.sleep", new_callable=AsyncMock):
            await adapter._poll_loop()

        assert poll_count >= 1
        adapter._poll_events.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_polled_when_enabled(self) -> None:
        """When poll_events=True, _poll_events should be called."""
        adapter, _ = _make_adapter(poll_events=True)

        original_get_stats = AsyncMock(return_value={"detectors": {}})

        async def _get_stats_then_stop() -> dict:
            result = await original_get_stats()
            return result

        adapter._client.get_stats = _get_stats_then_stop  # type: ignore[assignment]

        original_poll_events = AsyncMock()

        async def _poll_events_then_stop() -> None:
            await original_poll_events()
            adapter._stopping = True

        adapter._poll_events = _poll_events_then_stop  # type: ignore[method-assign]

        with patch("oasisagent.ingestion.frigate.asyncio.sleep", new_callable=AsyncMock):
            await adapter._poll_loop()

        original_poll_events.assert_called_once()


# ---------------------------------------------------------------------------
# Startup retry
# ---------------------------------------------------------------------------


class TestStartupRetry:
    @pytest.mark.asyncio
    async def test_retries_on_connection_failure(self) -> None:
        adapter, _ = _make_adapter()

        call_count = 0
        original_sleep = asyncio.sleep

        async def _start_side_effect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("refused")

        async def _noop_sleep(_: float) -> None:
            await original_sleep(0)

        adapter._client = AsyncMock()
        adapter._client.start = AsyncMock(side_effect=_start_side_effect)
        adapter._client.get_stats = AsyncMock(return_value={"detectors": {}})
        adapter._client.close = AsyncMock()

        with patch(
            "oasisagent.ingestion.frigate.asyncio.sleep",
            side_effect=_noop_sleep,
        ):
            task = asyncio.create_task(adapter.start())
            for _ in range(200):
                await original_sleep(0)
            adapter._stopping = True
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()

        assert call_count >= 3
        assert adapter._connected is True


# ---------------------------------------------------------------------------
# Enqueue error handling
# ---------------------------------------------------------------------------


class TestEnqueueErrorHandling:
    def test_enqueue_failure_logged(self) -> None:
        adapter, queue = _make_adapter()
        queue.put_nowait.side_effect = RuntimeError("queue full")

        from datetime import UTC, datetime

        from oasisagent.models import Event, EventMetadata

        event = Event(
            source="frigate",
            system="frigate",
            event_type="test",
            entity_id="test",
            severity=Severity.INFO,
            timestamp=datetime.now(tz=UTC),
            payload={},
            metadata=EventMetadata(dedup_key="test"),
        )

        adapter._enqueue(event)  # should not raise


# ---------------------------------------------------------------------------
# Known fixes YAML
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_file_exists(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "frigate.yaml"
        )
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        assert "fixes" in data
        fixes = data["fixes"]
        assert len(fixes) >= 2

        for fix in fixes:
            assert "id" in fix
            assert "match" in fix
            assert fix["match"]["system"] == "frigate"
            assert "diagnosis" in fix
            assert "action" in fix
            assert "risk_tier" in fix

    def test_known_fix_ids_unique(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "frigate.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        ids = [fix["id"] for fix in data["fixes"]]
        assert len(ids) == len(set(ids))

    def test_known_fix_event_types_match_adapter(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "frigate.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        expected_types = {
            "frigate_camera_offline",
            "frigate_detector_slow",
        }

        fix_types = {fix["match"]["event_type"] for fix in data["fixes"]}
        assert fix_types == expected_types


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_frigate_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "frigate" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["frigate"]
        assert meta.model is FrigateAdapterConfig
        assert meta.secret_fields == frozenset()  # no auth required
