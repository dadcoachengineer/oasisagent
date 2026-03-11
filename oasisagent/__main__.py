"""Entry point for running OasisAgent as a module: python -m oasisagent.

Sub-command routing:
    oasisagent              Start the web server (default)
    oasisagent serve        Start the web server (explicit)
    oasisagent run          Start the agent without web server (legacy)
    oasisagent queue ...    Approval queue CLI commands
    oasisagent config ...   Config import/export CLI commands
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn

from oasisagent.bootstrap import configure_logging, load_file_secrets
from oasisagent.cli import build_queue_parser, run_queue_command
from oasisagent.cli_config import build_config_parser, run_config_command
from oasisagent.config import ConfigError, load_config
from oasisagent.orchestrator import Orchestrator


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="oasisagent",
        description="OasisAgent — autonomous infrastructure operations agent",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Start the web server (default)")
    subparsers.add_parser("run", help="Start the agent without web server (legacy)")
    build_queue_parser(subparsers)
    build_config_parser(subparsers)

    return parser


def _serve() -> None:
    """Start the FastAPI web server with uvicorn."""
    port = int(os.environ.get("OASIS_PORT", "8080"))
    log_level = os.environ.get("OASIS_LOG_LEVEL", "info").lower()

    uvicorn.run(
        "oasisagent.web.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=port,
        log_level=log_level,
    )


def _run_agent() -> None:
    """Load config, create orchestrator, and run the event loop (legacy).

    This is the standalone mode without a web server. Kept for
    debugging and backward compatibility. Production deployments
    should use ``oasisagent serve`` (the default).
    """
    load_file_secrets()

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

    configure_logging(config)

    orchestrator = Orchestrator(config)
    asyncio.run(orchestrator.run())


def main() -> None:
    """Parse arguments and dispatch to the appropriate command."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "queue":
        run_queue_command(args)
    elif args.command == "config":
        run_config_command(args)
    elif args.command == "run":
        _run_agent()
    else:
        # Default: serve (no command or "serve" command)
        _serve()


if __name__ == "__main__":
    main()
