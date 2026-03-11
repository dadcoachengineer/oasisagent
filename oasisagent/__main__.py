"""Entry point for running OasisAgent as a module: python -m oasisagent.

Sub-command routing:
    oasisagent              Start the agent (default)
    oasisagent run          Start the agent (explicit)
    oasisagent queue ...    Approval queue CLI commands
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from oasisagent.cli import build_queue_parser, run_queue_command
from oasisagent.config import ConfigError, load_config
from oasisagent.orchestrator import Orchestrator


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="oasisagent",
        description="OasisAgent — autonomous infrastructure operations agent",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="Start the agent (default)")
    build_queue_parser(subparsers)

    return parser


def _load_file_secrets() -> None:
    """Load Docker/Swarm secrets from *_FILE env vars.

    For each env var ending in ``_FILE``, read the file contents and
    set the corresponding env var (without the ``_FILE`` suffix).
    This is the standard Docker secret pattern: the orchestrator mounts
    secrets as files at ``/run/secrets/``, and services read them via
    ``*_FILE`` environment variables.
    """
    for key in list(os.environ):
        if key.endswith("_FILE"):
            file_path = os.environ[key]
            target_key = key.removesuffix("_FILE")
            try:
                value = Path(file_path).read_text().strip()
                os.environ[target_key] = value
            except OSError:
                logging.getLogger(__name__).debug(
                    "Skipped %s: file %s not found", key, file_path,
                )


def _run_agent() -> None:
    """Load config, create orchestrator, and run the event loop."""
    _load_file_secrets()

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

    log_level = config.agent.log_level.value.upper()
    logging.getLogger().setLevel(log_level)

    # Suppress noisy LiteLLM logs ("Provider List: ..." on every call)
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM Proxy").setLevel(logging.WARNING)
    import litellm
    litellm.suppress_debug_info = True

    orchestrator = Orchestrator(config)
    asyncio.run(orchestrator.run())


def main() -> None:
    """Parse arguments and dispatch to the appropriate command."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "queue":
        run_queue_command(args)
    else:
        # Default: run agent (no command or "run" command)
        _run_agent()


if __name__ == "__main__":
    main()
