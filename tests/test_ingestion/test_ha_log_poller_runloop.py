"""Run-loop tests for the HA log poller ingestion adapter.

Tests start(), poll loop, HTTP error handling, and auth failure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from oasisagent.config import HaLogPollerConfig, LogPattern
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.ha_log_poller import HaLogPollerAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> HaLogPollerConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "url": "http://localhost:8123",
        "token": "test-token",
        "poll_interval": 1,
        "dedup_window": 300,
        "patterns": [
            LogPattern(
                regex=r"Error setting up integration '(.+)'",
                event_type="integration_failure",
                severity="error",
            ),
        ],
    }
    defaults.update(overrides)
    return HaLogPollerConfig(**defaults)


# ---------------------------------------------------------------------------
# start() — one poll, one event
# ---------------------------------------------------------------------------


class TestLogPollerStartOnePoll:
    async def test_poll_produces_event(self) -> None:
        """One successful poll with a matching log line produces an event."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(
            return_value="Error setting up integration 'zwave_js'\n"
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        poll_count = 0

        with patch("oasisagent.ingestion.ha_log_poller.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            original_poll = adapter._poll

            async def _poll_then_stop() -> None:
                nonlocal poll_count
                await original_poll()
                poll_count += 1
                await adapter.stop()

            adapter._poll = _poll_then_stop  # type: ignore[method-assign]
            await adapter.start()

        assert poll_count == 1
        assert queue.size == 1
        event = queue.drain()[0]
        assert event.entity_id == "zwave_js"
        assert event.event_type == "integration_failure"


# ---------------------------------------------------------------------------
# start() — auth failure stops adapter
# ---------------------------------------------------------------------------


class TestLogPollerAuthFailure:
    async def test_401_stops_adapter(self) -> None:
        """HTTP 401 is treated as fatal — adapter should return, not retry."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            )
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("oasisagent.ingestion.ha_log_poller.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await adapter.start()

        assert await adapter.healthy() is False


# ---------------------------------------------------------------------------
# start() — transient HTTP error retries
# ---------------------------------------------------------------------------


class TestLogPollerTransientError:
    async def test_500_retries_next_interval(self) -> None:
        """HTTP 500 logs a warning and retries on next interval."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(poll_interval=1), queue)

        call_count = 0

        async def _poll_that_fails_twice() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                await adapter.stop()
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=500,
                message="Internal Server Error",
            )

        adapter._poll = _poll_that_fails_twice  # type: ignore[method-assign]
        await adapter.start()

        assert call_count >= 2
        # stop() sets _connected = False
        assert await adapter.healthy() is False
