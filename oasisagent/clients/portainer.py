"""Portainer API client.

X-API-Key authentication. Two request surfaces:
- Native Portainer API (``GET /api/...``)
- Docker Engine API proxied through endpoints (``GET /api/endpoints/{id}/docker/...``)

Used by the Portainer ingestion adapter to poll for events.
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class PortainerClient:
    """HTTP client for the Portainer REST API.

    Args:
        url: Portainer base URL (e.g., ``https://192.168.1.120:9443``).
        api_key: API key for X-API-Key header auth.
        verify_ssl: Whether to verify SSL certificates.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify_ssl: bool = False,
        timeout: int = 10,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def start(self) -> None:
        """Create the HTTP session with X-API-Key auth header."""
        ssl_context: bool = self._verify_ssl or False
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={"X-API-Key": self._api_key},
        )

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def healthy(self) -> bool:
        """Check connectivity by hitting GET /api/status."""
        if self._session is None:
            return False
        try:
            async with self._session.get(
                f"{self._base_url}/api/status",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    # -----------------------------------------------------------------
    # Requests
    # -----------------------------------------------------------------

    async def get(self, path: str) -> dict[str, object] | list[object] | object:
        """GET a Portainer native API endpoint and return JSON.

        Args:
            path: API path (e.g., ``/api/endpoints``).

        Returns:
            Parsed JSON response (Portainer returns raw JSON, no envelope).

        Raises:
            RuntimeError: If the client has not been started.
            aiohttp.ClientResponseError: On non-2xx responses.
        """
        if self._session is None:
            msg = "Portainer client not started — call start() first"
            raise RuntimeError(msg)

        url = f"{self._base_url}{path}"

        async with self._session.get(url) as resp:
            if resp.status >= 400:
                body = await resp.text()
                msg = f"Portainer GET {path} failed (HTTP {resp.status}): {body[:200]}"
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=msg,
                )
            return await resp.json(content_type=None)

    async def get_docker(
        self, endpoint_id: int, path: str, **params: object,
    ) -> dict[str, object] | list[object] | object:
        """GET a Docker Engine API endpoint proxied through Portainer.

        Args:
            endpoint_id: Portainer environment/endpoint ID.
            path: Docker API path (e.g., ``containers/json``).
            **params: Query parameters.

        Returns:
            Parsed JSON response.

        Raises:
            RuntimeError: If the client has not been started.
            aiohttp.ClientResponseError: On non-2xx responses.
        """
        if self._session is None:
            msg = "Portainer client not started — call start() first"
            raise RuntimeError(msg)

        full_path = f"/api/endpoints/{endpoint_id}/docker/{path}"
        url = f"{self._base_url}{full_path}"

        async with self._session.get(url, params=params or None) as resp:
            if resp.status >= 400:
                body = await resp.text()
                msg = (
                    f"Portainer GET {full_path} failed "
                    f"(HTTP {resp.status}): {body[:200]}"
                )
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=msg,
                )
            return await resp.json(content_type=None)
