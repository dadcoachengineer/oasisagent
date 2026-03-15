"""Frigate NVR HTTP client.

Thin wrapper around Frigate's REST API for fetching camera stats
and detection events. Frigate's API is unauthenticated by default.

Endpoints used:
- ``GET /api/stats`` — per-camera FPS, process info, detector stats
- ``GET /api/events`` — recent object detection events
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class FrigateClient:
    """Async HTTP client for Frigate NVR API."""

    def __init__(
        self, url: str, timeout: int = 10, *, verify_ssl: bool = True,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._verify_ssl = verify_ssl
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Create the HTTP session and verify connectivity."""
        connector = aiohttp.TCPConnector(ssl=self._verify_ssl)
        self._session = aiohttp.ClientSession(
            timeout=self._timeout, connector=connector,
        )
        # Verify connectivity with a stats call
        async with self._session.get(f"{self._base_url}/api/stats") as resp:
            resp.raise_for_status()

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_stats(self) -> dict[str, Any]:
        """Fetch ``/api/stats`` — camera and detector statistics.

        Returns the raw JSON dict from Frigate.
        """
        if self._session is None:
            msg = "Client not started"
            raise RuntimeError(msg)

        async with self._session.get(f"{self._base_url}/api/stats") as resp:
            resp.raise_for_status()
            return await resp.json()  # type: ignore[no-any-return]

    async def get_events(
        self, after: float, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch ``/api/events`` — recent detection events.

        Args:
            after: Unix timestamp. Only events after this time are returned.
            limit: Maximum number of events to return.

        Returns:
            List of event dicts from Frigate.
        """
        if self._session is None:
            msg = "Client not started"
            raise RuntimeError(msg)

        params = {"after": str(after), "limit": str(limit)}
        async with self._session.get(
            f"{self._base_url}/api/events", params=params,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()  # type: ignore[no-any-return]
