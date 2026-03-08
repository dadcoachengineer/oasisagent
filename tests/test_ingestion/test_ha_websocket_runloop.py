"""Run-loop tests for the HA WebSocket ingestion adapter.

Tests auth handshake, message dispatch, auth failure, and reconnect behavior.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from oasisagent.config import HaWebSocketConfig
from oasisagent.engine.queue import EventQueue
from oasisagent.ingestion.ha_websocket import HaWebSocketAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> HaWebSocketConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "url": "ws://localhost:8123/api/websocket",
        "token": "test-token",
    }
    defaults.update(overrides)
    return HaWebSocketConfig(**defaults)


def _make_ws_msg(data: dict[str, Any]) -> MagicMock:
    """Create a mock aiohttp WSMessage."""
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.TEXT
    msg.data = json.dumps(data)
    return msg


class _AsyncIterFromList:
    """Wrap a list of items as an async iterator for `async for`."""

    def __init__(self, items: list[MagicMock]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIterFromList:
        return self

    async def __anext__(self) -> MagicMock:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


def _setup_session_mock(
    mock_session_cls: MagicMock,
    mock_ws: MagicMock,
) -> MagicMock:
    """Wire up the session → ws_connect → ws context manager chain."""
    mock_ws_ctx = MagicMock()
    mock_ws_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ws_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.ws_connect = MagicMock(return_value=mock_ws_ctx)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session_cls.return_value = mock_session
    return mock_session


# ---------------------------------------------------------------------------
# _connect_and_listen — full auth + one event
# ---------------------------------------------------------------------------


class TestWebSocketConnectAndListen:
    @patch("oasisagent.ingestion.ha_websocket.aiohttp.ClientSession")
    async def test_auth_and_event_processing(
        self, mock_session_cls: MagicMock
    ) -> None:
        """Full handshake: auth_required → auth → auth_ok → subscribe → event."""
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_ok"},
            ]
        )

        # Event + close message sequence
        state_event = _make_ws_msg({
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {
                    "entity_id": "light.kitchen",
                    "old_state": {"state": "on", "entity_id": "light.kitchen"},
                    "new_state": {
                        "state": "unavailable",
                        "entity_id": "light.kitchen",
                        "attributes": {},
                    },
                },
            },
        })
        close_msg = MagicMock()
        close_msg.type = aiohttp.WSMsgType.CLOSED
        mock_ws.__aiter__ = lambda self: _AsyncIterFromList([state_event, close_msg])

        _setup_session_mock(mock_session_cls, mock_ws)

        # Call _connect_and_listen directly (avoids start() reconnect loop)
        await adapter._connect_and_listen()

        assert queue.size == 1
        event = queue.drain()[0]
        assert event.entity_id == "light.kitchen"
        assert event.event_type == "state_unavailable"
        assert event.source == "ha_websocket"

    @patch("oasisagent.ingestion.ha_websocket.aiohttp.ClientSession")
    async def test_auth_sends_token(
        self, mock_session_cls: MagicMock
    ) -> None:
        """Adapter sends the configured token during auth handshake."""
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(token="my-secret-token"), queue)

        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_ok"},
            ]
        )
        close_msg = MagicMock()
        close_msg.type = aiohttp.WSMsgType.CLOSED
        mock_ws.__aiter__ = lambda self: _AsyncIterFromList([close_msg])

        _setup_session_mock(mock_session_cls, mock_ws)

        await adapter._connect_and_listen()

        # First send_json call should be auth
        auth_call = mock_ws.send_json.call_args_list[0]
        assert auth_call[0][0]["type"] == "auth"
        assert auth_call[0][0]["access_token"] == "my-secret-token"

    @patch("oasisagent.ingestion.ha_websocket.aiohttp.ClientSession")
    async def test_subscribes_after_auth(
        self, mock_session_cls: MagicMock
    ) -> None:
        """After auth_ok, adapter sends subscribe_events."""
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_ok"},
            ]
        )
        close_msg = MagicMock()
        close_msg.type = aiohttp.WSMsgType.CLOSED
        mock_ws.__aiter__ = lambda self: _AsyncIterFromList([close_msg])

        _setup_session_mock(mock_session_cls, mock_ws)

        await adapter._connect_and_listen()

        # Second send_json call should be subscribe
        sub_call = mock_ws.send_json.call_args_list[1]
        assert sub_call[0][0]["type"] == "subscribe_events"


# ---------------------------------------------------------------------------
# Auth failure — fatal, stops adapter
# ---------------------------------------------------------------------------


class TestWebSocketAuthFailure:
    @patch("oasisagent.ingestion.ha_websocket.aiohttp.ClientSession")
    async def test_auth_invalid_stops_adapter(
        self, mock_session_cls: MagicMock
    ) -> None:
        """auth_invalid response should set _stopping (fatal)."""
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_json = AsyncMock(
            side_effect=[
                {"type": "auth_required"},
                {"type": "auth_invalid", "message": "Invalid access token"},
            ]
        )

        _setup_session_mock(mock_session_cls, mock_ws)

        await adapter._connect_and_listen()

        assert adapter._stopping is True
        assert await adapter.healthy() is False


# ---------------------------------------------------------------------------
# Reconnect — handshake error triggers backoff
# ---------------------------------------------------------------------------


class TestWebSocketReconnect:
    async def test_handshake_error_retries(self) -> None:
        """WSServerHandshakeError triggers backoff via start() loop."""
        queue = EventQueue(max_size=10)
        adapter = HaWebSocketAdapter(_make_config(), queue)

        call_count = 0

        async def _failing_connect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                await adapter.stop()
            raise aiohttp.WSServerHandshakeError(
                request_info=MagicMock(),
                history=(),
                status=502,
                message="Bad Gateway",
                headers=MagicMock(),
            )

        with (
            patch.object(adapter, "_connect_and_listen", _failing_connect),
            patch.object(adapter._backoff, "wait", new_callable=AsyncMock),
        ):
            await adapter.start()

        assert call_count >= 2
        assert await adapter.healthy() is False
