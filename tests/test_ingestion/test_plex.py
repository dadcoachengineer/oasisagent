"""Tests for the Plex polling ingestion adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from oasisagent.config import PlexAdapterConfig
from oasisagent.ingestion.plex import PlexAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> PlexAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:32400",
        "token": "test-plex-token",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return PlexAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[PlexAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = PlexAdapter(config, queue)
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
        config = PlexAdapterConfig()
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
        assert adapter.name == "plex"

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
# Server reachability
# ---------------------------------------------------------------------------


class TestServerReachability:
    @pytest.mark.asyncio
    async def test_server_unreachable_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_reachable = True

        adapter._handle_unreachable("Connection refused")

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "plex_server_unreachable"
        assert event.severity == Severity.ERROR
        assert event.payload["error"] == "Connection refused"

    @pytest.mark.asyncio
    async def test_server_recovered_emits_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_reachable = False

        adapter._handle_reachable()

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "plex_server_recovered"
        assert event.severity == Severity.INFO

    def test_already_reachable_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_reachable = True

        adapter._handle_reachable()
        queue.put_nowait.assert_not_called()

    def test_already_unreachable_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_reachable = False

        adapter._handle_unreachable("still down")
        queue.put_nowait.assert_not_called()

    def test_first_poll_unreachable_emits(self) -> None:
        adapter, queue = _make_adapter()
        # _server_reachable is None (first poll)

        adapter._handle_unreachable("Connection refused")

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "plex_server_unreachable"

    def test_first_poll_reachable_no_event(self) -> None:
        adapter, queue = _make_adapter()

        adapter._handle_reachable()
        queue.put_nowait.assert_not_called()

    def test_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_reachable = True

        adapter._handle_unreachable("error")

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "plex:server:reachable"

    @pytest.mark.asyncio
    async def test_check_server_success(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._server_reachable = False

        mock_resp = _mock_response({}, status=200)
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        result = await adapter._check_server(mock_session)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_server_failure(self) -> None:
        adapter, _queue = _make_adapter()
        adapter._server_reachable = True

        mock_resp = _mock_response({}, status=503)
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        result = await adapter._check_server(mock_session)
        assert result is False


# ---------------------------------------------------------------------------
# Library polling
# ---------------------------------------------------------------------------


class TestLibraryPolling:
    @pytest.mark.asyncio
    async def test_empty_library_emits_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "MediaContainer": {
                "Directory": [
                    {
                        "key": "1",
                        "title": "Movies",
                        "type": "movie",
                        "count": 0,
                        "refreshing": False,
                    },
                ],
            },
        }))

        await adapter._poll_libraries(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "plex_library_scan_failed"
        assert event.severity == Severity.WARNING
        assert event.entity_id == "Movies"

    @pytest.mark.asyncio
    async def test_scanning_library_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "MediaContainer": {
                "Directory": [
                    {
                        "key": "1",
                        "title": "Movies",
                        "type": "movie",
                        "count": 0,
                        "refreshing": True,
                    },
                ],
            },
        }))

        await adapter._poll_libraries(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_populated_library_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "MediaContainer": {
                "Directory": [
                    {
                        "key": "1",
                        "title": "Movies",
                        "type": "movie",
                        "count": 500,
                        "refreshing": False,
                    },
                ],
            },
        }))

        await adapter._poll_libraries(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_library_dedup(self) -> None:
        adapter, queue = _make_adapter()
        adapter._library_errors.add("1")

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({
            "MediaContainer": {
                "Directory": [
                    {"key": "1", "title": "Movies", "count": 0, "refreshing": False},
                ],
            },
        }))

        await adapter._poll_libraries(mock_session)
        queue.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Known fixes
# ---------------------------------------------------------------------------


class TestKnownFixes:
    def test_known_fixes_file_exists(self) -> None:
        from pathlib import Path

        import yaml

        fixes_path = (
            Path(__file__).parent.parent.parent / "known_fixes" / "plex.yaml"
        )
        assert fixes_path.exists()

        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        assert "fixes" in data
        fixes = data["fixes"]
        assert len(fixes) >= 4

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
            Path(__file__).parent.parent.parent / "known_fixes" / "plex.yaml"
        )
        with fixes_path.open() as f:
            data = yaml.safe_load(f)

        ids = [fix["id"] for fix in data["fixes"]]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_plex_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "plex" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["plex"]
        assert meta.model is PlexAdapterConfig
        assert "token" in meta.secret_fields
