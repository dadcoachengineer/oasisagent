"""Tests for the qBittorrent polling ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from oasisagent.config import QBittorrentAdapterConfig
from oasisagent.ingestion.qbittorrent import QBittorrentAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> QBittorrentAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:8080",
        "username": "admin",
        "password": "test-password",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return QBittorrentAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[QBittorrentAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = QBittorrentAdapter(config, queue)
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
        config = QBittorrentAdapterConfig()
        assert config.enabled is False
        assert config.username == "admin"
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
        assert adapter.name == "qbittorrent"

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
# Transfer info (connectivity)
# ---------------------------------------------------------------------------


class TestTransferInfo:
    @pytest.mark.asyncio
    async def test_connection_lost_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._connection_ok = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "connection_status": "disconnected",
            "dl_info_speed": 0,
            "up_info_speed": 0,
        }))

        await adapter._poll_transfer_info(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "qbt_connection_lost"
        assert event.severity == Severity.ERROR

    @pytest.mark.asyncio
    async def test_connection_recovered_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._connection_ok = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "connection_status": "connected",
        }))

        await adapter._poll_transfer_info(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "qbt_connection_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_connected_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._connection_ok = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "connection_status": "connected",
        }))

        await adapter._poll_transfer_info(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_disconnected_emits(self) -> None:
        adapter, queue = _make_adapter()
        # _connection_ok is None (first poll)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "connection_status": "disconnected",
        }))

        await adapter._poll_transfer_info(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "qbt_connection_lost"

    @pytest.mark.asyncio
    async def test_first_poll_connected_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "connection_status": "connected",
        }))

        await adapter._poll_transfer_info(mock_session)
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Errored torrents
# ---------------------------------------------------------------------------


class TestErroredTorrents:
    @pytest.mark.asyncio
    async def test_errored_torrent_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response([
            {
                "hash": "abc123",
                "name": "Bad Torrent",
                "state": "error",
                "size": 1_000_000,
                "progress": 0.5,
                "category": "movies",
            },
        ]))

        await adapter._poll_errored_torrents(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "qbt_torrent_error"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "Bad Torrent"
        assert event.payload["hash"] == "abc123"

    @pytest.mark.asyncio
    async def test_errored_dedup(self) -> None:
        adapter, queue = _make_adapter()
        adapter._errored_hashes.add("abc123")

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response([
            {"hash": "abc123", "name": "Bad", "state": "error"},
        ]))

        await adapter._poll_errored_torrents(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolved_error_clears_tracking(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._errored_hashes.add("abc123")

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response([]))

        await adapter._poll_errored_torrents(mock_session)
        assert len(adapter._errored_hashes) == 0


# ---------------------------------------------------------------------------
# Stalled torrents
# ---------------------------------------------------------------------------


class TestStalledTorrents:
    @pytest.mark.asyncio
    async def test_stalled_torrent_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response([
            {
                "hash": "def456",
                "name": "Stalled Download",
                "state": "stalledDL",
                "size": 2_000_000,
                "progress": 0.1,
                "num_seeds": 0,
                "num_leechs": 0,
            },
        ]))

        await adapter._poll_stalled_torrents(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "qbt_torrent_stalled"
        assert event.severity == Severity.INFO
        assert event.entity_id == "Stalled Download"

    @pytest.mark.asyncio
    async def test_stalled_dedup(self) -> None:
        adapter, queue = _make_adapter()
        adapter._stalled_hashes.add("def456")

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response([
            {"hash": "def456", "name": "Stalled", "state": "stalledDL"},
        ]))

        await adapter._poll_stalled_torrents(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_hash_skipped(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response([
            {"hash": "", "name": "No Hash"},
        ]))

        await adapter._poll_stalled_torrents(mock_session)
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_file_exists(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "qbittorrent.yaml"
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
            Path(__file__).parent.parent.parent / "known_fixes" / "qbittorrent.yaml"
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

        async def _fail_session() -> None:
            nonlocal poll_count, current_total
            if poll_count > 0:
                sleep_totals.append(current_total)
                current_total = 0
            poll_count += 1
            if poll_count >= 4:
                adapter._stopping = True
                return MagicMock()
            import aiohttp
            raise aiohttp.ClientError("connection refused")

        adapter._ensure_session = _fail_session  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.qbittorrent.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
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

        async def _ensure_session() -> MagicMock:
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
            return MagicMock()

        adapter._ensure_session = _ensure_session  # type: ignore[method-assign]
        adapter._poll_transfer_info = AsyncMock()  # type: ignore[method-assign]
        adapter._poll_errored_torrents = AsyncMock()  # type: ignore[method-assign]
        adapter._poll_stalled_torrents = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "oasisagent.ingestion.qbittorrent.asyncio.sleep",
            side_effect=_tracking_sleep,
        ):
            await adapter._poll_loop()

        assert len(sleep_totals) >= 2
        assert sleep_totals[-1] == adapter._config.poll_interval


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_qbittorrent_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "qbittorrent" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["qbittorrent"]
        assert meta.model is QBittorrentAdapterConfig
        assert "password" in meta.secret_fields
