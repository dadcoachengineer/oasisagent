"""UniFi Network controller HTTP client.

Handles session cookie authentication, automatic re-auth on 401/403,
and UDM/UCG path prefixing. Used by both the UniFi ingestion adapter
and the UniFi handler.

UniFi local controllers use POST /api/auth/login returning a session
cookie — not a stateless token. Sessions expire unpredictably, so
the client retries exactly once on 401 or 403 (re-authenticate then replay).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl

import aiohttp

logger = logging.getLogger(__name__)


class UnifiClient:
    """HTTP client for UniFi Network controller API.

    Args:
        url: Controller base URL (e.g., https://192.168.1.1).
        username: Controller username.
        password: Controller password.
        site: UniFi site name (default: "default").
        is_udm: Whether the controller is a UDM/UCG device
                 (adds /proxy/network prefix to API paths).
        verify_ssl: Whether to verify SSL certificates.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        site: str = "default",
        *,
        is_udm: bool = True,
        verify_ssl: bool = False,
        timeout: int = 10,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._username = username
        self._password = password
        self._site = site
        self._is_udm = is_udm
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False

        self._validate_site_name(site)

    @property
    def site(self) -> str:
        """The configured UniFi site name."""
        return self._site

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def connect(self) -> None:
        """Create the HTTP session and authenticate."""
        ssl_context: ssl.SSLContext | bool = True
        if not self._verify_ssl:
            ssl_context = False

        jar = aiohttp.CookieJar(unsafe=True)
        self._session = aiohttp.ClientSession(
            cookie_jar=jar,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            connector=aiohttp.TCPConnector(ssl=ssl_context),
        )
        await self._authenticate()

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._authenticated = False

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------

    async def _authenticate(self) -> None:
        """POST credentials to the login endpoint."""
        assert self._session is not None

        login_path = "/api/auth/login"
        if self._is_udm:
            login_url = f"{self._base_url}{login_path}"
        else:
            login_url = f"{self._base_url}/api/login"

        payload = {"username": self._username, "password": self._password}

        async with self._session.post(login_url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._authenticated = False
                msg = f"UniFi login failed (HTTP {resp.status}): {body[:200]}"
                raise ConnectionError(msg)

        self._authenticated = True
        logger.info("UniFi: authenticated to %s", self._base_url)

    # -----------------------------------------------------------------
    # Request
    # -----------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> aiohttp.ClientResponse:
        """Make an authenticated request. Retries exactly once on 401 or 403.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            path: API path relative to site (e.g., "stat/device").
            json: Optional JSON body.

        Returns:
            The aiohttp response (caller must close or use as context manager).
        """
        if self._session is None:
            msg = "UniFi client not connected — call connect() first"
            raise RuntimeError(msg)

        url = self._build_url(path)

        resp = await self._session.request(method, url, json=json)
        if resp.status in (401, 403):
            resp.release()
            await self._authenticate()
            resp = await self._session.request(method, url, json=json)

        return resp

    async def get(self, path: str) -> dict[str, Any]:
        """GET a JSON endpoint. Returns parsed response body.

        Raises on non-2xx response.
        """
        resp = await self.request("GET", path)
        try:
            if resp.status >= 400:
                body = await resp.text()
                msg = f"UniFi GET {path} failed (HTTP {resp.status}): {body[:200]}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            return await resp.json(content_type=None)
        finally:
            resp.release()

    async def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to an endpoint. Returns parsed response body.

        Raises on non-2xx response.
        """
        resp = await self.request("POST", path, json=json)
        try:
            if resp.status >= 400:
                body = await resp.text()
                msg = f"UniFi POST {path} failed (HTTP {resp.status}): {body[:200]}"
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=msg,
                )
            return await resp.json(content_type=None)
        finally:
            resp.release()

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _validate_site_name(site: str) -> None:
        """Warn if the site name looks like a display name instead of a site ID.

        UniFi site IDs are typically lowercase alphanumeric (e.g., "default").
        Display names often contain spaces or uppercase letters, which produce
        URLs like ``/api/s/Oasis%20UDMP/stat/device`` and fail with
        ``api.err.NoSiteContext``.
        """
        reasons: list[str] = []
        if " " in site:
            reasons.append("contains spaces")
        if site != site.lower():
            reasons.append("contains uppercase letters")

        if reasons:
            logger.warning(
                "UniFi site '%s' %s — this looks like a display name, not a "
                "site ID. Site IDs are typically lowercase alphanumeric "
                "(e.g., 'default').",
                site,
                " and ".join(reasons),
            )

    def _build_url(self, path: str) -> str:
        """Build the full URL with optional UDM prefix and site."""
        path = path.lstrip("/")
        if self._is_udm:
            return f"{self._base_url}/proxy/network/api/s/{self._site}/{path}"
        return f"{self._base_url}/api/s/{self._site}/{path}"
