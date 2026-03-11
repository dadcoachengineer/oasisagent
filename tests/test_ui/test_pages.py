"""Tests for authenticated page rendering and RBAC."""

from __future__ import annotations

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class TestDashboard:
    async def test_dashboard_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    async def test_dashboard_shows_stats(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/dashboard")
        assert "Events Processed" in resp.text
        assert "42" in resp.text  # From mock orchestrator

    async def test_dashboard_requires_auth(self, setup_client: AsyncClient) -> None:
        """Dashboard requires login (setup mode redirects to setup)."""
        resp = await setup_client.get("/ui/dashboard", follow_redirects=False)
        assert resp.status_code == 303

    async def test_viewer_can_access_dashboard(self, viewer_client: AsyncClient) -> None:
        resp = await viewer_client.get("/ui/dashboard")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


class TestSSEStream:
    async def test_sse_requires_auth(self, setup_client: AsyncClient) -> None:
        """SSE endpoint requires authentication."""
        # SSE streams hang with httpx ASGI transport, so we only test auth guard
        resp = await setup_client.get("/ui/events/stream", follow_redirects=False)
        assert resp.status_code == 303  # redirected to setup/login


# ---------------------------------------------------------------------------
# Connectors / Services / Notifications pages
# ---------------------------------------------------------------------------


class TestConfigPages:
    async def test_connectors_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/connectors")
        assert resp.status_code == 200
        assert "Connectors" in resp.text

    async def test_services_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/services")
        assert resp.status_code == 200
        assert "Services" in resp.text

    async def test_notifications_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/notifications")
        assert resp.status_code == 200
        assert "Notification" in resp.text

    async def test_viewer_can_read_connectors(self, viewer_client: AsyncClient) -> None:
        resp = await viewer_client.get("/ui/connectors")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Approvals page (requires operator)
# ---------------------------------------------------------------------------


class TestApprovalsPage:
    async def test_approvals_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/approvals")
        assert resp.status_code == 200
        assert "Approval" in resp.text

    async def test_viewer_cannot_access_approvals(self, viewer_client: AsyncClient) -> None:
        """Viewer role should be forbidden from approvals page."""
        resp = await viewer_client.get("/ui/approvals")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Events, Known Fixes pages
# ---------------------------------------------------------------------------


class TestReadOnlyPages:
    async def test_events_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/events")
        assert resp.status_code == 200
        assert "Event" in resp.text

    async def test_known_fixes_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/known-fixes")
        assert resp.status_code == 200
        assert "Known Fixes" in resp.text


# ---------------------------------------------------------------------------
# Users page (admin-only)
# ---------------------------------------------------------------------------


class TestUsersPage:
    async def test_users_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/users")
        assert resp.status_code == 200
        assert "Users" in resp.text
        assert "admin" in resp.text

    async def test_viewer_cannot_access_users(self, viewer_client: AsyncClient) -> None:
        resp = await viewer_client.get("/ui/users")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


class TestRootRedirect:
    async def test_root_redirects_to_dashboard(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/dashboard"
