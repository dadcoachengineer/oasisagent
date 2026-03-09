"""Entry point for running OasisAgent as a module: python -m oasisagent."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from oasisagent.config import ConfigError, load_config
from oasisagent.orchestrator import Orchestrator


def main() -> None:
    """Load config, create orchestrator, and run the event loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path("config.yaml")
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        sys.exit(1)

    # Apply log level from config
    log_level = config.agent.log_level.value.upper()
    logging.getLogger().setLevel(log_level)

    orchestrator = Orchestrator(config)
    asyncio.run(orchestrator.run())


if __name__ == "__main__":
    main()
