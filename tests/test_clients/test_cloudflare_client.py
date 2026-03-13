"""Tests for the Cloudflare API v4 HTTP client."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from oasisagent.clients.cloudflare import _GRAPHQL_URL, CloudflareClient


def _make_client(**overrides: object) -> CloudflareClient:
    defaults: dict[str, object] = {
        "api_token": "test-token-123",
        "timeout": 10,
    }
    defaults.update(overrides)
    return CloudflareClient(**defaults)  # type: ignore[arg-type]


def _mock_resp(
    status: int = 200,
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock aiohttp response for use in async context managers."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {"success": True, "result": []})
    resp.request_info = MagicMock()
    resp.history = ()
    return resp


def _mock_session_with(
    method: str, resp: MagicMock,
) -> tuple[MagicMock, CloudflareClient]:
    """Create a client with a mocked session returning resp for the given method."""
    client = _make_client()
    mock_session = MagicMock()

    @asynccontextmanager
    async def _ctx_manager(*args: object, **kwargs: object) -> Any:  # noqa: ANN401
        yield resp

    setattr(mock_session, method, _ctx_manager)
    client._session = mock_session
    return mock_session, client


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_session(self) -> None:
        with patch(
            "oasisagent.clients.cloudflare.aiohttp.ClientSession",
        ) as mock_cls:
            mock_cls.return_value = AsyncMock()

            client = _make_client()
            await client.start()

            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args[1]
            assert "Bearer test-token-123" in str(call_kwargs["headers"])

    @pytest.mark.asyncio
    async def test_close_clears_session(self) -> None:
        client = _make_client()
        mock_session = AsyncMock()
        client._session = mock_session

        await client.close()

        mock_session.close.assert_called_once()
        assert client._session is None

    @pytest.mark.asyncio
    async def test_close_when_no_session(self) -> None:
        client = _make_client()
        await client.close()  # should not raise


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_get_returns_json(self) -> None:
        resp = _mock_resp(200, {
            "success": True,
            "result": [{"id": "t1", "status": "active"}],
        })
        _, client = _mock_session_with("get", resp)

        result = await client.get("/accounts/abc/cfd_tunnel")

        assert result["success"] is True
        assert len(result["result"]) == 1

    @pytest.mark.asyncio
    async def test_get_raises_on_http_error(self) -> None:
        resp = _mock_resp(403, {
            "success": False,
            "errors": [{"code": 9109, "message": "Invalid access token"}],
        })
        _, client = _mock_session_with("get", resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.get("/accounts/abc/cfd_tunnel")

    @pytest.mark.asyncio
    async def test_get_raises_on_api_error(self) -> None:
        """success=false with 200 status should still raise."""
        resp = _mock_resp(200, {
            "success": False,
            "errors": [{"code": 1001, "message": "Invalid zone"}],
        })
        _, client = _mock_session_with("get", resp)

        with pytest.raises(aiohttp.ClientResponseError, match="API error"):
            await client.get("/zones/z1/dns_records")

    @pytest.mark.asyncio
    async def test_get_not_started_raises(self) -> None:
        client = _make_client()
        with pytest.raises(RuntimeError, match="not started"):
            await client.get("/test")


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


class TestPost:
    @pytest.mark.asyncio
    async def test_post_returns_json(self) -> None:
        resp = _mock_resp(200, {
            "success": True,
            "result": {"id": "purge-1"},
        })
        _, client = _mock_session_with("post", resp)

        result = await client.post(
            "/zones/z1/purge_cache",
            {"purge_everything": True},
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_post_raises_on_error(self) -> None:
        resp = _mock_resp(400, {
            "success": False,
            "errors": [{"message": "Bad request"}],
        })
        _, client = _mock_session_with("post", resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.post("/zones/z1/purge_cache", {})

    @pytest.mark.asyncio
    async def test_post_raises_on_api_error(self) -> None:
        """success=false with 200 status should raise on POST."""
        resp = _mock_resp(200, {
            "success": False,
            "errors": [{"message": "Invalid purge request"}],
        })
        _, client = _mock_session_with("post", resp)

        with pytest.raises(aiohttp.ClientResponseError, match="API error"):
            await client.post("/zones/z1/purge_cache", {})

    @pytest.mark.asyncio
    async def test_post_not_started_raises(self) -> None:
        client = _make_client()
        with pytest.raises(RuntimeError, match="not started"):
            await client.post("/test", {})


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        resp = _mock_resp(200, {
            "success": True,
            "result": {"id": "rule-1"},
        })
        _, client = _mock_session_with("delete", resp)

        result = await client.delete("/zones/z1/firewall/rules/rule-1")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_delete_raises_on_error(self) -> None:
        resp = _mock_resp(404, {
            "success": False,
            "errors": [{"message": "Not found"}],
        })
        _, client = _mock_session_with("delete", resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.delete("/zones/z1/firewall/rules/bad-id")

    @pytest.mark.asyncio
    async def test_delete_raises_on_api_error(self) -> None:
        """success=false with 200 status should raise on DELETE."""
        resp = _mock_resp(200, {
            "success": False,
            "errors": [{"message": "Cannot delete active rule"}],
        })
        _, client = _mock_session_with("delete", resp)

        with pytest.raises(aiohttp.ClientResponseError, match="API error"):
            await client.delete("/zones/z1/firewall/rules/rule-1")

    @pytest.mark.asyncio
    async def test_delete_not_started_raises(self) -> None:
        client = _make_client()
        with pytest.raises(RuntimeError, match="not started"):
            await client.delete("/test")


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------


class TestGraphql:
    @pytest.mark.asyncio
    async def test_graphql_returns_data(self) -> None:
        gql_resp = {
            "data": {"viewer": {"zones": [{"firewallEventsAdaptive": []}]}},
            "errors": None,
        }
        resp = _mock_resp(200, gql_resp)
        client = _make_client()
        mock_session = MagicMock()
        captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        @asynccontextmanager
        async def _tracking_post(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            captured.append((args, kwargs))
            yield resp

        mock_session.post = _tracking_post
        client._session = mock_session

        result = await client.graphql("{ viewer { zones { id } } }")

        assert result == gql_resp
        assert len(captured) == 1
        assert captured[0][0][0] == _GRAPHQL_URL
        assert captured[0][1]["json"] == {"query": "{ viewer { zones { id } } }"}

    @pytest.mark.asyncio
    async def test_graphql_raises_on_http_error(self) -> None:
        resp = _mock_resp(403, {"errors": [{"message": "Forbidden"}]})
        _, client = _mock_session_with("post", resp)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.graphql("{ viewer { zones { id } } }")

    @pytest.mark.asyncio
    async def test_graphql_raises_on_graphql_errors(self) -> None:
        resp = _mock_resp(200, {
            "data": None,
            "errors": [{"message": "Validation error"}],
        })
        _, client = _mock_session_with("post", resp)

        with pytest.raises(aiohttp.ClientResponseError, match="GraphQL errors"):
            await client.graphql("{ bad }")

    @pytest.mark.asyncio
    async def test_graphql_not_started_raises(self) -> None:
        client = _make_client()
        with pytest.raises(RuntimeError, match="not started"):
            await client.graphql("{ viewer { zones { id } } }")

    @pytest.mark.asyncio
    async def test_graphql_no_variables(self) -> None:
        """When variables is None, body should only contain 'query' key."""
        resp = _mock_resp(200, {"data": {}, "errors": None})
        client = _make_client()
        mock_session = MagicMock()
        captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        @asynccontextmanager
        async def _tracking_post(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            captured.append((args, kwargs))
            yield resp

        mock_session.post = _tracking_post
        client._session = mock_session

        await client.graphql("{ viewer { zones { id } } }")

        assert len(captured) == 1
        assert "variables" not in captured[0][1]["json"]
        assert captured[0][1]["json"] == {"query": "{ viewer { zones { id } } }"}
