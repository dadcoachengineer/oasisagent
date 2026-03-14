"""Tests for the Tdarr polling ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from oasisagent.config import TdarrAdapterConfig
from oasisagent.ingestion.tdarr import _STUCK_POLL_THRESHOLD, TdarrAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> TdarrAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:8265",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return TdarrAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[TdarrAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = TdarrAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = TdarrAdapterConfig()
        assert config.enabled is False
        assert config.poll_interval == 60

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "tdarr"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.stop()
        assert adapter._stopping is True


# ---------------------------------------------------------------------------
# Worker status
# ---------------------------------------------------------------------------


class TestWorkerStatus:
    def test_worker_goes_offline(self) -> None:
        adapter, queue = _make_adapter()
        adapter._worker_states["worker1"] = True

        adapter._check_workers({
            "workers": {
                "worker1": None,
            },
        })

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tdarr_worker_failed"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "worker1"

    def test_worker_recovered(self) -> None:
        adapter, queue = _make_adapter()
        adapter._worker_states["worker1"] = False

        adapter._check_workers({
            "workers": {
                "worker1": {"idle": True, "workerType": "transcode"},
            },
        })

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tdarr_worker_recovered"
        assert event.severity == Severity.INFO

    def test_worker_still_online_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._worker_states["worker1"] = True

        adapter._check_workers({
            "workers": {
                "worker1": {"idle": True, "workerType": "transcode"},
            },
        })

        queue.put_nowait.assert_not_called()

    def test_first_poll_offline_emits(self) -> None:
        adapter, queue = _make_adapter()

        adapter._check_workers({
            "workers": {
                "worker1": None,
            },
        })

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tdarr_worker_failed"

    def test_first_poll_online_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._check_workers({
            "workers": {
                "worker1": {"idle": True, "workerType": "transcode"},
            },
        })

        queue.put_nowait.assert_not_called()

    def test_worker_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._worker_states["worker1"] = True

        adapter._check_workers({"workers": {"worker1": None}})

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "tdarr:worker:worker1"


# ---------------------------------------------------------------------------
# Queue stuck detection
# ---------------------------------------------------------------------------


class TestQueueStuck:
    def test_queue_stuck_after_threshold_polls(self) -> None:
        adapter, queue = _make_adapter()
        adapter._last_queue_processed = 10

        # Simulate _STUCK_POLL_THRESHOLD polls with no progress
        for _i in range(_STUCK_POLL_THRESHOLD):
            queue.reset_mock()
            adapter._check_queue({
                "queueCount": 5,
                "processedCount": 10,
            })

        # Should have emitted on the threshold-th poll
        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tdarr_queue_stuck"
        assert event.severity == Severity.WARNING

    def test_queue_progress_resets_counter(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._last_queue_processed = 10
        adapter._stall_counter = 2  # almost stuck

        adapter._check_queue({
            "queueCount": 5,
            "processedCount": 12,  # progress made
        })

        assert adapter._stall_counter == 0

    def test_queue_empty_no_stuck(self) -> None:
        adapter, queue = _make_adapter()
        adapter._last_queue_processed = 10
        adapter._stall_counter = 5

        adapter._check_queue({
            "queueCount": 0,
            "processedCount": 10,
        })

        assert adapter._stall_counter == 0
        queue.put_nowait.assert_not_called()

    def test_queue_stuck_only_emits_once(self) -> None:
        adapter, queue = _make_adapter()
        adapter._last_queue_processed = 10
        adapter._queue_stuck = True
        adapter._stall_counter = _STUCK_POLL_THRESHOLD + 1

        adapter._check_queue({
            "queueCount": 5,
            "processedCount": 10,
        })

        queue.put_nowait.assert_not_called()

    def test_queue_recovered_after_stuck(self) -> None:
        adapter, queue = _make_adapter()
        adapter._last_queue_processed = 10
        adapter._queue_stuck = True

        adapter._check_queue({
            "queueCount": 5,
            "processedCount": 15,  # progress made
        })

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tdarr_queue_recovered"
        assert event.severity == Severity.INFO

    def test_queue_stuck_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._last_queue_processed = 10
        adapter._stall_counter = _STUCK_POLL_THRESHOLD - 1

        adapter._check_queue({
            "queueCount": 5,
            "processedCount": 10,
        })

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "tdarr:queue:stuck"


# ---------------------------------------------------------------------------
# Known fixes (in plex.yaml)
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_tdarr_worker_fix_in_plex_yaml(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "plex.yaml"
        )
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        fix_ids = {fix["id"] for fix in data["fixes"]}
        assert "tdarr-worker-failed" in fix_ids


# ---------------------------------------------------------------------------
# Poll loop backoff
# ---------------------------------------------------------------------------


class TestPollLoopBackoff:
    @pytest.mark.asyncio
    async def test_backoff_increases_on_repeated_failures(self) -> None:
        """Poll loop uses exponential backoff when service is persistently down."""
        adapter, _ = _make_adapter(poll_interval=10)

        poll_count = 0
        sleep_totals: list[int] = []
        current_total = 0

        original_sleep = asyncio.sleep

        async def _tracking_sleep(_: float) -> None:
            nonlocal current_total
            current_total += 1
            await original_sleep(0)

        async def _fail_poll(_session: object) -> None:
            nonlocal poll_count, current_total
            if poll_count > 0:
                sleep_totals.append(current_total)
                current_total = 0
            poll_count += 1
            if poll_count >= 4:
                adapter._stopping = True
                return
            import aiohttp
            raise aiohttp.ClientError("connection refused")

        adapter._poll_status = _fail_poll  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.tdarr.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.tdarr.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
            mock_session = AsyncMock()
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter._poll_loop()

        assert len(sleep_totals) >= 2
        for i in range(1, len(sleep_totals)):
            assert sleep_totals[i] >= sleep_totals[i - 1]

    @pytest.mark.asyncio
    async def test_backoff_resets_on_success(self) -> None:
        """Backoff resets to poll_interval after a successful poll."""
        adapter, _ = _make_adapter(poll_interval=10)

        poll_count = 0
        sleep_totals: list[int] = []
        current_total = 0

        original_sleep = asyncio.sleep

        async def _tracking_sleep(_: float) -> None:
            nonlocal current_total
            current_total += 1
            await original_sleep(0)

        async def _poll_status(session: object) -> None:
            nonlocal poll_count, current_total
            if poll_count > 0:
                sleep_totals.append(current_total)
                current_total = 0
            poll_count += 1
            if poll_count == 1:
                import aiohttp
                raise aiohttp.ClientError("down")
            if poll_count >= 3:
                adapter._stopping = True
                return

        adapter._poll_status = _poll_status  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.tdarr.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.tdarr.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
            mock_session = AsyncMock()
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter._poll_loop()

        assert len(sleep_totals) >= 2
        assert sleep_totals[-1] == adapter._config.poll_interval


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_tdarr_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "tdarr" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["tdarr"]
        assert meta.model is TdarrAdapterConfig
        assert len(meta.secret_fields) == 0  # no secrets
