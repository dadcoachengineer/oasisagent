"""Tests for the config CRUD REST API endpoints."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, PropertyMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from oasisagent.db.config_store import ConfigStore
from oasisagent.db.crypto import CryptoProvider
from oasisagent.db.schema import run_migrations
from oasisagent.web.app import create_app

if TYPE_CHECKING:
    from pathlib import Path


def _mock_orchestrator() -> MagicMock:
    orch = MagicMock()
    orch._events_processed = 0
    orch._actions_taken = 0
    orch._errors = 0
    orch._queue = MagicMock()
    type(orch._queue).size = PropertyMock(return_value=0)
    return orch


@pytest.fixture
async def client(tmp_path: Path) -> AsyncClient:
    """Test client with real SQLite database (no mocked store)."""
    db_path = tmp_path / "test.db"
    db = await run_migrations(db_path)
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    store = ConfigStore(db, crypto)

    app = create_app()
    app.state.orchestrator = _mock_orchestrator()
    app.state.config_store = store
    app.state.db = db
    app.state.start_time = time.monotonic()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]

    await db.close()


class TestConnectorAPI:
    async def test_list_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/connectors/")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_connector(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/connectors/", json={
            "type": "mqtt",
            "name": "mqtt-test",
            "config": {"broker": "mqtt://test:1883", "password": "secret"},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["type"] == "mqtt"
        assert data["name"] == "mqtt-test"
        assert data["config"]["broker"] == "mqtt://test:1883"
        assert data["config"]["password"] == "********"  # masked

    async def test_get_connector(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/v1/connectors/", json={
            "type": "mqtt",
            "name": "mqtt-get",
            "config": {"broker": "mqtt://test:1883"},
        })
        row_id = create_resp.json()["id"]

        resp = await client.get(f"/api/v1/connectors/{row_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "mqtt-get"

    async def test_update_connector(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/v1/connectors/", json={
            "type": "mqtt",
            "name": "mqtt-upd",
            "config": {"broker": "mqtt://old:1883"},
        })
        row_id = create_resp.json()["id"]

        resp = await client.put(f"/api/v1/connectors/{row_id}", json={
            "config": {"broker": "mqtt://new:1883"},
        })
        assert resp.status_code == 200
        assert resp.json()["config"]["broker"] == "mqtt://new:1883"

    async def test_delete_connector(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/v1/connectors/", json={
            "type": "mqtt",
            "name": "mqtt-del",
            "config": {"broker": "mqtt://test:1883"},
        })
        row_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/v1/connectors/{row_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/api/v1/connectors/{row_id}")
        assert resp.status_code == 404

    async def test_create_invalid_type(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/connectors/", json={
            "type": "nonexistent",
            "name": "bad",
            "config": {},
        })
        assert resp.status_code == 422

    async def test_create_duplicate_name(self, client: AsyncClient) -> None:
        await client.post("/api/v1/connectors/", json={
            "type": "mqtt",
            "name": "mqtt-dup",
            "config": {"broker": "mqtt://a:1883"},
        })
        resp = await client.post("/api/v1/connectors/", json={
            "type": "mqtt",
            "name": "mqtt-dup",
            "config": {"broker": "mqtt://b:1883"},
        })
        assert resp.status_code == 409

    async def test_get_nonexistent(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/connectors/999")
        assert resp.status_code == 404

    async def test_delete_nonexistent(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/v1/connectors/999")
        assert resp.status_code == 404


class TestServiceAPI:
    async def test_create_and_list(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/services/", json={
            "type": "llm_triage",
            "name": "triage-ollama",
            "config": {
                "base_url": "http://ollama:11434/v1",
                "model": "qwen2.5:7b",
                "api_key": "key123",
            },
        })
        assert resp.status_code == 201
        assert resp.json()["config"]["api_key"] == "********"

        resp = await client.get("/api/v1/services/")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestNotificationAPI:
    async def test_create_webhook(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/notifications/", json={
            "type": "webhook",
            "name": "discord-hook",
            "config": {"urls": ["https://discord.com/api/webhooks/123"]},
        })
        assert resp.status_code == 201
        assert resp.json()["config"]["urls"] == ["https://discord.com/api/webhooks/123"]
