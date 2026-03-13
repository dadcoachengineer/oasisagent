"""Tests for health badge UI routes."""

from __future__ import annotations

from unittest.mock import AsyncMock

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_orchestrator_health(
    client: AsyncClient,
    health: dict | None = None,
    *,
    missing: bool = False,
) -> None:
    """Replace the test app's orchestrator with one returning given health."""
    if missing:
        client._transport.app.state.orchestrator = None  # type: ignore[union-attr]
        return

    orch = client._transport.app.state.orchestrator  # type: ignore[union-attr]
    if health is not None:
        orch.get_component_health = AsyncMock(return_value=health)
    else:
        orch.get_component_health = AsyncMock(return_value={
            "connectors": {}, "services": {}, "notifications": {},
        })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConnectorHealth:
    async def test_health_endpoint_returns_badges(
        self, auth_client: AsyncClient,
    ) -> None:
        # Create a connector first
        await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "test-mqtt",
                "broker": "mqtt://localhost:1883",
            },
        )

        _patch_orchestrator_health(auth_client, {
            "connectors": {"mqtt": "connected"},
            "services": {},
            "notifications": {},
        })

        resp = await auth_client.get("/ui/connectors/health")
        assert resp.status_code == 200
        assert "Connected" in resp.text

    async def test_disabled_row_shows_disabled(
        self, auth_client: AsyncClient,
    ) -> None:
        # Create and disable
        await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "disabled-mqtt",
                "broker": "mqtt://localhost:1883",
            },
        )
        # Toggle off — get the row id
        list_resp = await auth_client.get("/ui/connectors")
        # Find the row id from the toggle button
        import re
        match = re.search(r"/ui/connectors/(\d+)/toggle", list_resp.text)
        assert match
        row_id = match.group(1)
        await auth_client.post(f"/ui/connectors/{row_id}/toggle")

        _patch_orchestrator_health(auth_client, {
            "connectors": {"mqtt": "connected"},
            "services": {},
            "notifications": {},
        })

        resp = await auth_client.get("/ui/connectors/health")
        assert resp.status_code == 200
        assert "Disabled" in resp.text

    async def test_missing_orchestrator_shows_not_running(
        self, auth_client: AsyncClient,
    ) -> None:
        await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "orphan-mqtt",
                "broker": "mqtt://localhost:1883",
            },
        )

        _patch_orchestrator_health(auth_client, missing=True)

        resp = await auth_client.get("/ui/connectors/health")
        assert resp.status_code == 200
        assert "Not Running" in resp.text


class TestServiceHealth:
    async def test_service_health_endpoint(
        self, auth_client: AsyncClient,
    ) -> None:
        # Create a service
        await auth_client.post(
            "/ui/services",
            data={
                "type": "ha_handler",
                "name": "home-assistant",
                "url": "http://ha.local:8123",
                "token": "test-token",
            },
        )

        _patch_orchestrator_health(auth_client, {
            "connectors": {},
            "services": {"ha_handler": "disconnected"},
            "notifications": {},
        })

        resp = await auth_client.get("/ui/services/health")
        assert resp.status_code == 200
        assert "Disconnected" in resp.text


class TestNotificationHealth:
    async def test_notification_health_endpoint(
        self, auth_client: AsyncClient,
    ) -> None:
        await auth_client.post(
            "/ui/notifications",
            data={
                "type": "mqtt_notification",
                "name": "mqtt-notify",
                "broker": "mqtt://localhost:1883",
            },
        )

        _patch_orchestrator_health(auth_client, {
            "connectors": {},
            "services": {},
            "notifications": {"mqtt_notification": "error"},
        })

        resp = await auth_client.get("/ui/notifications/health")
        assert resp.status_code == 200
        assert "Error" in resp.text
