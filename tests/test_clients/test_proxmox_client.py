"""Tests for the Proxmox VE API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.clients.proxmox import ProxmoxClient


@pytest.fixture
def client() -> ProxmoxClient:
    return ProxmoxClient(
        url="https://pve.local:8006",
        user="root@pam",
        token_name="oasis",
        token_value="secret-token-value",
        verify_ssl=False,
        timeout=5,
    )


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_session_with_auth_header(
        self, client: ProxmoxClient,
    ) -> None:
        with patch(
            "oasisagent.clients.proxmox.aiohttp.ClientSession",
        ) as mock_cls:
            mock_session = AsyncMock()
            mock_cls.return_value = mock_session
            await client.start()

        # Verify auth header format
        call_kwargs = mock_cls.call_args[1]
        headers = call_kwargs["headers"]
        assert headers["Authorization"] == (
            "PVEAPIToken=root@pam!oasis=secret-token-value"
        )

    @pytest.mark.asyncio
    async def test_start_ssl_false_when_verify_disabled(
        self, client: ProxmoxClient,
    ) -> None:
        with patch(
            "oasisagent.clients.proxmox.aiohttp.TCPConnector",
        ) as mock_conn, patch(
            "oasisagent.clients.proxmox.aiohttp.ClientSession",
        ):
            await client.start()

        mock_conn.assert_called_once_with(ssl=False)

    @pytest.mark.asyncio
    async def test_start_ssl_true_when_verify_enabled(self) -> None:
        client = ProxmoxClient(
            url="https://pve.local:8006",
            user="root@pam",
            token_name="oasis",
            token_value="secret",
            verify_ssl=True,
        )
        with patch(
            "oasisagent.clients.proxmox.aiohttp.TCPConnector",
        ) as mock_conn, patch(
            "oasisagent.clients.proxmox.aiohttp.ClientSession",
        ):
            await client.start()

        mock_conn.assert_called_once_with(ssl=True)

    @pytest.mark.asyncio
    async def test_close_clears_session(self, client: ProxmoxClient) -> None:
        client._session = AsyncMock()
        await client.close()
        assert client._session is None

    @pytest.mark.asyncio
    async def test_close_noop_when_no_session(
        self, client: ProxmoxClient,
    ) -> None:
        await client.close()  # Should not raise


class TestHealthy:
    @pytest.mark.asyncio
    async def test_healthy_true_on_200(self, client: ProxmoxClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        assert await client.healthy() is True

    @pytest.mark.asyncio
    async def test_healthy_false_on_error(
        self, client: ProxmoxClient,
    ) -> None:
        client._session = AsyncMock()
        client._session.get = MagicMock(
            side_effect=aiohttp.ClientError("connection refused"),
        )
        assert await client.healthy() is False

    @pytest.mark.asyncio
    async def test_healthy_false_without_session(
        self, client: ProxmoxClient,
    ) -> None:
        assert await client.healthy() is False


class TestGet:
    @pytest.mark.asyncio
    async def test_get_not_started_raises(
        self, client: ProxmoxClient,
    ) -> None:
        with pytest.raises(RuntimeError, match="not started"):
            await client.get("/api2/json/version")

    @pytest.mark.asyncio
    async def test_get_unwraps_data(self, client: ProxmoxClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={"data": [{"node": "pve01", "online": 1}]},
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        result = await client.get("/api2/json/cluster/status")
        assert result == [{"node": "pve01", "online": 1}]

    @pytest.mark.asyncio
    async def test_get_returns_raw_when_no_data_key(
        self, client: ProxmoxClient,
    ) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"version": "8.0"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        result = await client.get("/api2/json/version")
        assert result == {"version": "8.0"}

    @pytest.mark.asyncio
    async def test_get_raises_on_http_error(
        self, client: ProxmoxClient,
    ) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.text = AsyncMock(return_value="authentication required")
        mock_resp.request_info = MagicMock()
        mock_resp.history = ()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.get("/api2/json/cluster/status")
