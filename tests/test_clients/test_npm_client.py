"""Tests for the Nginx Proxy Manager API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.clients.npm import NpmClient


@pytest.fixture
def client() -> NpmClient:
    return NpmClient(
        url="http://npm.local:81",
        email="admin@example.com",
        password="secret",
        timeout=5,
    )


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_authenticates(self, client: NpmClient) -> None:
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"token": "jwt-abc"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch("oasisagent.clients.npm.aiohttp.ClientSession", return_value=mock_session):
            await client.connect()

        assert client._token == "jwt-abc"

    @pytest.mark.asyncio
    async def test_connect_failure_raises(self, client: NpmClient) -> None:
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.text = AsyncMock(return_value="bad creds")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        with (
            patch("oasisagent.clients.npm.aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(ConnectionError, match="NPM login failed"),
        ):
            await client.connect()

    @pytest.mark.asyncio
    async def test_close_clears_state(self, client: NpmClient) -> None:
        client._session = AsyncMock()
        client._token = "jwt-abc"
        await client.close()
        assert client._session is None
        assert client._token is None


class TestGet:
    @pytest.mark.asyncio
    async def test_get_not_connected_raises(self, client: NpmClient) -> None:
        with pytest.raises(RuntimeError, match="not connected"):
            await client.get("/api/nginx/proxy-hosts")

    @pytest.mark.asyncio
    async def test_get_success(self, client: NpmClient) -> None:
        client._session = AsyncMock()
        client._token = "jwt-abc"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=[{"id": 1}])
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        client._session.get = MagicMock(return_value=mock_resp)

        result = await client.get("/api/nginx/proxy-hosts")
        assert result == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_get_401_retries_with_new_token(self, client: NpmClient) -> None:
        client._session = AsyncMock()
        client._token = "old-token"

        # First call returns 401
        mock_resp_401 = AsyncMock()
        mock_resp_401.status = 401
        mock_resp_401.__aenter__ = AsyncMock(return_value=mock_resp_401)
        mock_resp_401.__aexit__ = AsyncMock(return_value=False)

        # Re-auth response
        mock_auth_resp = AsyncMock()
        mock_auth_resp.status = 200
        mock_auth_resp.json = AsyncMock(return_value={"token": "new-token"})
        mock_auth_resp.__aenter__ = AsyncMock(return_value=mock_auth_resp)
        mock_auth_resp.__aexit__ = AsyncMock(return_value=False)

        # Retry response
        mock_resp_200 = AsyncMock()
        mock_resp_200.status = 200
        mock_resp_200.json = AsyncMock(return_value=[{"id": 1}])
        mock_resp_200.__aenter__ = AsyncMock(return_value=mock_resp_200)
        mock_resp_200.__aexit__ = AsyncMock(return_value=False)

        # get returns 401 first, then 200 on retry
        client._session.get = MagicMock(
            side_effect=[mock_resp_401, mock_resp_200],
        )
        client._session.post = MagicMock(return_value=mock_auth_resp)

        result = await client.get("/api/nginx/proxy-hosts")
        assert result == [{"id": 1}]
        assert client._token == "new-token"

    @pytest.mark.asyncio
    async def test_get_error_raises(self, client: NpmClient) -> None:
        client._session = AsyncMock()
        client._token = "jwt-abc"

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal error")
        mock_resp.json = AsyncMock(return_value={"error": "fail"})
        mock_resp.request_info = MagicMock()
        mock_resp.history = ()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        client._session.get = MagicMock(return_value=mock_resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.get("/api/nginx/proxy-hosts")


class TestAuthHeaders:
    def test_auth_headers_with_token(self, client: NpmClient) -> None:
        client._token = "my-jwt"
        headers = client._auth_headers()
        assert headers == {"Authorization": "Bearer my-jwt"}

    def test_auth_headers_without_token(self, client: NpmClient) -> None:
        client._token = None
        headers = client._auth_headers()
        assert headers == {}
