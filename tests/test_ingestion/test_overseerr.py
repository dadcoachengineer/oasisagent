"""Tests for the Overseerr polling ingestion adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from oasisagent.config import OverseerrAdapterConfig
from oasisagent.ingestion.overseerr import OverseerrAdapter
from oasisagent.models import Severity


def _make_config(**overrides: object) -> OverseerrAdapterConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "url": "http://localhost:5055",
        "api_key": "test-overseerr-key",
        "poll_interval": 60,
        "timeout": 10,
    }
    defaults.update(overrides)
    return OverseerrAdapterConfig(**defaults)  # type: ignore[arg-type]


def _mock_queue() -> MagicMock:
    return MagicMock()


def _make_adapter(**overrides: object) -> tuple[OverseerrAdapter, MagicMock]:
    config = _make_config(**overrides)
    queue = _mock_queue()
    adapter = OverseerrAdapter(config, queue)
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
        config = OverseerrAdapterConfig()
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
        assert adapter.name == "overseerr"

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
# Status polling
# ---------------------------------------------------------------------------


class TestStatusPolling:
    @pytest.mark.asyncio
    async def test_server_recovered(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_ok = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"version": "1.33.0"}, status=200,
        ))

        await adapter._poll_status(mock_session)

        queue.put_nowait.assert_called_once()
        event = queue.put_nowait.call_args[0][0]
        assert event.event_type == "overseerr_server_recovered"
        assert event.severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_already_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_ok = True

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"version": "1.33.0"}, status=200,
        ))

        await adapter._poll_status(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_poll_ok_no_event(self) -> None:
        adapter, queue = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"version": "1.33.0"}, status=200,
        ))

        await adapter._poll_status(mock_session)
        queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_key(self) -> None:
        adapter, queue = _make_adapter()
        adapter._server_ok = False

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(
            {"version": "1.33.0"}, status=200,
        ))

        await adapter._poll_status(mock_session)

        event = queue.put_nowait.call_args[0][0]
        assert event.metadata.dedup_key == "overseerr:server:status"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_overseerr_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "overseerr" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["overseerr"]
        assert meta.model is OverseerrAdapterConfig
        assert "api_key" in meta.secret_fields
