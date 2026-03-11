"""Tests for the first-run setup wizard API and middleware."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from oasisagent.db.config_store import ConfigStore
from oasisagent.db.crypto import CryptoProvider
from oasisagent.db.schema import run_migrations
from oasisagent.web.app import create_app


@pytest.fixture
async def setup_client(tmp_path: Path) -> AsyncClient:
    """Create a test client with a real config store but mocked orchestrator.

    The database starts empty (no admin user), so the app is in setup mode.
    """
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    db = await run_migrations(tmp_path / "test.db")
    store = ConfigStore(db, crypto)

    app = create_app()
    # Inject state directly — bypass lifespan
    app.state.config_store = store
    app.state.db = db
    app.state.start_time = time.monotonic()

    # Mock orchestrator for /api/v1/status
    from unittest.mock import MagicMock, PropertyMock

    orch = MagicMock()
    orch._events_processed = 0
    orch._actions_taken = 0
    orch._errors = 0
    orch._queue = MagicMock()
    type(orch._queue).size = PropertyMock(return_value=0)
    app.state.orchestrator = orch

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]

    await db.close()


# ---------------------------------------------------------------------------
# Setup status
# ---------------------------------------------------------------------------


class TestSetupStatus:
    async def test_status_returns_setup_required(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/api/v1/setup/status")
        assert resp.status_code == 200
        assert resp.json()["setup_required"] is True

    async def test_status_after_admin_created(self, setup_client: AsyncClient) -> None:
        # Create admin first
        await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "securepass123"},
        )
        resp = await setup_client.get("/api/v1/setup/status")
        assert resp.status_code == 200
        assert resp.json()["setup_required"] is False


# ---------------------------------------------------------------------------
# Admin creation
# ---------------------------------------------------------------------------


class TestAdminCreation:
    async def test_create_admin_success(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "securepass123"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "admin"
        assert data["is_admin"] is True
        assert "id" in data

    async def test_create_admin_with_totp(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/api/v1/setup/admin",
            json={
                "username": "admin",
                "password": "securepass123",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )
        assert resp.status_code == 201

    async def test_create_admin_rejects_second_admin(self, setup_client: AsyncClient) -> None:
        # First admin
        await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin1", "password": "securepass123"},
        )
        # Second attempt
        resp = await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin2", "password": "securepass456"},
        )
        assert resp.status_code == 409
        assert "already complete" in resp.json()["detail"].lower()

    async def test_create_admin_short_password(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "short"},
        )
        assert resp.status_code == 422  # Pydantic validation

    async def test_create_admin_empty_username(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "", "password": "securepass123"},
        )
        assert resp.status_code == 422

    async def test_create_admin_rejects_extra_fields(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "securepass123", "role": "superadmin"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Core services configuration
# ---------------------------------------------------------------------------


class TestCoreServices:
    async def test_setup_core_services_defaults(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/api/v1/setup/core-services",
            json={},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "mqtt_connector_id" in data
        assert "influxdb_service_id" in data

    async def test_setup_core_services_custom(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/api/v1/setup/core-services",
            json={
                "mqtt_broker": "mqtt://mybroker:1883",
                "mqtt_username": "user",
                "mqtt_password": "pass",
                "influxdb_url": "http://influx:8086",
                "influxdb_token": "mytoken",
                "influxdb_org": "myorg",
                "influxdb_bucket": "mybucket",
            },
        )
        assert resp.status_code == 201

    async def test_setup_core_services_idempotent(self, setup_client: AsyncClient) -> None:
        """Calling core-services twice replaces existing entries."""
        resp1 = await setup_client.post(
            "/api/v1/setup/core-services",
            json={"mqtt_broker": "mqtt://first:1883"},
        )
        assert resp1.status_code == 201
        id1 = resp1.json()["mqtt_connector_id"]

        resp2 = await setup_client.post(
            "/api/v1/setup/core-services",
            json={"mqtt_broker": "mqtt://second:1883"},
        )
        assert resp2.status_code == 201
        id2 = resp2.json()["mqtt_connector_id"]
        # New ID means old was deleted and new was created
        assert id2 != id1


# ---------------------------------------------------------------------------
# Setup completion
# ---------------------------------------------------------------------------


class TestCompleteSetup:
    async def test_complete_without_admin_fails(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post("/api/v1/setup/complete")
        assert resp.status_code == 400
        assert "no admin" in resp.json()["detail"].lower()

    async def test_complete_with_admin_succeeds(self, setup_client: AsyncClient) -> None:
        # Create admin
        await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "securepass123"},
        )
        resp = await setup_client.post("/api/v1/setup/complete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "complete"


# ---------------------------------------------------------------------------
# Full setup flow
# ---------------------------------------------------------------------------


class TestFullSetupFlow:
    async def test_end_to_end_setup(self, setup_client: AsyncClient) -> None:
        """Walk through the entire setup wizard flow."""
        # Step 0: Check status
        resp = await setup_client.get("/api/v1/setup/status")
        assert resp.json()["setup_required"] is True

        # Step 1: Create admin
        resp = await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "securepass123"},
        )
        assert resp.status_code == 201

        # Step 2: Configure core services
        resp = await setup_client.post(
            "/api/v1/setup/core-services",
            json={"mqtt_broker": "mqtt://mybroker:1883"},
        )
        assert resp.status_code == 201

        # Step 3: Complete setup
        resp = await setup_client.post("/api/v1/setup/complete")
        assert resp.status_code == 200

        # Verify: status shows setup is done
        resp = await setup_client.get("/api/v1/setup/status")
        assert resp.json()["setup_required"] is False

        # Verify: non-setup endpoints are now accessible
        resp = await setup_client.get("/api/v1/status")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Setup guard middleware
# ---------------------------------------------------------------------------


class TestSetupGuardMiddleware:
    async def test_blocks_api_during_setup(self, setup_client: AsyncClient) -> None:
        """Non-setup API endpoints return 403 when no admin exists."""
        resp = await setup_client.get("/api/v1/status")
        assert resp.status_code == 403
        assert "setup required" in resp.json()["detail"].lower()

    async def test_blocks_config_endpoints_during_setup(
        self, setup_client: AsyncClient,
    ) -> None:
        resp = await setup_client.get("/api/v1/connectors")
        assert resp.status_code == 403

    async def test_allows_setup_endpoints(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/api/v1/setup/status")
        assert resp.status_code == 200

    async def test_allows_healthz(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/healthz")
        assert resp.status_code == 200

    async def test_allows_root(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/")
        assert resp.status_code == 200

    async def test_allows_docs(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/docs")
        # FastAPI docs redirect or serve HTML
        assert resp.status_code in (200, 307)

    async def test_allows_openapi_json(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/openapi.json")
        assert resp.status_code == 200

    async def test_unblocks_after_admin_created(self, setup_client: AsyncClient) -> None:
        """Once admin exists, non-setup endpoints work again."""
        # Blocked before
        resp = await setup_client.get("/api/v1/status")
        assert resp.status_code == 403

        # Create admin
        await setup_client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "securepass123"},
        )

        # Unblocked after
        resp = await setup_client.get("/api/v1/status")
        assert resp.status_code == 200
