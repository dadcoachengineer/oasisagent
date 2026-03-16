"""Tests for the Overseerr polling ingestion adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
            "oasisagent.ingestion.overseerr.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.overseerr.asyncio.sleep",
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
            "oasisagent.ingestion.overseerr.aiohttp.ClientSession",
        ) as mock_cs, patch(
            "oasisagent.ingestion.overseerr.asyncio.sleep",
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
    def test_overseerr_connector_registered(self) -> None:
        from oasisagent.db.registry import CONNECTOR_TYPES

        assert "overseerr" in CONNECTOR_TYPES
        meta = CONNECTOR_TYPES["overseerr"]
        assert meta.model is OverseerrAdapterConfig
        assert "api_key" in meta.secret_fields
