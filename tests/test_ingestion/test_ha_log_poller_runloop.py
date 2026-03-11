"""Run-loop tests for the HA log poller ingestion adapter.

Tests start(), WebSocket reconnection, and auth failure handling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

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
# start() — one poll cycle, one event
# ---------------------------------------------------------------------------


class TestLogPollerStartOnePoll:
    async def test_connect_and_poll_produces_event(self) -> None:
        """One successful WebSocket poll with a matching entry produces an event."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        poll_count = 0

        async def _fake_connect_and_poll() -> None:
            nonlocal poll_count
            poll_count += 1
            # Simulate processing a matching entry
            adapter._process_entries([{
                "name": "homeassistant.components.zwave_js",
                "message": "Error setting up integration 'zwave_js'",
                "level": "ERROR",
                "timestamp": 1741624926.123,
                "count": 1,
                "first_occurred": 1741624926.123,
                "source": ["components/zwave_js/__init__.py", 100],
            }])
            adapter._stopping = True

        adapter._connect_and_poll = _fake_connect_and_poll  # type: ignore[method-assign]
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
    async def test_auth_failure_stops_adapter(self) -> None:
        """WebSocket auth failure is fatal — adapter should return, not retry."""
        from oasisagent.ingestion.ha_log_poller import _AuthError

        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(), queue)

        async def _fake_connect_and_poll() -> None:
            raise _AuthError("bad token")

        adapter._connect_and_poll = _fake_connect_and_poll  # type: ignore[method-assign]
        await adapter.start()

        assert adapter._stopping is True
        assert await adapter.healthy() is False


# ---------------------------------------------------------------------------
# start() — transient WebSocket error retries
# ---------------------------------------------------------------------------


class TestLogPollerTransientError:
    async def test_connection_error_retries(self) -> None:
        """WebSocket connection error triggers backoff and retry."""
        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(poll_interval=1), queue)

        call_count = 0

        async def _fake_connect_and_poll() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                adapter._stopping = True
            raise TimeoutError("connection timed out")

        adapter._connect_and_poll = _fake_connect_and_poll  # type: ignore[method-assign]

        # Patch backoff to avoid real delays
        with patch.object(adapter._backoff, "wait", new_callable=AsyncMock):
            await adapter.start()

        assert call_count >= 2
        assert await adapter.healthy() is False

    async def test_handshake_error_retries(self) -> None:
        """WSServerHandshakeError triggers backoff and retry."""
        import aiohttp

        queue = EventQueue(max_size=10)
        adapter = HaLogPollerAdapter(_make_config(poll_interval=1), queue)

        call_count = 0

        async def _fake_connect_and_poll() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                adapter._stopping = True
            raise aiohttp.WSServerHandshakeError(
                request_info=aiohttp.RequestInfo(
                    url="ws://localhost:8123/api/websocket",
                    method="GET",
                    headers={},  # type: ignore[arg-type]
                    real_url="ws://localhost:8123/api/websocket",
                ),
                history=(),
                status=403,
                message="Forbidden",
                headers={},  # type: ignore[arg-type]
            )

        adapter._connect_and_poll = _fake_connect_and_poll  # type: ignore[method-assign]

        with patch.object(adapter._backoff, "wait", new_callable=AsyncMock):
            await adapter.start()

        assert call_count >= 2
        assert await adapter.healthy() is False
