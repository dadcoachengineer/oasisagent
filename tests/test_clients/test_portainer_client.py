"""Tests for the Portainer API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.clients.portainer import PortainerClient


@pytest.fixture
def client() -> PortainerClient:
    return PortainerClient(
        url="https://portainer.local:9443",
        api_key="ptr_test_key_1234567890",
        verify_ssl=False,
        timeout=5,
    )


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_session_with_api_key_header(
        self, client: PortainerClient,
    ) -> None:
        with patch(
            "oasisagent.clients.portainer.aiohttp.ClientSession",
        ) as mock_cls:
            mock_session = AsyncMock()
            mock_cls.return_value = mock_session
            await client.start()

        call_kwargs = mock_cls.call_args[1]
        headers = call_kwargs["headers"]
        assert headers["X-API-Key"] == "ptr_test_key_1234567890"

    @pytest.mark.asyncio
    async def test_start_ssl_false_when_verify_disabled(
        self, client: PortainerClient,
    ) -> None:
        with patch(
            "oasisagent.clients.portainer.aiohttp.TCPConnector",
        ) as mock_conn, patch(
            "oasisagent.clients.portainer.aiohttp.ClientSession",
        ):
            await client.start()

        mock_conn.assert_called_once_with(ssl=False)

    @pytest.mark.asyncio
    async def test_start_ssl_true_when_verify_enabled(self) -> None:
        client = PortainerClient(
            url="https://portainer.local:9443",
            api_key="key",
            verify_ssl=True,
        )
        with patch(
            "oasisagent.clients.portainer.aiohttp.TCPConnector",
        ) as mock_conn, patch(
            "oasisagent.clients.portainer.aiohttp.ClientSession",
        ):
            await client.start()

        mock_conn.assert_called_once_with(ssl=True)

    @pytest.mark.asyncio
    async def test_close_clears_session(self, client: PortainerClient) -> None:
        client._session = AsyncMock()
        await client.close()
        assert client._session is None

    @pytest.mark.asyncio
    async def test_close_noop_when_no_session(
        self, client: PortainerClient,
    ) -> None:
        await client.close()  # Should not raise


class TestHealthy:
    @pytest.mark.asyncio
    async def test_healthy_true_on_200(self, client: PortainerClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        assert await client.healthy() is True

    @pytest.mark.asyncio
    async def test_healthy_false_on_error(
        self, client: PortainerClient,
    ) -> None:
        client._session = AsyncMock()
        client._session.get = MagicMock(
            side_effect=aiohttp.ClientError("connection refused"),
        )
        assert await client.healthy() is False

    @pytest.mark.asyncio
    async def test_healthy_false_without_session(
        self, client: PortainerClient,
    ) -> None:
        assert await client.healthy() is False


class TestGet:
    @pytest.mark.asyncio
    async def test_get_not_started_raises(
        self, client: PortainerClient,
    ) -> None:
        with pytest.raises(RuntimeError, match="not started"):
            await client.get("/api/endpoints")

    @pytest.mark.asyncio
    async def test_get_returns_json(self, client: PortainerClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value=[{"Id": 1, "Name": "primary", "Type": 1}],
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        result = await client.get("/api/endpoints")
        assert result == [{"Id": 1, "Name": "primary", "Type": 1}]

    @pytest.mark.asyncio
    async def test_get_raises_on_http_error(
        self, client: PortainerClient,
    ) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.text = AsyncMock(return_value="unauthorized")
        mock_resp.request_info = MagicMock()
        mock_resp.history = ()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.get("/api/endpoints")


class TestGetDocker:
    @pytest.mark.asyncio
    async def test_get_docker_not_started_raises(
        self, client: PortainerClient,
    ) -> None:
        with pytest.raises(RuntimeError, match="not started"):
            await client.get_docker(1, "containers/json")

    @pytest.mark.asyncio
    async def test_get_docker_constructs_correct_path(
        self, client: PortainerClient,
    ) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=[])
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        await client.get_docker(2, "containers/json", all="true")

        call_url = client._session.get.call_args[0][0]
        assert "/api/endpoints/2/docker/containers/json" in call_url

    @pytest.mark.asyncio
    async def test_get_docker_raises_on_http_error(
        self, client: PortainerClient,
    ) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="internal error")
        mock_resp.request_info = MagicMock()
        mock_resp.history = ()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        client._session = AsyncMock()
        client._session.get = MagicMock(return_value=mock_resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.get_docker(1, "containers/json")
