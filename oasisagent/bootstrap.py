"""Shared bootstrap utilities used by both __main__ and the FastAPI lifespan.

Extracted here so that ``oasisagent.web.app`` doesn't import private
functions from ``__main__``, which is an entry point — not a library module.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oasisagent.config import OasisAgentConfig


def load_file_secrets() -> None:
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


def configure_logging(config: OasisAgentConfig) -> None:
    """Set up logging from config. Suppresses noisy LiteLLM output."""
    log_level = config.agent.log_level.value.upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger().setLevel(log_level)

    # Suppress noisy LiteLLM logs ("Provider List: ..." on every call)
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM Proxy").setLevel(logging.WARNING)
    import litellm
    litellm.suppress_debug_info = True
