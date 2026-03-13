"""Cloudflare API v4 HTTP client.

Bearer token authentication against https://api.cloudflare.com/client/v4/.
Used by both the Cloudflare ingestion adapter and handler.

Rate limit: 1200 requests per 5 minutes per user. The client does not
enforce this internally — callers should use appropriate poll intervals.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.cloudflare.com/client/v4/"


class CloudflareClient:
    """HTTP client for the Cloudflare API v4.

    Args:
        api_token: Cloudflare API token (scoped bearer token).
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        api_token: str,
        *,
        timeout: int = 30,
    ) -> None:
        self._api_token = api_token
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Create the HTTP session with bearer auth."""
        self._session = aiohttp.ClientSession(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        )
        logger.info("Cloudflare client started")

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get(self, path: str, **params: str) -> dict[str, Any]:
        """GET a JSON endpoint. Returns parsed response body.

        Raises on non-2xx or Cloudflare API errors.
        """
        if self._session is None:
            msg = "Cloudflare client not started — call start() first"
            raise RuntimeError(msg)

        async with self._session.get(path.lstrip("/"), params=params or None) as resp:
            data = await resp.json()
            if resp.status >= 400:
                errors = data.get("errors", [])
                msg = f"Cloudflare GET {path} failed (HTTP {resp.status}): {errors}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            if not data.get("success", True):
                errors = data.get("errors", [])
                msg = f"Cloudflare API error on {path}: {errors}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            return data

    async def post(
        self, path: str, json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST JSON to an endpoint. Returns parsed response body.

        Raises on non-2xx or Cloudflare API errors.
        """
        if self._session is None:
            msg = "Cloudflare client not started — call start() first"
            raise RuntimeError(msg)

        async with self._session.post(path.lstrip("/"), json=json or {}) as resp:
            data = await resp.json()
            if resp.status >= 400:
                errors = data.get("errors", [])
                msg = f"Cloudflare POST {path} failed (HTTP {resp.status}): {errors}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            if not data.get("success", True):
                errors = data.get("errors", [])
                msg = f"Cloudflare API error on POST {path}: {errors}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            return data

    async def delete(self, path: str) -> dict[str, Any]:
        """DELETE an endpoint. Returns parsed response body.

        Raises on non-2xx or Cloudflare API errors.
        """
        if self._session is None:
            msg = "Cloudflare client not started — call start() first"
            raise RuntimeError(msg)

        async with self._session.delete(path.lstrip("/")) as resp:
            data = await resp.json()
            if resp.status >= 400:
                errors = data.get("errors", [])
                msg = f"Cloudflare DELETE {path} failed (HTTP {resp.status}): {errors}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            if not data.get("success", True):
                errors = data.get("errors", [])
                msg = f"Cloudflare API error on DELETE {path}: {errors}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            return data
