"""Tests for the Tautulli polling ingestion adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from oasisagent.config import TautulliAdapterConfig
from oasisagent.ingestion.tautulli import TautulliAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> TautulliAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:8181",
        "api_key": "test-tautulli-key",
        "poll_interval": 60,
        "timeout": 10,
        "bandwidth_threshold_kbps": 100_000,
    }
    defaults.update(overrides)
    return TautulliAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[TautulliAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = TautulliAdapter(config, queue)
    return adapter, queue


def _mock_response(data: object, status: int = 200) -> AsyncMock:
    mock = AsyncMock()
    mock.status = status
    mock.raise_for_status = MagicMock()
    mock.json = AsyncMock(return_value=data)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults(self) -> None:
        config = TautulliAdapterConfig()
        assert config.enabled is False
        assert config.bandwidth_threshold_kbps == 100_000

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            _make_config(bogus="nope")


# ---------------------------------------------------------------------------
# Adapter identity & lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    def test_name(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.name == "tautulli"

    @pytest.mark.asyncio
    async def test_healthy_initially_false(self) -> None:
        adapter, _ = _make_adapter()
        assert not await adapter.healthy()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.stop()
        assert adapter._stopping is True

    def test_api_url_format(self) -> None:
        adapter, _ = _make_adapter()
        url = adapter._api_url("server_status")
        assert "apikey=test-tautulli-key" in url
        assert "cmd=server_status" in url


# ---------------------------------------------------------------------------
# Server status polling
# ---------------------------------------------------------------------------


class TestServerStatus:
    @pytest.mark.asyncio
    async def test_plex_down_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._plex_connected = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "connected": False,
                    "pms_version": "1.40.0",
                    "pms_platform": "Linux",
                },
            },
        }))

        await adapter._poll_server_status(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tautulli_plex_down"
        assert event.severity == Severity.ERROR
        assert event.payload["plex_version"] == "1.40.0"

    @pytest.mark.asyncio
    async def test_plex_recovered_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._plex_connected = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "connected": True,
                    "pms_version": "1.40.0",
                },
            },
        }))

        await adapter._poll_server_status(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tautulli_plex_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_plex_already_connected_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._plex_connected = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {"connected": True},
            },
        }))

        await adapter._poll_server_status(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_disconnected_emits(self) -> None:
        adapter, queue = _make_adapter()
        # _plex_connected is None

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "connected": False,
                    "pms_version": "",
                    "pms_platform": "",
                },
            },
        }))

        await adapter._poll_server_status(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tautulli_plex_down"

    @pytest.mark.asyncio
    async def test_first_poll_connected_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {"connected": True},
            },
        }))

        await adapter._poll_server_status(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_result_skipped(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "error",
                "data": {},
            },
        }))

        await adapter._poll_server_status(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._plex_connected = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {"connected": False, "pms_version": "", "pms_platform": ""},
            },
        }))

        await adapter._poll_server_status(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "tautulli:plex:status"


# ---------------------------------------------------------------------------
# Activity / bandwidth polling
# ---------------------------------------------------------------------------


class TestBandwidthPolling:
    @pytest.mark.asyncio
    async def test_high_bandwidth_emits_event(self) -> None:
        adapter, queue = _make_adapter(bandwidth_threshold_kbps=50_000)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "total_bandwidth": 60_000,
                    "stream_count": 5,
                    "stream_count_direct_play": 3,
                    "stream_count_transcode": 2,
                },
            },
        }))

        await adapter._poll_activity(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tautulli_high_bandwidth"
        assert event.severity == Severity.WARNING
        assert event.payload["total_bandwidth_kbps"] == 60_000
        assert event.payload["stream_count"] == 5

    @pytest.mark.asyncio
    async def test_bandwidth_recovered_emits_event(self) -> None:
        adapter, queue = _make_adapter(bandwidth_threshold_kbps=50_000)
        adapter._bandwidth_high = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "total_bandwidth": 30_000,
                    "stream_count": 2,
                },
            },
        }))

        await adapter._poll_activity(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "tautulli_bandwidth_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_normal_bandwidth_no_event(self) -> None:
        adapter, queue = _make_adapter(bandwidth_threshold_kbps=100_000)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "total_bandwidth": 50_000,
                    "stream_count": 2,
                },
            },
        }))

        await adapter._poll_activity(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_bandwidth_already_high_no_event(self) -> None:
        adapter, queue = _make_adapter(bandwidth_threshold_kbps=50_000)
        adapter._bandwidth_high = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "total_bandwidth": 60_000,
                    "stream_count": 5,
                },
            },
        }))

        await adapter._poll_activity(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_bandwidth_dedup_key(self) -> None:
        adapter, queue = _make_adapter(bandwidth_threshold_kbps=1)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "response": {
                "result": "success",
                "data": {
                    "total_bandwidth": 100,
                    "stream_count": 1,
                    "stream_count_direct_play": 1,
                    "stream_count_transcode": 0,
                },
            },
        }))

        await adapter._poll_activity(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "tautulli:bandwidth"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_tautulli_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "tautulli" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["tautulli"]
        assert meta.model is TautulliAdapterConfig
        assert "api_key" in meta.secret_fields
