"""Nginx Proxy Manager API client.

JWT-based authentication: POST /api/tokens with email/password returns
a JWT token used as ``Authorization: Bearer <token>``.  Tokens expire
after a configurable period; the client transparently refreshes on
401 or 403.

Used by the NPM ingestion adapter to poll proxy hosts, certificates,
and dead hosts.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class NpmClient:
    """HTTP client for the Nginx Proxy Manager API.

    Args:
        url: NPM base URL (e.g., ``http://192.168.1.50:81``).
        email: Admin email for authentication.
        password: Admin password for authentication.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        url: str,
        email: str,
        password: str,
        *,
        timeout: int = 10,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._email = email
        self._password = password
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def connect(self) -> None:
        """Create the HTTP session and authenticate."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        )
        await self._authenticate()

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._token = None

    async def healthy(self) -> bool:
        """Check if the client has a valid session and token."""
        if self._session is None or self._token is None:
            return False
        try:
            await self.get("/api/nginx/proxy-hosts?limit=1")
            return True
        except Exception:
            return False

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------

    async def _authenticate(self) -> None:
        """POST credentials to /api/tokens to obtain a JWT."""
        assert self._session is not None

        url = f"{self._base_url}/api/tokens"
        payload = {"identity": self._email, "secret": self._password}

        async with self._session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._token = None
                msg = f"NPM login failed (HTTP {resp.status}): {body[:200]}"
                raise ConnectionError(msg)

            data = await resp.json()
            self._token = data.get("token") or data.get("access_token", "")
            if not self._token:
                msg = "NPM login response missing token"
                raise ConnectionError(msg)

        logger.info("NPM client: authenticated to %s", self._base_url)

    # -----------------------------------------------------------------
    # Requests
    # -----------------------------------------------------------------

    async def get(self, path: str) -> dict[str, Any] | list[dict[str, Any]]:
        """GET a JSON endpoint. Retries once on 401 (token refresh).

        Returns the parsed JSON response body.
        Raises on non-2xx after retry.
        """
        if self._session is None:
            msg = "NPM client not connected -- call connect() first"
            raise RuntimeError(msg)

        url = f"{self._base_url}{path}"
        headers = self._auth_headers()

        async with self._session.get(url, headers=headers) as resp:
            if resp.status in (401, 403):
                # Token expired -- re-authenticate and retry once
                await self._authenticate()
                headers = self._auth_headers()
                async with self._session.get(url, headers=headers) as retry_resp:
                    if retry_resp.status >= 400:
                        body = await retry_resp.text()
                        msg = (
                            f"NPM GET {path} failed after re-auth"
                            f" (HTTP {retry_resp.status}): {body[:200]}"
                        )
                        raise aiohttp.ClientResponseError(
                            retry_resp.request_info, retry_resp.history,
                            status=retry_resp.status, message=msg,
                        )
                    return await retry_resp.json()

            if resp.status >= 400:
                body = await resp.text()
                msg = f"NPM GET {path} failed (HTTP {resp.status}): {body[:200]}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            return await resp.json()

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Build authorization headers with current token."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}
