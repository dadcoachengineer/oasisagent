"""Tests for the UniFi Network controller HTTP client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.clients.unifi import UnifiClient


def _make_client(**overrides: object) -> UnifiClient:
    """Create a UnifiClient with sensible defaults."""
    defaults: dict[str, object] = {
        "url": "https://192.168.1.1",
        "username": "admin",
        "password": "secret",
        "site": "default",
        "is_udm": True,
        "verify_ssl": False,
        "timeout": 10,
    }
    defaults.update(overrides)
    return UnifiClient(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


class TestUrlBuilding:
    def test_udm_path_prefix(self) -> None:
        client = _make_client(is_udm=True)
        url = client._build_url("stat/device")
        assert url == "https://192.168.1.1/proxy/network/api/s/default/stat/device"

    def test_standalone_path_prefix(self) -> None:
        client = _make_client(is_udm=False)
        url = client._build_url("stat/device")
        assert url == "https://192.168.1.1/api/s/default/stat/device"

    def test_custom_site(self) -> None:
        client = _make_client(site="home")
        url = client._build_url("rest/alarm")
        assert url == "https://192.168.1.1/proxy/network/api/s/home/rest/alarm"

    def test_leading_slash_stripped(self) -> None:
        client = _make_client()
        url = client._build_url("/stat/device")
        assert url == "https://192.168.1.1/proxy/network/api/s/default/stat/device"

    def test_trailing_slash_on_base_url(self) -> None:
        client = _make_client(url="https://192.168.1.1/")
        url = client._build_url("stat/device")
        assert url == "https://192.168.1.1/proxy/network/api/s/default/stat/device"

    def test_site_property(self) -> None:
        client = _make_client(site="office")
        assert client.site == "office"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    @pytest.mark.asyncio
    async def test_udm_login_url(self) -> None:
        """UDM devices use /api/auth/login."""
        client = _make_client(is_udm=True)
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        client._session = mock_session

        await client._authenticate()

        call_args = mock_session.post.call_args
        assert call_args[0][0] == "https://192.168.1.1/api/auth/login"
        assert client._authenticated is True

    @pytest.mark.asyncio
    async def test_standalone_login_url(self) -> None:
        """Standalone controllers use /api/login."""
        client = _make_client(is_udm=False)
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        client._session = mock_session

        await client._authenticate()

        call_args = mock_session.post.call_args
        assert call_args[0][0] == "https://192.168.1.1/api/login"

    @pytest.mark.asyncio
    async def test_login_sends_credentials(self) -> None:
        client = _make_client(username="testuser", password="testpass")
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        client._session = mock_session

        await client._authenticate()

        call_args = mock_session.post.call_args
        assert call_args[1]["json"] == {"username": "testuser", "password": "testpass"}

    @pytest.mark.asyncio
    async def test_login_failure_raises(self) -> None:
        client = _make_client()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_resp = AsyncMock()
        mock_resp.status = 403
        mock_resp.text = AsyncMock(return_value="Forbidden")
        mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        client._session = mock_session

        with pytest.raises(ConnectionError, match="UniFi login failed"):
            await client._authenticate()

        assert client._authenticated is False


# ---------------------------------------------------------------------------
# Request with retry-on-401/403
# ---------------------------------------------------------------------------


class TestRequestRetry:
    @pytest.mark.asyncio
    async def test_successful_request(self) -> None:
        client = _make_client()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_session.request = AsyncMock(return_value=mock_resp)
        client._session = mock_session

        resp = await client.request("GET", "stat/device")
        assert resp.status == 200
        mock_session.request.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_failure_triggers_reauth_and_retry(self, status: int) -> None:
        client = _make_client()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)

        first_resp = MagicMock()
        first_resp.status = status
        first_resp.release = MagicMock()

        second_resp = MagicMock()
        second_resp.status = 200

        mock_session.request = AsyncMock(side_effect=[first_resp, second_resp])

        # Mock _authenticate to avoid real login
        client._session = mock_session
        client._authenticate = AsyncMock()  # type: ignore[method-assign]

        resp = await client.request("GET", "stat/device")

        assert resp.status == 200
        assert mock_session.request.call_count == 2
        client._authenticate.assert_called_once()
        first_resp.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_403_after_reauth_is_returned_as_is(self) -> None:
        """If re-auth succeeds but the retry also returns 403, return it without looping."""
        client = _make_client()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)

        first_resp = MagicMock()
        first_resp.status = 403
        first_resp.release = MagicMock()

        second_resp = MagicMock()
        second_resp.status = 403

        mock_session.request = AsyncMock(side_effect=[first_resp, second_resp])

        client._session = mock_session
        client._authenticate = AsyncMock()  # type: ignore[method-assign]

        resp = await client.request("GET", "stat/device")

        assert resp.status == 403
        assert mock_session.request.call_count == 2
        client._authenticate.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_without_connect_raises(self) -> None:
        client = _make_client()
        with pytest.raises(RuntimeError, match="not connected"):
            await client.request("GET", "stat/device")


# ---------------------------------------------------------------------------
# GET / POST convenience methods
# ---------------------------------------------------------------------------


class TestGetPost:
    @pytest.mark.asyncio
    async def test_get_returns_json(self) -> None:
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"data": [{"mac": "aa:bb:cc"}]})
        mock_resp.release = MagicMock()

        client.request = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        result = await client.get("stat/device")
        assert result == {"data": [{"mac": "aa:bb:cc"}]}
        mock_resp.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_raises_on_error_status(self) -> None:
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal Server Error")
        mock_resp.request_info = MagicMock()
        mock_resp.history = ()
        mock_resp.release = MagicMock()

        client.request = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        with pytest.raises(aiohttp.ClientResponseError):
            await client.get("stat/device")

        mock_resp.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_sends_json_body(self) -> None:
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"meta": {"rc": "ok"}})
        mock_resp.release = MagicMock()

        client.request = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        result = await client.post("cmd/devmgr", {"cmd": "restart", "mac": "aa:bb:cc"})
        assert result == {"meta": {"rc": "ok"}}
        client.request.assert_called_once_with(
            "POST", "cmd/devmgr", json={"cmd": "restart", "mac": "aa:bb:cc"},
        )

    @pytest.mark.asyncio
    async def test_post_raises_on_error_status(self) -> None:
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(return_value="Bad Request")
        mock_resp.request_info = MagicMock()
        mock_resp.history = ()
        mock_resp.release = MagicMock()

        client.request = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

        with pytest.raises(aiohttp.ClientResponseError):
            await client.post("cmd/devmgr", {"cmd": "bad"})


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_creates_session(self) -> None:
        with patch("oasisagent.clients.unifi.aiohttp.ClientSession") as mock_cls:
            mock_session = AsyncMock()
            mock_cls.return_value = mock_session

            client = _make_client()
            client._authenticate = AsyncMock()  # type: ignore[method-assign]
            await client.connect()

            assert client._session is mock_session
            client._authenticate.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_clears_session(self) -> None:
        client = _make_client()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        client._session = mock_session
        client._authenticated = True

        await client.close()

        mock_session.close.assert_called_once()
        assert client._session is None
        assert client._authenticated is False

    @pytest.mark.asyncio
    async def test_close_when_no_session(self) -> None:
        """close() should not raise when session is already None."""
        client = _make_client()
        await client.close()  # should not raise


# ---------------------------------------------------------------------------
# SSL config
# ---------------------------------------------------------------------------


class TestSslConfig:
    @pytest.mark.asyncio
    async def test_verify_ssl_false_uses_no_ssl(self) -> None:
        with (
            patch("oasisagent.clients.unifi.aiohttp.ClientSession") as mock_cls,
            patch("oasisagent.clients.unifi.aiohttp.TCPConnector") as mock_connector_cls,
        ):
            mock_cls.return_value = AsyncMock()

            client = _make_client(verify_ssl=False)
            client._authenticate = AsyncMock()  # type: ignore[method-assign]
            await client.connect()

            mock_connector_cls.assert_called_once_with(ssl=False)

    @pytest.mark.asyncio
    async def test_verify_ssl_true_uses_default(self) -> None:
        with (
            patch("oasisagent.clients.unifi.aiohttp.ClientSession") as mock_cls,
            patch("oasisagent.clients.unifi.aiohttp.TCPConnector") as mock_connector_cls,
        ):
            mock_cls.return_value = AsyncMock()

            client = _make_client(verify_ssl=True)
            client._authenticate = AsyncMock()  # type: ignore[method-assign]
            await client.connect()

            mock_connector_cls.assert_called_once_with(ssl=True)
