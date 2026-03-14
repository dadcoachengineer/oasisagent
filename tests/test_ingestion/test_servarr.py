"""Tests for the Servarr polling ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from oasisagent.config import ServarrAdapterConfig
from oasisagent.ingestion.servarr import _API_VERSION, ServarrAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> ServarrAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:8989",
        "api_key": "test-api-key",
        "app_type": "sonarr",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return ServarrAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[ServarrAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = ServarrAdapter(config, queue)
    return adapter, queue


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = ServarrAdapterConfig()
        assert config.enabled is False
        assert config.app_type == "sonarr"
        assert config.poll_interval == 60

    def test_valid_app_types(self) -> None:
        for app_type in ("sonarr", "radarr", "prowlarr", "bazarr"):
            config = _make_config(app_type=app_type)
            assert config.app_type == app_type

    def test_invalid_app_type(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(app_type="lidarr")

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name_includes_app_type(self) -> None:
        adapter, _ = _make_adapter(app_type="sonarr")
        assert adapter.name == "servarr_sonarr"

        adapter2, _ = _make_adapter(app_type="radarr")
        assert adapter2.name == "servarr_radarr"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.stop()
        assert adapter._stopping is True

    def test_api_version_sonarr(self) -> None:
        adapter, _ = _make_adapter(app_type="sonarr")
        assert adapter._api_version == "v3"

    def test_api_version_prowlarr(self) -> None:
        adapter, _ = _make_adapter(app_type="prowlarr")
        assert adapter._api_version == "v1"

    def test_api_version_bazarr(self) -> None:
        adapter, _ = _make_adapter(app_type="bazarr")
        assert adapter._api_version == "v1"


# ---------------------------------------------------------------------------
# API version mapping
# ---------------------------------------------------------------------------


class TestApiVersionMapping:
    def test_all_app_types_have_version(self) -> None:
        for app_type in ("sonarr", "radarr", "prowlarr", "bazarr"):
            assert app_type in _API_VERSION


# ---------------------------------------------------------------------------
# Health polling
# ---------------------------------------------------------------------------


class TestHealthPolling:
    @pytest.mark.asyncio
    async def test_health_issue_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=[
            {
                "source": "IndexerStatusCheck",
                "type": "warning",
                "message": "Indexer NZBgeek is unavailable",
                "wikiUrl": "https://wiki.servarr.com/sonarr/system#indexers",
            },
        ])
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_health(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "servarr_health_issue"
        assert event.severity == Severity.WARNING
        assert event.payload["app_type"] == "sonarr"
        assert "Indexer NZBgeek" in event.payload["message"]

    @pytest.mark.asyncio
    async def test_health_error_type_severity(self) -> None:
        adapter, queue = _make_adapter()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=[
            {
                "source": "DiskSpaceCheck",
                "type": "error",
                "message": "Disk space critically low",
                "wikiUrl": "",
            },
        ])
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_health(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.severity == Severity.ERROR

    @pytest.mark.asyncio
    async def test_health_dedup_same_issue(self) -> None:
        """Same health issue on second poll should not emit again."""
        adapter, queue = _make_adapter()
        adapter._health_issues.add("IndexerStatusCheck:Indexer down")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=[
            {
                "source": "IndexerStatusCheck",
                "type": "warning",
                "message": "Indexer down",
            },
        ])
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_health(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_clears_resolved_issues(self) -> None:
        """Resolved issues should be removed from tracking."""
        adapter, _queue = _make_adapter()
        adapter._health_issues.add("OldIssue:gone now")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=[])
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_health(mock_session)
        assert len(adapter._health_issues) == 0


# ---------------------------------------------------------------------------
# Queue polling
# ---------------------------------------------------------------------------


class TestQueuePolling:
    @pytest.mark.asyncio
    async def test_failed_download_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "records": [
                {
                    "id": 42,
                    "title": "Show S01E01",
                    "status": "failed",
                    "trackedDownloadStatus": "warning",
                    "statusMessages": [{"title": "Download failed"}],
                },
            ],
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_queue(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "servarr_queue_failed"
        assert event.severity == Severity.ERROR
        assert event.entity_id == "Show S01E01"

    @pytest.mark.asyncio
    async def test_stuck_download_emits_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()

        three_hours_ago = (
            datetime.now(tz=UTC) - timedelta(hours=3)
        ).isoformat()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "records": [
                {
                    "id": 99,
                    "title": "Movie 2026",
                    "status": "downloading",
                    "trackedDownloadStatus": "ok",
                    "added": three_hours_ago,
                },
            ],
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_queue(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "servarr_queue_stuck"
        assert event.severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_recent_download_no_stuck_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        adapter, queue = _make_adapter()

        ten_minutes_ago = (
            datetime.now(tz=UTC) - timedelta(minutes=10)
        ).isoformat()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "records": [
                {
                    "id": 100,
                    "title": "New Show",
                    "status": "downloading",
                    "trackedDownloadStatus": "ok",
                    "added": ten_minutes_ago,
                },
            ],
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_queue(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_queue_dedup_same_failed(self) -> None:
        """Same failed download ID should not emit twice."""
        adapter, queue = _make_adapter()
        adapter._failed_ids.add("42")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "records": [
                {
                    "id": 42,
                    "title": "Show",
                    "status": "failed",
                    "trackedDownloadStatus": "warning",
                },
            ],
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_queue(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_queue_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"records": []})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await adapter._poll_queue(mock_session)
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_file_exists(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "servarr.yaml"
        )
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        assert "fixes" in data
        fixes = data["fixes"]
        assert len(fixes) >= 3

        for fix in fixes:
            assert "id" in fix
            assert "match" in fix
            assert "diagnosis" in fix
            assert "action" in fix
            assert "risk_tier" in fix

    def test_known_fix_ids_unique(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "servarr.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        ids = [fix["id"] for fix in data["fixes"]]
        assert len(ids) == len(set(ids))


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

        adapter._poll_health = _fail_poll  # type: ignore[method-assign]
        adapter._poll_queue = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.servarr.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.servarr.asyncio.sleep",
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

        async def _poll_health(session: object) -> None:
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

        adapter._poll_health = _poll_health  # type: ignore[method-assign]
        adapter._poll_queue = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.servarr.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.servarr.asyncio.sleep",
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
    def test_servarr_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "servarr" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["servarr"]
        assert meta.model is ServarrAdapterConfig
        assert "api_key" in meta.secret_fields


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
            source="servarr_sonarr",
            system="sonarr",
            event_type="test",
            entity_id="test",
            severity=Severity.INFO,
            timestamp=datetime.now(tz=UTC),
            payload={},
            metadata=EventMetadata(dedup_key="test"),
        )

        adapter._enqueue(event)  # should not raise
