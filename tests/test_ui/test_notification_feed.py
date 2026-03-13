"""Tests for the notification feed UI routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import AsyncClient

from oasisagent.models import Notification, Severity

if TYPE_CHECKING:
    from oasisagent.db.notification_store import NotificationStore


def _get_store(client: AsyncClient) -> NotificationStore:
    return client._transport.app.state.notification_store  # type: ignore[union-attr]


async def _add_notification(
    client: AsyncClient,
    *,
    title: str = "Test alert",
    severity: Severity = Severity.WARNING,
    action_id: str | None = None,
    action_status: str | None = None,
) -> str:
    """Insert a notification directly into the store."""
    store = _get_store(client)
    n = Notification(
        severity=severity,
        title=title,
        message="Test message",
        event_id="evt-test",
    )
    await store.add(n, action_id=action_id, action_status=action_status)
    return n.id


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------


class TestNotificationFeedPage:
    async def test_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/notifications")
        assert resp.status_code == 200
        assert "Notifications" in resp.text

    async def test_page_shows_notifications(self, auth_client: AsyncClient) -> None:
        await _add_notification(auth_client, title="Server restarted")
        resp = await auth_client.get("/ui/notifications")
        assert "Server restarted" in resp.text

    async def test_empty_state(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/notifications")
        assert "No notifications yet" in resp.text

    async def test_viewer_can_access(self, viewer_client: AsyncClient) -> None:
        resp = await viewer_client.get("/ui/notifications")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# HTMX list partial
# ---------------------------------------------------------------------------


class TestNotificationList:
    async def test_list_partial(self, auth_client: AsyncClient) -> None:
        await _add_notification(auth_client, title="Alert 1")
        await _add_notification(auth_client, title="Alert 2")
        resp = await auth_client.get("/ui/notifications/list")
        assert resp.status_code == 200
        assert "Alert 1" in resp.text
        assert "Alert 2" in resp.text

    async def test_severity_filter(self, auth_client: AsyncClient) -> None:
        await _add_notification(auth_client, severity=Severity.INFO, title="Info msg")
        await _add_notification(auth_client, severity=Severity.ERROR, title="Error msg")
        resp = await auth_client.get("/ui/notifications/list?severity=error")
        assert "Error msg" in resp.text
        assert "Info msg" not in resp.text


# ---------------------------------------------------------------------------
# Unread count badge
# ---------------------------------------------------------------------------


class TestUnreadCount:
    async def test_zero_count_empty(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/notifications/unread-count")
        assert resp.status_code == 200
        assert resp.text == ""

    async def test_nonzero_count_badge(self, auth_client: AsyncClient) -> None:
        await _add_notification(auth_client)
        resp = await auth_client.get("/ui/notifications/unread-count")
        assert resp.status_code == 200
        assert "1" in resp.text
        assert "bg-red-500" in resp.text


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestNotificationRBAC:
    async def test_viewer_cannot_approve(self, viewer_client: AsyncClient) -> None:
        nid = await _add_notification(
            viewer_client, action_id="act-1", action_status="pending",
        )
        resp = await viewer_client.post(f"/ui/notifications/{nid}/approve")
        assert resp.status_code == 403

    async def test_viewer_cannot_reject(self, viewer_client: AsyncClient) -> None:
        nid = await _add_notification(
            viewer_client, action_id="act-1", action_status="pending",
        )
        resp = await viewer_client.post(f"/ui/notifications/{nid}/reject")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Approve/Reject — already resolved
# ---------------------------------------------------------------------------


class TestAlreadyResolved:
    async def test_approve_already_approved(self, auth_client: AsyncClient) -> None:
        """Approving an already-resolved notification returns card, not error."""
        nid = await _add_notification(
            auth_client, action_id="act-2", action_status="approved",
        )
        resp = await auth_client.post(f"/ui/notifications/{nid}/approve")
        assert resp.status_code == 200
        # Should render the card without error
        assert "Approved" in resp.text

    async def test_reject_already_expired(self, auth_client: AsyncClient) -> None:
        nid = await _add_notification(
            auth_client, action_id="act-3", action_status="expired",
        )
        resp = await auth_client.post(f"/ui/notifications/{nid}/reject")
        assert resp.status_code == 200
        assert "Expired" in resp.text


# ---------------------------------------------------------------------------
# Channel config moved to /ui/channels
# ---------------------------------------------------------------------------


class TestChannelConfigRedirect:
    async def test_channels_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/channels")
        assert resp.status_code == 200
        assert "Notification" in resp.text
