"""Tests for HTMX 401 handling — session-expired redirects.

Verifies that the setup_guard_middleware correctly handles 401 responses
for both HTMX and non-HTMX requests on UI paths:
- HTMX requests receive an HX-Redirect header (client-side redirect)
- Non-HTMX requests receive a 303 redirect to the login page
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from oasisagent.db.config_store import ConfigStore
from oasisagent.db.crypto import CryptoProvider
from oasisagent.db.schema import run_migrations
from oasisagent.ui.auth import derive_jwt_key
from oasisagent.web.app import create_app


def _mock_orchestrator() -> MagicMock:
    orch = MagicMock()
    orch._events_processed = 0
    orch._actions_taken = 0
    orch._errors = 0
    orch._queue = MagicMock()
    type(orch._queue).size = PropertyMock(return_value=0)
    return orch


@pytest.fixture
async def app_and_store(tmp_path: Path) -> tuple:
    """Create app with real config store for auth testing."""
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    db = await run_migrations(tmp_path / "test.db")
    store = ConfigStore(db, crypto)

    # Create admin user so setup guard allows requests through
    await store.create_user("admin", "hashed", role="admin")

    app = create_app()
    app.state.config_store = store
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.orchestrator = _mock_orchestrator()
    app.state.jwt_signing_key = derive_jwt_key(key)

    return app, store, key


@pytest.fixture
async def client(app_and_store: tuple) -> AsyncClient:
    """Test client without auth cookies — simulates expired session."""
    app, _store, _key = app_and_store
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


class TestHtmx401Redirect:
    """Middleware returns HX-Redirect on 401 for HTMX requests."""

    async def test_htmx_health_poll_gets_hx_redirect(
        self, client: AsyncClient,
    ) -> None:
        """Health poll with expired session gets HX-Redirect header."""
        resp = await client.get(
            "/ui/connectors/health",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 401
        assert resp.headers.get("hx-redirect") == "/ui/login"

    async def test_htmx_unread_count_gets_hx_redirect(
        self, client: AsyncClient,
    ) -> None:
        """Notification badge poll with expired session gets HX-Redirect."""
        resp = await client.get(
            "/ui/notifications/unread-count",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 401
        assert resp.headers.get("hx-redirect") == "/ui/login"

    async def test_htmx_post_gets_hx_redirect(
        self, client: AsyncClient,
    ) -> None:
        """HTMX POST with expired session gets HX-Redirect."""
        resp = await client.post(
            "/ui/connectors/1/toggle",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 401
        assert resp.headers.get("hx-redirect") == "/ui/login"


class TestNonHtmx401Redirect:
    """Non-HTMX UI requests get a 303 redirect to login."""

    async def test_browser_navigation_redirects_to_login(
        self, client: AsyncClient,
    ) -> None:
        """Normal browser request with expired session redirects to login."""
        resp = await client.get(
            "/ui/dashboard",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/login"

    async def test_connectors_page_redirects_to_login(
        self, client: AsyncClient,
    ) -> None:
        """Connectors page with no session redirects to login."""
        resp = await client.get(
            "/ui/connectors",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/login"


class TestApiUnaffected:
    """API endpoints should NOT get redirect treatment."""

    async def test_api_returns_raw_401(self, client: AsyncClient) -> None:
        """API endpoints return raw 401 without HX-Redirect."""
        resp = await client.get("/api/v1/status")
        # API status doesn't require auth, but other endpoints that
        # return 401 should not get the UI redirect treatment.
        # The middleware only intercepts /ui/ paths.
        assert "hx-redirect" not in resp.headers
