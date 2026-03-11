"""Tests for the FastAPI application scaffold."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, PropertyMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from oasisagent.web.app import _get_version, create_app

# ---------------------------------------------------------------------------
# Test app with mocked orchestrator (no real components)
# ---------------------------------------------------------------------------


def _mock_orchestrator() -> MagicMock:
    """Create a mock orchestrator with the attributes the API reads."""
    orch = MagicMock()
    orch._events_processed = 42
    orch._actions_taken = 7
    orch._errors = 1
    orch._queue = MagicMock()
    type(orch._queue).size = PropertyMock(return_value=3)
    return orch


@pytest.fixture
async def client() -> AsyncClient:
    """Create a test client with a mocked orchestrator in app state."""
    app = create_app()
    # Inject mock state directly — bypasses the real lifespan which would
    # try to load config and start real components.
    app.state.orchestrator = _mock_orchestrator()
    app.state.start_time = time.monotonic()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    async def test_healthz_returns_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    async def test_healthz_includes_version(self, client: AsyncClient) -> None:
        resp = await client.get("/healthz")
        data = resp.json()
        assert data["version"] == _get_version()


# ---------------------------------------------------------------------------
# API status endpoint
# ---------------------------------------------------------------------------


class TestAPIStatus:
    async def test_status_returns_agent_stats(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["events_processed"] == 42
        assert data["actions_taken"] == 7
        assert data["errors"] == 1
        assert data["queue_depth"] == 3
        assert "uptime_seconds" in data


# ---------------------------------------------------------------------------
# Webhook (no config store → 503)
# ---------------------------------------------------------------------------


class TestWebhookNoStore:
    async def test_webhook_without_store_returns_503(self, client: AsyncClient) -> None:
        resp = await client.post("/ingest/webhook/radarr", json={"eventType": "Test"})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# UI placeholder
# ---------------------------------------------------------------------------


class TestRootRedirect:
    async def test_root_redirects_to_dashboard(self, client: AsyncClient) -> None:
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/dashboard"


# ---------------------------------------------------------------------------
# Metrics endpoint
# ---------------------------------------------------------------------------


class TestMetrics:
    async def test_metrics_returns_prometheus_format(self, client: AsyncClient) -> None:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"] or \
               "text/plain" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------


class TestNotFound:
    async def test_unknown_route_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# create_app factory
# ---------------------------------------------------------------------------


class TestAppFactory:
    def test_create_app_returns_fastapi(self) -> None:
        app = create_app()
        assert isinstance(app, FastAPI)
        assert app.title == "OasisAgent"

    def test_get_version_returns_string(self) -> None:
        version = _get_version()
        assert isinstance(version, str)
        assert len(version) > 0
