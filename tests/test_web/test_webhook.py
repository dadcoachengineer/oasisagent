"""Tests for the webhook receiver ingestion adapter."""

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
from oasisagent.web.app import create_app


def _mock_orchestrator() -> MagicMock:
    orch = MagicMock()
    orch._events_processed = 0
    orch._actions_taken = 0
    orch._errors = 0
    orch._queue = MagicMock()
    type(orch._queue).size = PropertyMock(return_value=0)
    orch.enqueue = MagicMock()
    return orch


@pytest.fixture
async def webhook_client(tmp_path: Path) -> AsyncClient:
    """Test client with a real config store and mocked orchestrator."""
    key = Fernet.generate_key().decode()
    crypto = CryptoProvider(key)
    db = await run_migrations(tmp_path / "test.db")
    store = ConfigStore(db, crypto)

    # Create admin so middleware allows requests through
    await store.create_user("admin", "hashed", role="admin")

    app = create_app()
    app.state.config_store = store
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.orchestrator = _mock_orchestrator()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]

    await db.close()


async def _create_webhook_source(
    client: AsyncClient,
    name: str,
    config: dict,
) -> int:
    """Helper to create a webhook_receiver connector via CRUD API."""
    resp = await client.post(
        "/api/v1/connectors/",
        json={"type": "webhook_receiver", "name": name, "config": config},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Source lookup
# ---------------------------------------------------------------------------


class TestSourceLookup:
    async def test_unknown_source_returns_404(self, webhook_client: AsyncClient) -> None:
        resp = await webhook_client.post(
            "/ingest/webhook/nonexistent",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 404
        assert "unknown" in resp.json()["error"].lower()

    async def test_disabled_source_returns_404(self, webhook_client: AsyncClient) -> None:
        # Create as enabled, then disable via update
        store = webhook_client._transport.app.state.config_store  # type: ignore[union-attr]
        row_id = await store.create_connector(
            "webhook_receiver", "disabled-src", {"system": "test"}, enabled=True,
        )
        await store.update_connector(row_id, {"enabled": False})

        resp = await webhook_client.post(
            "/ingest/webhook/disabled-src",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth validation
# ---------------------------------------------------------------------------


class TestAuthValidation:
    async def test_no_auth_accepts_any(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "open-src", {
            "system": "test",
            "auth_mode": "none",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/open-src",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 202

    async def test_header_secret_valid(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "secure-src", {
            "system": "test",
            "auth_mode": "header_secret",
            "auth_header": "X-Webhook-Secret",
            "auth_secret": "mysecret123",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/secure-src",
            json={"eventType": "Test"},
            headers={"X-Webhook-Secret": "mysecret123"},
        )
        assert resp.status_code == 202

    async def test_header_secret_invalid(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "secure-src2", {
            "system": "test",
            "auth_mode": "header_secret",
            "auth_header": "X-Webhook-Secret",
            "auth_secret": "correct",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/secure-src2",
            json={"eventType": "Test"},
            headers={"X-Webhook-Secret": "wrong"},
        )
        assert resp.status_code == 401

    async def test_header_secret_missing(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "secure-src3", {
            "system": "test",
            "auth_mode": "header_secret",
            "auth_header": "X-Webhook-Secret",
            "auth_secret": "secret",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/secure-src3",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 401

    async def test_api_key_via_header(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "apikey-src", {
            "system": "test",
            "auth_mode": "api_key",
            "auth_secret": "key123",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/apikey-src",
            json={"eventType": "Test"},
            headers={"X-Api-Key": "key123"},
        )
        assert resp.status_code == 202

    async def test_api_key_via_query(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "apikey-src2", {
            "system": "test",
            "auth_mode": "api_key",
            "auth_secret": "key456",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/apikey-src2?apikey=key456",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 202

    async def test_api_key_invalid(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "apikey-src3", {
            "system": "test",
            "auth_mode": "api_key",
            "auth_secret": "correct",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/apikey-src3",
            json={"eventType": "Test"},
            headers={"X-Api-Key": "wrong"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    async def test_non_object_returns_400(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "payload-src", {
            "system": "test",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/payload-src",
            content=b"[1, 2, 3]",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert "object" in resp.json()["error"].lower()

    async def test_invalid_json_returns_400(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "payload-src2", {
            "system": "test",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/payload-src2",
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Event mapping
# ---------------------------------------------------------------------------


class TestEventMapping:
    async def test_mapped_event(self, webhook_client: AsyncClient) -> None:
        """Known event type uses configured mapping."""
        await _create_webhook_source(webhook_client, "mapped-src", {
            "system": "radarr",
            "event_type_field": "eventType",
            "event_mappings": [
                {
                    "source_event": "Health",
                    "event_type": "health_issue",
                    "severity": "error",
                },
            ],
        })
        resp = await webhook_client.post(
            "/ingest/webhook/mapped-src",
            json={"eventType": "Health", "message": "disk low"},
        )
        assert resp.status_code == 202

        # Verify the enqueued event
        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.event_type == "health_issue"
        assert event.severity.value == "error"
        assert event.system == "radarr"
        assert event.source == "webhook:mapped-src"

    async def test_unmapped_event_uses_defaults(
        self, webhook_client: AsyncClient,
    ) -> None:
        """Unknown event type falls through to raw name + default severity."""
        await _create_webhook_source(webhook_client, "default-src", {
            "system": "test",
            "default_severity": "info",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/default-src",
            json={"eventType": "SomeUnknownEvent"},
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.event_type == "SomeUnknownEvent"
        assert event.severity.value == "info"

    async def test_entity_id_dotted_path(self, webhook_client: AsyncClient) -> None:
        """Entity ID extracted from nested payload field."""
        await _create_webhook_source(webhook_client, "entity-src", {
            "system": "radarr",
            "entity_id_field": "movie.title",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/entity-src",
            json={"eventType": "Grab", "movie": {"title": "Inception"}},
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.entity_id == "Inception"

    async def test_entity_id_fallback(self, webhook_client: AsyncClient) -> None:
        """Missing entity_id_field falls back to source:event_type."""
        await _create_webhook_source(webhook_client, "fallback-src", {
            "system": "test",
            "entity_id_field": "nonexistent.path",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/fallback-src",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.entity_id == "fallback-src:Test"

    async def test_payload_preserved(self, webhook_client: AsyncClient) -> None:
        """Full raw payload is preserved in the Event."""
        await _create_webhook_source(webhook_client, "raw-src", {
            "system": "test",
        })
        payload = {"eventType": "Test", "data": {"nested": True}, "count": 42}
        resp = await webhook_client.post(
            "/ingest/webhook/raw-src",
            json=payload,
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.payload == payload

    async def test_dedup_key_set(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "dedup-src", {
            "system": "test",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/dedup-src",
            json={"eventType": "Alert"},
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.metadata.dedup_key.startswith("webhook:dedup-src:Alert:")


# ---------------------------------------------------------------------------
# Enqueue behavior
# ---------------------------------------------------------------------------


class TestEnqueue:
    async def test_returns_202_with_event_id(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "enqueue-src", {
            "system": "test",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/enqueue-src",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "event_id" in data
        assert data["source"] == "enqueue-src"

    async def test_queue_full_returns_503(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "full-src", {
            "system": "test",
        })
        # Make put_nowait raise
        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        orch.enqueue.side_effect = Exception("Queue full")

        resp = await webhook_client.post(
            "/ingest/webhook/full-src",
            json={"eventType": "Test"},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Realistic service payloads
# ---------------------------------------------------------------------------


class TestRealisticPayloads:
    async def test_radarr_health_webhook(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "radarr", {
            "system": "radarr",
            "event_type_field": "eventType",
            "entity_id_field": "movie.title",
            "event_mappings": [
                {"source_event": "Health", "event_type": "health_issue", "severity": "error"},
                {"source_event": "Grab", "event_type": "media_grab", "severity": "info"},
                {"source_event": "Download", "event_type": "media_download", "severity": "info"},
            ],
        })
        resp = await webhook_client.post(
            "/ingest/webhook/radarr",
            json={
                "eventType": "Health",
                "health": {"type": "IndexerStatusCheck", "message": "Indexers unavailable"},
            },
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.event_type == "health_issue"
        assert event.severity.value == "error"
        assert event.system == "radarr"

    async def test_sonarr_grab_webhook(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "sonarr", {
            "system": "sonarr",
            "auth_mode": "header_secret",
            "auth_header": "X-Sonarr-Signature",
            "auth_secret": "sonarr-key",
            "event_type_field": "eventType",
            "entity_id_field": "series.title",
            "event_mappings": [
                {"source_event": "Grab", "event_type": "media_grab", "severity": "info"},
            ],
        })
        resp = await webhook_client.post(
            "/ingest/webhook/sonarr",
            json={
                "eventType": "Grab",
                "series": {"title": "Breaking Bad"},
                "episodes": [{"seasonNumber": 1, "episodeNumber": 1}],
            },
            headers={"X-Sonarr-Signature": "sonarr-key"},
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.event_type == "media_grab"
        assert event.entity_id == "Breaking Bad"

    async def test_plex_media_play(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "plex", {
            "system": "plex",
            "event_type_field": "event",
            "entity_id_field": "Metadata.title",
            "event_mappings": [
                {"source_event": "media.play", "event_type": "media_play", "severity": "info"},
                {"source_event": "media.stop", "event_type": "media_stop", "severity": "info"},
            ],
        })
        resp = await webhook_client.post(
            "/ingest/webhook/plex",
            json={
                "event": "media.play",
                "Metadata": {"title": "The Matrix", "type": "movie"},
                "Player": {"title": "Living Room TV"},
            },
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.event_type == "media_play"
        assert event.entity_id == "The Matrix"

    async def test_proxmox_node_alert(self, webhook_client: AsyncClient) -> None:
        await _create_webhook_source(webhook_client, "proxmox", {
            "system": "proxmox",
            "auth_mode": "api_key",
            "auth_secret": "pve-token",
            "event_type_field": "type",
            "entity_id_field": "node",
            "default_severity": "warning",
        })
        resp = await webhook_client.post(
            "/ingest/webhook/proxmox",
            json={
                "type": "node_high_cpu",
                "node": "pve-01",
                "cpu_percent": 95.2,
            },
            headers={"X-Api-Key": "pve-token"},
        )
        assert resp.status_code == 202

        orch = webhook_client._transport.app.state.orchestrator  # type: ignore[union-attr]
        event = orch.enqueue.call_args[0][0]
        assert event.event_type == "node_high_cpu"
        assert event.entity_id == "pve-01"
        assert event.system == "proxmox"
