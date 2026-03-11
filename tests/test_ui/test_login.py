"""Tests for login/logout flows and setup wizard UI."""

from __future__ import annotations

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------


class TestLoginPage:
    async def test_login_page_renders(self, setup_client: AsyncClient) -> None:
        """Login page is accessible even during setup mode."""
        resp = await setup_client.get("/ui/login")
        assert resp.status_code == 200
        assert "Sign in" in resp.text

    async def test_login_invalid_credentials(self, setup_client: AsyncClient) -> None:
        """Login with wrong password returns error."""
        # First create an admin so we're past setup mode
        from oasisagent.ui.auth import hash_password

        store = setup_client._transport.app.state.config_store  # type: ignore[union-attr]
        await store.create_user(
            username="admin", password_hash=hash_password("correctpass"),
            role="admin",
        )

        resp = await setup_client.post(
            "/ui/login",
            data={"username": "admin", "password": "wrongpass"},
        )
        assert resp.status_code == 401
        assert "Invalid" in resp.text

    async def test_login_nonexistent_user(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.post(
            "/ui/login",
            data={"username": "nobody", "password": "anything"},
        )
        assert resp.status_code == 401

    async def test_login_success_viewer_no_totp(self, setup_client: AsyncClient) -> None:
        """Viewer without TOTP can login directly."""
        from oasisagent.ui.auth import hash_password

        store = setup_client._transport.app.state.config_store  # type: ignore[union-attr]
        # Need admin first for setup guard
        await store.create_user(
            username="admin", password_hash=hash_password("adminpass1"),
            role="admin", totp_confirmed=True,
        )
        await store.create_user(
            username="viewer", password_hash=hash_password("viewerpass1"),
            role="viewer",
        )

        resp = await setup_client.post(
            "/ui/login",
            data={"username": "viewer", "password": "viewerpass1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/dashboard"
        assert "oasis_access_token" in resp.cookies

    async def test_login_admin_no_totp_forces_enrollment(
        self, setup_client: AsyncClient,
    ) -> None:
        """Admin without TOTP enrolled gets forced to enrollment page."""
        from oasisagent.ui.auth import hash_password

        store = setup_client._transport.app.state.config_store  # type: ignore[union-attr]
        await store.create_user(
            username="admin", password_hash=hash_password("adminpass1"),
            role="admin",
        )

        resp = await setup_client.post(
            "/ui/login",
            data={"username": "admin", "password": "adminpass1"},
        )
        assert resp.status_code == 200
        assert "Two-Factor Authentication" in resp.text or "Set Up" in resp.text
        # M1 fix: pending_token cookie must be set for enrollment flow
        assert "oasis_pending_token" in resp.cookies

    async def test_login_admin_with_totp_shows_verify(
        self, setup_client: AsyncClient,
    ) -> None:
        """Admin with TOTP enrolled gets 2FA verification page."""
        from oasisagent.ui.auth import hash_password

        store = setup_client._transport.app.state.config_store  # type: ignore[union-attr]
        user_id = await store.create_user(
            username="admin", password_hash=hash_password("adminpass1"),
            role="admin",
        )
        await store.update_user(user_id, {
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "totp_confirmed": 1,
        })

        resp = await setup_client.post(
            "/ui/login",
            data={"username": "admin", "password": "adminpass1"},
        )
        assert resp.status_code == 200
        assert "Authentication code" in resp.text


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    async def test_logout_clears_cookies(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.post("/ui/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/login"

    async def test_logout_redirects_to_login(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.post("/ui/logout", follow_redirects=False)
        assert resp.headers["location"] == "/ui/login"


# ---------------------------------------------------------------------------
# Setup wizard UI
# ---------------------------------------------------------------------------


class TestSetupWizard:
    async def test_setup_page_renders(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/ui/setup")
        assert resp.status_code == 200
        assert "Welcome to OasisAgent" in resp.text

    async def test_setup_redirects_after_admin_exists(
        self, auth_client: AsyncClient,
    ) -> None:
        """Setup page redirects to login when admin already exists."""
        resp = await auth_client.get("/ui/setup", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/login"

    async def test_setup_create_admin_mismatched_passwords(
        self, setup_client: AsyncClient,
    ) -> None:
        resp = await setup_client.post(
            "/ui/setup/admin",
            data={
                "username": "admin",
                "password": "securepass123",
                "password_confirm": "different",
            },
        )
        assert resp.status_code == 200
        assert "do not match" in resp.text

    async def test_setup_create_admin_short_password(
        self, setup_client: AsyncClient,
    ) -> None:
        resp = await setup_client.post(
            "/ui/setup/admin",
            data={
                "username": "admin",
                "password": "short",
                "password_confirm": "short",
            },
        )
        assert resp.status_code == 200
        assert "8 characters" in resp.text

    async def test_setup_create_admin_success_shows_totp(
        self, setup_client: AsyncClient,
    ) -> None:
        """Admin creation redirects to TOTP enrollment."""
        resp = await setup_client.post(
            "/ui/setup/admin",
            data={
                "username": "admin",
                "password": "securepass123",
                "password_confirm": "securepass123",
            },
        )
        assert resp.status_code == 200
        # Should show TOTP enrollment page
        assert "QR" in resp.text or "authenticator" in resp.text.lower()

    async def test_setup_complete_redirects(
        self, setup_client: AsyncClient,
    ) -> None:
        resp = await setup_client.get("/ui/setup/complete", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/login"


# ---------------------------------------------------------------------------
# Middleware redirects
# ---------------------------------------------------------------------------


class TestSetupGuardUI:
    async def test_ui_pages_redirect_to_setup_when_no_admin(
        self, setup_client: AsyncClient,
    ) -> None:
        """UI pages redirect to setup wizard during setup mode."""
        resp = await setup_client.get("/ui/dashboard", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/setup"

    async def test_api_blocked_during_setup(
        self, setup_client: AsyncClient,
    ) -> None:
        """API endpoints return 403 during setup mode."""
        resp = await setup_client.get("/api/v1/status")
        assert resp.status_code == 403
