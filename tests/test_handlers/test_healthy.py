"""Tests for Handler.healthy() — default and overrides."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from oasisagent.handlers.base import Handler

if TYPE_CHECKING:
    from oasisagent.models import ActionResult, Event, RecommendedAction, VerifyResult

# ---------------------------------------------------------------------------
# Default healthy()
# ---------------------------------------------------------------------------


class TestDefaultHealthy:
    """Handler ABC default healthy() returns True."""

    async def test_default_returns_true(self) -> None:
        # Create a concrete subclass with stubs for abstract methods
        class StubHandler(Handler):
            def name(self) -> str:
                return "stub"

            async def can_handle(self, event: Event, action: RecommendedAction) -> bool:
                return False

            async def execute(self, event: Event, action: RecommendedAction) -> ActionResult:
                ...  # type: ignore[return-value]

            async def verify(
                self, event: Event, action: RecommendedAction, result: ActionResult,
            ) -> VerifyResult:
                ...  # type: ignore[return-value]

            async def get_context(self, event: Event) -> dict[str, Any]:
                return {}

        handler = StubHandler()
        assert await handler.healthy() is True


# ---------------------------------------------------------------------------
# HomeAssistant healthy()
# ---------------------------------------------------------------------------


class TestHomeAssistantHealthy:
    async def test_healthy_when_api_returns_200(self) -> None:
        from oasisagent.config import HaHandlerConfig
        from oasisagent.handlers.homeassistant import HomeAssistantHandler

        config = HaHandlerConfig(
            enabled=True, url="http://localhost:8123", token="test",
        )
        handler = HomeAssistantHandler(config)

        # Mock session
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        handler._session = mock_session

        assert await handler.healthy() is True

    async def test_unhealthy_when_api_returns_non_200(self) -> None:
        from oasisagent.config import HaHandlerConfig
        from oasisagent.handlers.homeassistant import HomeAssistantHandler

        config = HaHandlerConfig(
            enabled=True, url="http://localhost:8123", token="test",
        )
        handler = HomeAssistantHandler(config)

        mock_resp = MagicMock()
        mock_resp.status = 401
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        handler._session = mock_session

        assert await handler.healthy() is False

    async def test_unhealthy_when_no_session(self) -> None:
        from oasisagent.config import HaHandlerConfig
        from oasisagent.handlers.homeassistant import HomeAssistantHandler

        config = HaHandlerConfig(
            enabled=True, url="http://localhost:8123", token="test",
        )
        handler = HomeAssistantHandler(config)
        assert await handler.healthy() is False


# ---------------------------------------------------------------------------
# Docker healthy()
# ---------------------------------------------------------------------------


class TestDockerHealthy:
    async def test_healthy_when_ping_returns_ok(self) -> None:
        from oasisagent.config import DockerHandlerConfig
        from oasisagent.handlers.docker import DockerHandler

        config = DockerHandlerConfig(enabled=True)
        handler = DockerHandler(config)

        mock_resp = MagicMock()
        mock_resp.text = AsyncMock(return_value="OK")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        handler._session = mock_session

        assert await handler.healthy() is True

    async def test_unhealthy_when_ping_returns_not_ok(self) -> None:
        from oasisagent.config import DockerHandlerConfig
        from oasisagent.handlers.docker import DockerHandler

        config = DockerHandlerConfig(enabled=True)
        handler = DockerHandler(config)

        mock_resp = MagicMock()
        mock_resp.text = AsyncMock(return_value="Not OK")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        handler._session = mock_session

        assert await handler.healthy() is False

    async def test_unhealthy_when_no_session(self) -> None:
        from oasisagent.config import DockerHandlerConfig
        from oasisagent.handlers.docker import DockerHandler

        config = DockerHandlerConfig(enabled=True)
        handler = DockerHandler(config)
        assert await handler.healthy() is False


# ---------------------------------------------------------------------------
# Portainer healthy()
# ---------------------------------------------------------------------------


class TestPortainerHealthy:
    async def test_healthy_when_status_returns_200(self) -> None:
        from oasisagent.config import PortainerHandlerConfig
        from oasisagent.handlers.portainer import PortainerHandler

        config = PortainerHandlerConfig(
            enabled=True, url="https://portainer.local:9443",
            api_key="test-key", endpoint_id=1,
        )
        handler = PortainerHandler(config)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        handler._session = mock_session

        assert await handler.healthy() is True

    async def test_unhealthy_when_no_session(self) -> None:
        from oasisagent.config import PortainerHandlerConfig
        from oasisagent.handlers.portainer import PortainerHandler

        config = PortainerHandlerConfig(
            enabled=True, url="https://portainer.local:9443",
            api_key="test-key", endpoint_id=1,
        )
        handler = PortainerHandler(config)
        assert await handler.healthy() is False


# ---------------------------------------------------------------------------
# Proxmox healthy()
# ---------------------------------------------------------------------------


class TestProxmoxHealthy:
    async def test_healthy_when_version_returns_200(self) -> None:
        from oasisagent.config import ProxmoxHandlerConfig
        from oasisagent.handlers.proxmox import ProxmoxHandler

        config = ProxmoxHandlerConfig(
            enabled=True, url="https://pve.local:8006",
            user="root@pam", token_name="oasis",
            token_value="test-uuid",
        )
        handler = ProxmoxHandler(config)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        handler._session = mock_session

        assert await handler.healthy() is True

    async def test_unhealthy_when_no_session(self) -> None:
        from oasisagent.config import ProxmoxHandlerConfig
        from oasisagent.handlers.proxmox import ProxmoxHandler

        config = ProxmoxHandlerConfig(
            enabled=True, url="https://pve.local:8006",
            user="root@pam", token_name="oasis",
            token_value="test-uuid",
        )
        handler = ProxmoxHandler(config)
        assert await handler.healthy() is False


# ---------------------------------------------------------------------------
# UniFi healthy()
# ---------------------------------------------------------------------------


class TestUnifiHealthy:
    async def test_healthy_when_client_exists(self) -> None:
        from oasisagent.config import UnifiHandlerConfig
        from oasisagent.handlers.unifi import UnifiHandler

        config = UnifiHandlerConfig(
            enabled=True, url="https://unifi.local",
            username="admin", password="secret",
        )
        handler = UnifiHandler(config)
        handler._client = MagicMock()  # Simulate started client

        assert await handler.healthy() is True

    async def test_unhealthy_when_no_client(self) -> None:
        from oasisagent.config import UnifiHandlerConfig
        from oasisagent.handlers.unifi import UnifiHandler

        config = UnifiHandlerConfig(
            enabled=True, url="https://unifi.local",
            username="admin", password="secret",
        )
        handler = UnifiHandler(config)
        assert await handler.healthy() is False


# ---------------------------------------------------------------------------
# Cloudflare healthy()
# ---------------------------------------------------------------------------


class TestCloudflareHealthy:
    async def test_healthy_when_token_verified(self) -> None:
        from oasisagent.config import CloudflareHandlerConfig
        from oasisagent.handlers.cloudflare import CloudflareHandler

        config = CloudflareHandlerConfig(
            enabled=True, api_token="test-token",
        )
        handler = CloudflareHandler(config)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value={"success": True})
        handler._client = mock_client

        assert await handler.healthy() is True

    async def test_unhealthy_when_token_not_verified(self) -> None:
        from oasisagent.config import CloudflareHandlerConfig
        from oasisagent.handlers.cloudflare import CloudflareHandler

        config = CloudflareHandlerConfig(
            enabled=True, api_token="test-token",
        )
        handler = CloudflareHandler(config)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value={"success": False})
        handler._client = mock_client

        assert await handler.healthy() is False

    async def test_unhealthy_when_no_client(self) -> None:
        from oasisagent.config import CloudflareHandlerConfig
        from oasisagent.handlers.cloudflare import CloudflareHandler

        config = CloudflareHandlerConfig(
            enabled=True, api_token="test-token",
        )
        handler = CloudflareHandler(config)
        assert await handler.healthy() is False
