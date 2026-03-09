"""Tests for __main__.py entry point."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oasisagent.__main__ import main
from oasisagent.config import ConfigError


class TestMain:
    def test_missing_config_exits_with_error(self) -> None:
        """Exit code 1 when config.yaml doesn't exist."""
        with (
            patch(
                "oasisagent.__main__.load_config",
                side_effect=ConfigError("not found"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    def test_valid_config_runs_orchestrator(self) -> None:
        """Verify load_config -> Orchestrator -> run() pipeline."""
        mock_config = MagicMock()
        mock_config.agent.log_level.value = "info"

        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()

        with (
            patch("oasisagent.__main__.load_config", return_value=mock_config),
            patch(
                "oasisagent.__main__.Orchestrator",
                return_value=mock_orchestrator,
            ) as mock_orch_cls,
            patch("oasisagent.__main__.asyncio") as mock_asyncio,
        ):
            main()

            mock_orch_cls.assert_called_once_with(mock_config)
            # asyncio.run() is called with the coroutine from orchestrator.run()
            mock_asyncio.run.assert_called_once()
