"""Tests for connector/service/notification restart UI routes."""

from __future__ import annotations

from unittest.mock import AsyncMock

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_orchestrator_restart(
    client: AsyncClient,
    method: str,
    *,
    success: bool = True,
) -> AsyncMock:
    """Patch a restart method on the test app's orchestrator."""
    orch = client._transport.app.state.orchestrator  # type: ignore[union-attr]
    mock = AsyncMock(return_value=success)
    setattr(orch, method, mock)
    return mock


async def _create_connector(client: AsyncClient, name: str = "test-mqtt") -> int:
    """Create a test connector and return its row ID."""
    await client.post(
        "/ui/connectors",
        data={
            "type": "mqtt",
            "name": name,
            "broker": "mqtt://localhost:1883",
        },
    )
    # Find row ID from the list page
    import re

    resp = await client.get("/ui/connectors")
    match = re.search(r"/ui/connectors/(\d+)/toggle", resp.text)
    assert match, "Could not find connector row ID"
    return int(match.group(1))


async def _create_service(client: AsyncClient, name: str = "test-ha") -> int:
    """Create a test service and return its row ID."""
    await client.post(
        "/ui/services",
        data={
            "type": "ha_handler",
            "name": name,
            "url": "http://localhost:8123",
            "token": "test-token",
        },
    )
    import re

    resp = await client.get("/ui/services")
    match = re.search(r"/ui/services/(\d+)/toggle", resp.text)
    assert match, "Could not find service row ID"
    return int(match.group(1))


async def _create_notification(client: AsyncClient, name: str = "test-webhook") -> int:
    """Create a test notification and return its row ID."""
    await client.post(
        "/ui/channels",
        data={
            "type": "webhook",
            "name": name,
            "url": "http://localhost:9999/hook",
        },
    )
    import re

    resp = await client.get("/ui/channels")
    match = re.search(r"/ui/channels/(\d+)/toggle", resp.text)
    assert match, "Could not find notification row ID"
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Connector restart
# ---------------------------------------------------------------------------


class TestConnectorRestart:
    async def test_restart_calls_orchestrator(
        self, auth_client: AsyncClient,
    ) -> None:
        row_id = await _create_connector(auth_client)
        mock = _patch_orchestrator_restart(
            auth_client, "restart_connector", success=True,
        )

        resp = await auth_client.post(f"/ui/connectors/{row_id}/restart")
        assert resp.status_code == 200
        mock.assert_awaited_once_with(row_id)
        assert resp.headers.get("hx-trigger") == "restart-success"

    async def test_restart_failure_returns_trigger(
        self, auth_client: AsyncClient,
    ) -> None:
        row_id = await _create_connector(auth_client)
        _patch_orchestrator_restart(
            auth_client, "restart_connector", success=False,
        )

        resp = await auth_client.post(f"/ui/connectors/{row_id}/restart")
        assert resp.status_code == 200
        assert resp.headers.get("hx-trigger") == "restart-failed"

    async def test_restart_not_found_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        _patch_orchestrator_restart(auth_client, "restart_connector")
        resp = await auth_client.post("/ui/connectors/999/restart")
        assert resp.status_code == 404

    async def test_restart_requires_admin(
        self, viewer_client: AsyncClient,
    ) -> None:
        resp = await viewer_client.post(
            "/ui/connectors/1/restart", follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_restart_no_orchestrator_returns_503(
        self, auth_client: AsyncClient,
    ) -> None:
        row_id = await _create_connector(auth_client)
        # Remove orchestrator
        auth_client._transport.app.state.orchestrator = None  # type: ignore[union-attr]

        resp = await auth_client.post(f"/ui/connectors/{row_id}/restart")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Service restart
# ---------------------------------------------------------------------------


class TestServiceRestart:
    async def test_restart_calls_orchestrator(
        self, auth_client: AsyncClient,
    ) -> None:
        row_id = await _create_service(auth_client)
        mock = _patch_orchestrator_restart(
            auth_client, "restart_service", success=True,
        )

        resp = await auth_client.post(f"/ui/services/{row_id}/restart")
        assert resp.status_code == 200
        mock.assert_awaited_once_with(row_id)
        assert resp.headers.get("hx-trigger") == "restart-success"

    async def test_restart_failure_returns_trigger(
        self, auth_client: AsyncClient,
    ) -> None:
        row_id = await _create_service(auth_client)
        _patch_orchestrator_restart(
            auth_client, "restart_service", success=False,
        )

        resp = await auth_client.post(f"/ui/services/{row_id}/restart")
        assert resp.status_code == 200
        assert resp.headers.get("hx-trigger") == "restart-failed"

    async def test_restart_not_found_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        _patch_orchestrator_restart(auth_client, "restart_service")
        resp = await auth_client.post("/ui/services/999/restart")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Notification restart
# ---------------------------------------------------------------------------


class TestNotificationRestart:
    async def test_restart_calls_orchestrator(
        self, auth_client: AsyncClient,
    ) -> None:
        row_id = await _create_notification(auth_client)
        mock = _patch_orchestrator_restart(
            auth_client, "restart_notification", success=True,
        )

        resp = await auth_client.post(f"/ui/channels/{row_id}/restart")
        assert resp.status_code == 200
        mock.assert_awaited_once_with(row_id)
        assert resp.headers.get("hx-trigger") == "restart-success"

    async def test_restart_not_found_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        _patch_orchestrator_restart(auth_client, "restart_notification")
        resp = await auth_client.post("/ui/channels/999/restart")
        assert resp.status_code == 404
