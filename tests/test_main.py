"""Tests for __main__.py entry point."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.__main__ import main
from oasisagent.config import ConfigError


class TestMain:
    def test_missing_config_exits_with_error(self) -> None:
        """Exit code 1 when config.yaml doesn't exist (legacy run mode)."""
        with (
            patch("sys.argv", ["oasisagent", "run"]),
            patch(
                "oasisagent.__main__.load_config",
                side_effect=ConfigError("not found"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    def test_run_command_uses_asyncio(self) -> None:
        """Verify 'oasisagent run' uses asyncio.run with orchestrator."""
        mock_config = MagicMock()
        mock_config.agent.log_level.value = "info"

        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()

        with (
            patch("sys.argv", ["oasisagent", "run"]),
            patch("oasisagent.__main__.load_config", return_value=mock_config),
            patch(
                "oasisagent.__main__.Orchestrator",
                return_value=mock_orchestrator,
            ) as mock_orch_cls,
            patch("oasisagent.__main__.asyncio") as mock_asyncio,
        ):
            main()

            mock_orch_cls.assert_called_once_with(mock_config)
            mock_asyncio.run.assert_called_once()

    def test_default_command_starts_uvicorn(self) -> None:
        """Verify default (no subcommand) calls uvicorn.run."""
        with (
            patch("sys.argv", ["oasisagent"]),
            patch("oasisagent.__main__.uvicorn") as mock_uvicorn,
        ):
            main()

            mock_uvicorn.run.assert_called_once_with(
                "oasisagent.web.app:create_app",
                factory=True,
                host="0.0.0.0",
                port=8080,
                log_level="info",
            )

    def test_serve_command_starts_uvicorn(self) -> None:
        """Verify 'oasisagent serve' calls uvicorn.run."""
        with (
            patch("sys.argv", ["oasisagent", "serve"]),
            patch("oasisagent.__main__.uvicorn") as mock_uvicorn,
        ):
            main()

            mock_uvicorn.run.assert_called_once()

    def test_oasis_port_env_var(self) -> None:
        """Verify OASIS_PORT env var is respected."""
        with (
            patch("sys.argv", ["oasisagent"]),
            patch.dict("os.environ", {"OASIS_PORT": "9090"}),
            patch("oasisagent.__main__.uvicorn") as mock_uvicorn,
        ):
            main()

            call_kwargs = mock_uvicorn.run.call_args
            assert call_kwargs.kwargs.get("port") == 9090 or call_kwargs[1].get("port") == 9090
