"""Tests for connector/service/notification CRUD UI routes.

Uses the shared ``auth_client`` fixture from conftest.py which provides
a real ConfigStore backed by an in-memory SQLite database.
"""

from __future__ import annotations

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Connectors CRUD
# ---------------------------------------------------------------------------


class TestConnectorList:
    async def test_list_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/connectors")
        assert resp.status_code == 200
        assert "Connectors" in resp.text

    async def test_viewer_can_list(self, viewer_client: AsyncClient) -> None:
        resp = await viewer_client.get("/ui/connectors")
        assert resp.status_code == 200


class TestConnectorTypePicker:
    async def test_type_picker_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/connectors/new")
        assert resp.status_code == 200
        assert "MQTT" in resp.text  # mqtt type should appear

    async def test_type_picker_requires_admin(
        self, viewer_client: AsyncClient,
    ) -> None:
        resp = await viewer_client.get(
            "/ui/connectors/new", follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_unknown_type_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.get("/ui/connectors/new?type=nonexistent")
        assert resp.status_code == 404


class TestConnectorCreateForm:
    async def test_create_form_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/connectors/new?type=mqtt")
        assert resp.status_code == 200
        assert "MQTT" in resp.text
        assert 'name="name"' in resp.text

    async def test_create_connector(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "test-mqtt",
                "broker": "mqtt://mqtt.example.com:1883",
            },
        )
        # Success → 204 with HX-Redirect
        assert resp.status_code == 204
        assert resp.headers.get("hx-redirect") == "/ui/connectors"

        # Verify it appears in list
        list_resp = await auth_client.get("/ui/connectors")
        assert "test-mqtt" in list_resp.text

    async def test_create_duplicate_name_returns_422(
        self, auth_client: AsyncClient,
    ) -> None:
        # Create first
        await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "dup-mqtt",
                "broker": "mqtt://mqtt.example.com:1883",
            },
        )
        # Create duplicate
        resp = await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "dup-mqtt",
                "broker": "mqtt://mqtt2.example.com:1883",
            },
        )
        assert resp.status_code == 422
        assert "already exists" in resp.text.lower()

    async def test_create_unknown_type_returns_422(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.post(
            "/ui/connectors",
            data={"type": "bogus", "name": "whatever"},
        )
        assert resp.status_code == 422


class TestConnectorEdit:
    async def _create_mqtt(self, client: AsyncClient) -> None:
        await client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "edit-test",
                "broker": "mqtt://mqtt.example.com:1883",
            },
        )

    async def test_edit_form_renders(self, auth_client: AsyncClient) -> None:
        await self._create_mqtt(auth_client)
        # Row ID should be 1 (first insert)
        resp = await auth_client.get("/ui/connectors/1/edit")
        assert resp.status_code == 200
        assert "edit-test" in resp.text
        assert "Save Changes" in resp.text

    async def test_edit_not_found_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.get("/ui/connectors/999/edit")
        assert resp.status_code == 404

    async def test_update_connector(self, auth_client: AsyncClient) -> None:
        await self._create_mqtt(auth_client)
        resp = await auth_client.post(
            "/ui/connectors/1",
            data={
                "name": "updated-mqtt",
                "broker": "mqtt://new-host.example.com:1884",
            },
        )
        assert resp.status_code == 204
        assert resp.headers.get("hx-redirect") == "/ui/connectors"

        # Verify update
        list_resp = await auth_client.get("/ui/connectors")
        assert "updated-mqtt" in list_resp.text

    async def test_update_not_found_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.post(
            "/ui/connectors/999",
            data={"name": "nope", "broker": "mqtt://x:1883"},
        )
        assert resp.status_code == 404


class TestConnectorDelete:
    async def test_delete_connector(self, auth_client: AsyncClient) -> None:
        # Create first
        await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "delete-me",
                "broker": "mqtt://mqtt.example.com:1883",
            },
        )
        # Delete
        resp = await auth_client.post("/ui/connectors/1/delete")
        assert resp.status_code == 204

        # Verify gone
        list_resp = await auth_client.get("/ui/connectors")
        assert "delete-me" not in list_resp.text

    async def test_delete_not_found_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.post("/ui/connectors/999/delete")
        assert resp.status_code == 404


class TestConnectorToggle:
    async def test_toggle_enable_disable(
        self, auth_client: AsyncClient,
    ) -> None:
        # Create connector (defaults to enabled=False)
        await auth_client.post(
            "/ui/connectors",
            data={
                "type": "mqtt",
                "name": "toggle-test",
                "broker": "mqtt://mqtt.example.com:1883",
            },
        )
        # Toggle — should return the _row.html partial
        resp = await auth_client.post("/ui/connectors/1/toggle")
        assert resp.status_code == 200
        # The partial should contain the row HTML
        assert "toggle-test" in resp.text

    async def test_toggle_not_found_returns_404(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.post("/ui/connectors/999/toggle")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Services CRUD (smoke tests — same factory, verify routing works)
# ---------------------------------------------------------------------------


class TestServiceRoutes:
    async def test_list_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/services")
        assert resp.status_code == 200
        assert "Services" in resp.text

    async def test_type_picker_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/services/new")
        assert resp.status_code == 200

    async def test_create_service(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.post(
            "/ui/services",
            data={
                "type": "influxdb",
                "name": "test-influxdb",
                "url": "http://localhost:8086",
                "token": "my-token",
                "org": "my-org",
                "bucket": "oasis",
            },
        )
        assert resp.status_code == 204
        assert resp.headers.get("hx-redirect") == "/ui/services"


# ---------------------------------------------------------------------------
# Notifications CRUD (smoke tests)
# ---------------------------------------------------------------------------


class TestNotificationRoutes:
    async def test_list_page_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/channels")
        assert resp.status_code == 200
        assert "Notification" in resp.text

    async def test_type_picker_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get("/ui/channels/new")
        assert resp.status_code == 200

    async def test_create_notification(
        self, auth_client: AsyncClient,
    ) -> None:
        resp = await auth_client.post(
            "/ui/channels",
            data={
                "type": "telegram",
                "name": "test-telegram",
                "bot_token": "123456:ABC",
                "chat_id": "12345",
            },
        )
        assert resp.status_code == 204
        assert resp.headers.get("hx-redirect") == "/ui/channels"


# ---------------------------------------------------------------------------
# RBAC — admin-only routes
# ---------------------------------------------------------------------------


class TestRBAC:
    async def test_viewer_cannot_create(
        self, viewer_client: AsyncClient,
    ) -> None:
        resp = await viewer_client.post(
            "/ui/connectors",
            data={"type": "mqtt", "name": "hack"},
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_delete(
        self, viewer_client: AsyncClient,
    ) -> None:
        resp = await viewer_client.post("/ui/connectors/1/delete")
        assert resp.status_code == 403

    async def test_viewer_cannot_toggle(
        self, viewer_client: AsyncClient,
    ) -> None:
        resp = await viewer_client.post("/ui/connectors/1/toggle")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# _parse_form_config unit tests
# ---------------------------------------------------------------------------


class TestParseFormConfig:
    """Direct tests for the form parsing helper."""

    def test_checkbox_present_is_true(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("verify_ssl", "Verify SSL", "checkbox", default=True)]
        result = _parse_form_config({"verify_ssl": "on"}, specs)
        assert result["verify_ssl"] is True

    def test_checkbox_absent_is_false(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("verify_ssl", "Verify SSL", "checkbox", default=True)]
        result = _parse_form_config({}, specs)
        assert result["verify_ssl"] is False

    def test_password_empty_on_edit_omitted(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("api_key", "API Key", "password")]
        result = _parse_form_config({"api_key": ""}, specs, is_edit=True)
        assert "api_key" not in result

    def test_password_filled_on_edit_included(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("api_key", "API Key", "password")]
        result = _parse_form_config({"api_key": "new-secret"}, specs, is_edit=True)
        assert result["api_key"] == "new-secret"

    def test_password_on_create_included(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("token", "Token", "password")]
        result = _parse_form_config({"token": "abc"}, specs, is_edit=False)
        assert result["token"] == "abc"

    def test_number_casting(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("port", "Port", "number", default=1883)]
        result = _parse_form_config({"port": "8883"}, specs)
        assert result["port"] == 8883
        assert isinstance(result["port"], int)

    def test_number_empty_uses_default(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("port", "Port", "number", default=1883)]
        result = _parse_form_config({"port": ""}, specs)
        assert result["port"] == 1883

    def test_float_casting(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("temp", "Temperature", "float")]
        result = _parse_form_config({"temp": "0.7"}, specs)
        assert result["temp"] == 0.7

    def test_list_str_splitting(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("endpoints", "Endpoints", "list_str")]
        result = _parse_form_config(
            {"endpoints": "https://a.com\n\nhttps://b.com\n  \n"},
            specs,
        )
        assert result["endpoints"] == ["https://a.com", "https://b.com"]

    def test_list_str_empty(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("endpoints", "Endpoints", "list_str")]
        result = _parse_form_config({"endpoints": ""}, specs)
        assert result["endpoints"] == []

    def test_text_with_default(self) -> None:
        from oasisagent.ui.form_specs import FieldSpec
        from oasisagent.ui.routes.connectors import _parse_form_config

        specs = [FieldSpec("host", "Host", "text", default="localhost")]
        result = _parse_form_config({"host": ""}, specs)
        assert result["host"] == "localhost"
