"""Tests for the scanner config page (/ui/services/scanners).

Uses the shared ``auth_client`` fixture which provides a real ConfigStore.
"""

from __future__ import annotations

from httpx import AsyncClient


class TestScannerPageAccess:
    async def test_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/services/scanners")
        assert resp.status_code == 200
        assert "Scanner Configuration" in resp.text

    async def test_page_requires_admin(
        self, viewer_client: AsyncClient,
    ) -> None:
        resp = await viewer_client.get(
            "/ui/services/scanners", follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_page_shows_all_sections(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.get("/ui/services/scanners")
        assert "Certificate Expiry" in resp.text
        assert "Disk Space" in resp.text
        assert "HA Integration Health" in resp.text
        assert "Docker Health" in resp.text


class TestScannerHandlerDependency:
    async def test_ha_section_disabled_without_handler(
        self, auth_client: AsyncClient,
    ) -> None:
        """HA health section shows warning when HA handler is not enabled."""
        resp = await auth_client.get("/ui/services/scanners")
        assert "Enable the Home Assistant handler first" in resp.text

    async def test_docker_section_disabled_without_handler(
        self, auth_client: AsyncClient,
    ) -> None:
        """Docker health section shows warning when Docker handler is not enabled."""
        resp = await auth_client.get("/ui/services/scanners")
        assert "Enable the Docker handler first" in resp.text

    async def test_ha_section_enabled_with_handler(
        self, auth_client: AsyncClient,
    ) -> None:
        """HA health section is editable when HA handler exists and is enabled."""
        # Create and enable HA handler
        await auth_client.post(
            "/ui/services",
            data={
                "type": "ha_handler",
                "name": "ha",
                "url": "http://ha.local:8123",
                "token": "test-token",
            },
        )
        # Toggle it enabled (it starts enabled via create_service default)
        resp = await auth_client.get("/ui/services/scanners")
        assert "Enable the Home Assistant handler first" not in resp.text

    async def test_docker_section_enabled_with_handler(
        self, auth_client: AsyncClient,
    ) -> None:
        """Docker health section is editable when Docker handler is enabled."""
        await auth_client.post(
            "/ui/services",
            data={
                "type": "docker_handler",
                "name": "docker",
            },
        )
        resp = await auth_client.get("/ui/services/scanners")
        assert "Enable the Docker handler first" not in resp.text


class TestScannerSave:
    async def test_save_creates_scanner_row(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.post(
            "/ui/services/scanners",
            data={
                "enabled": "on",
                "interval": "600",
                "cert_enabled": "on",
                "cert_endpoints": "example.com:443\ngoogle.com",
                "cert_warning_days": "14",
                "cert_critical_days": "3",
                "cert_interval": "1800",
                "disk_enabled": "on",
                "disk_paths": "/\n/data",
                "disk_warning_pct": "80",
                "disk_critical_pct": "90",
                "disk_interval": "900",
            },
        )
        assert resp.status_code == 200
        assert "Scanner configuration saved" in resp.text

        # Verify config persisted by re-loading page
        page = await auth_client.get("/ui/services/scanners")
        assert "example.com:443" in page.text
        assert "google.com" in page.text

    async def test_save_updates_existing_scanner(
        self, auth_client: AsyncClient,
    ) -> None:
        # Create initial
        await auth_client.post(
            "/ui/services/scanners",
            data={
                "interval": "900",
                "cert_warning_days": "30",
                "cert_critical_days": "7",
                "cert_interval": "900",
                "disk_warning_pct": "85",
                "disk_critical_pct": "95",
                "disk_interval": "900",
                "ha_interval": "900",
                "docker_interval": "900",
            },
        )
        # Update with new interval
        resp = await auth_client.post(
            "/ui/services/scanners",
            data={
                "enabled": "on",
                "interval": "300",
                "cert_warning_days": "30",
                "cert_critical_days": "7",
                "cert_interval": "900",
                "disk_warning_pct": "85",
                "disk_critical_pct": "95",
                "disk_interval": "900",
                "ha_interval": "900",
                "docker_interval": "900",
            },
        )
        assert resp.status_code == 200
        assert "Scanner configuration saved" in resp.text

    async def test_save_requires_admin(
        self, viewer_client: AsyncClient,
    ) -> None:
        resp = await viewer_client.post(
            "/ui/services/scanners",
            data={"interval": "900"},
        )
        assert resp.status_code == 403

    async def test_save_invalid_interval(
        self, auth_client: AsyncClient,
    ) -> None:
        """Interval below 60 should fail validation."""
        resp = await auth_client.post(
            "/ui/services/scanners",
            data={
                "interval": "10",
                "cert_warning_days": "30",
                "cert_critical_days": "7",
                "cert_interval": "900",
                "disk_warning_pct": "85",
                "disk_critical_pct": "95",
                "disk_interval": "900",
                "ha_interval": "900",
                "docker_interval": "900",
            },
        )
        assert resp.status_code == 422


class TestScannerFormParsing:
    """Direct tests for _parse_scanner_form."""

    def test_checkbox_handling(self) -> None:
        from oasisagent.ui.routes.connectors import _parse_scanner_form

        result = _parse_scanner_form({
            "enabled": "on",
            "interval": "600",
            "cert_enabled": "on",
            "cert_endpoints": "",
            "cert_warning_days": "30",
            "cert_critical_days": "7",
            "cert_interval": "900",
            "disk_warning_pct": "85",
            "disk_critical_pct": "95",
            "disk_interval": "900",
            "ha_interval": "900",
            "docker_interval": "900",
        })
        assert result["enabled"] is True
        assert result["certificate_expiry"]["enabled"] is True
        assert result["disk_space"]["enabled"] is False
        assert result["ha_health"]["enabled"] is False
        assert result["docker_health"]["enabled"] is False
        assert result["interval"] == 600

    def test_list_str_parsing(self) -> None:
        from oasisagent.ui.routes.connectors import _parse_scanner_form

        result = _parse_scanner_form({
            "interval": "900",
            "cert_endpoints": "a.com:443\n\nb.com\n  ",
            "cert_warning_days": "30",
            "cert_critical_days": "7",
            "cert_interval": "900",
            "disk_paths": "/data\n/var",
            "disk_warning_pct": "85",
            "disk_critical_pct": "95",
            "disk_interval": "900",
            "docker_ignore": "portainer\nwatchtower",
            "ha_interval": "900",
            "docker_interval": "900",
        })
        assert result["certificate_expiry"]["endpoints"] == ["a.com:443", "b.com"]
        assert result["disk_space"]["paths"] == ["/data", "/var"]
        assert result["docker_health"]["ignore_containers"] == [
            "portainer", "watchtower",
        ]

    def test_defaults_on_empty(self) -> None:
        from oasisagent.ui.routes.connectors import _parse_scanner_form

        result = _parse_scanner_form({})
        assert result["enabled"] is False
        assert result["interval"] == 900
        assert result["certificate_expiry"]["warning_days"] == 30
        assert result["disk_space"]["warning_threshold_pct"] == 85
