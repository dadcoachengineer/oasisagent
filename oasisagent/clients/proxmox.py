"""Proxmox VE API client.

Token-based authentication using PVE API tokens (PVEAPIToken header).
Read-only client for polling cluster status, node resources, VM states,
replication jobs, and tasks.

Used by the Proxmox ingestion adapter to poll for events.
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class ProxmoxClient:
    """HTTP client for the Proxmox VE API.

    Args:
        url: PVE base URL (e.g., ``https://192.168.1.106:8006``).
        user: PVE user (e.g., ``root@pam``).
        token_name: API token name.
        token_value: API token secret value.
        verify_ssl: Whether to verify SSL certificates.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        url: str,
        user: str,
        token_name: str,
        token_value: str,
        *,
        verify_ssl: bool = False,
        timeout: int = 10,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._user = user
        self._token_name = token_name
        self._token_value = token_value
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def start(self) -> None:
        """Create the HTTP session with PVEAPIToken auth header."""
        ssl_context: bool = self._verify_ssl or False
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={
                "Authorization": (
                    f"PVEAPIToken={self._user}!{self._token_name}"
                    f"={self._token_value}"
                ),
            },
        )

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def healthy(self) -> bool:
        """Check connectivity by hitting GET /api2/json/version."""
        if self._session is None:
            return False
        try:
            async with self._session.get(
                f"{self._base_url}/api2/json/version",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    # -----------------------------------------------------------------
    # Requests
    # -----------------------------------------------------------------

    async def get(self, path: str, **params: object) -> dict[str, object] | list[object] | object:
        """GET a JSON endpoint and unwrap the ``{"data": ...}`` envelope.

        Args:
            path: API path (e.g., ``/api2/json/cluster/status``).
            **params: Query parameters.

        Returns:
            The ``data`` value from the response, or the raw body if
            no ``data`` key is present.

        Raises:
            RuntimeError: If the client has not been started.
            aiohttp.ClientResponseError: On non-2xx responses.
        """
        if self._session is None:
            msg = "Proxmox client not started — call start() first"
            raise RuntimeError(msg)

        url = f"{self._base_url}{path}"

        async with self._session.get(url, params=params or None) as resp:
            if resp.status >= 400:
                body = await resp.text()
                msg = f"Proxmox GET {path} failed (HTTP {resp.status}): {body[:200]}"
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=msg,
                )
            body = await resp.json(content_type=None)
            if isinstance(body, dict):
                return body.get("data", body)
            return body
