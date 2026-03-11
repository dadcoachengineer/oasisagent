"""FastAPI application factory — single process for web UI, API, and webhooks.

ARCHITECTURE.md §17.1 defines the single-process design:
- ``/`` — Web UI (placeholder until v0.3.1)
- ``/api/v1/`` — REST API
- ``/ingest/webhook/`` — Webhook receiver (placeholder until #51)
- ``/healthz`` — Health check
- ``/metrics`` — Prometheus metrics
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from oasisagent.web.api import router as api_router
from oasisagent.web.webhook import router as webhook_router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


def _get_version() -> str:
    """Read package version from installed metadata."""
    try:
        return importlib.metadata.version("oasisagent")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage orchestrator lifecycle within the FastAPI process."""
    from oasisagent.__main__ import _configure_logging, _load_file_secrets
    from oasisagent.config import load_config
    from oasisagent.orchestrator import Orchestrator

    _load_file_secrets()

    from pathlib import Path

    config = load_config(Path("config.yaml"))
    _configure_logging(config)

    orchestrator = Orchestrator(config)
    await orchestrator.start()

    loop_task = asyncio.create_task(orchestrator.run_loop(), name="orchestrator-loop")
    app.state.orchestrator = orchestrator
    app.state.start_time = time.monotonic()

    yield

    # Shutdown
    await orchestrator.stop()
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


def create_app() -> FastAPI:
    """Create the FastAPI application with all route mounts."""
    app = FastAPI(
        title="OasisAgent",
        version=_get_version(),
        lifespan=lifespan,
    )

    # --- Routes ---

    app.include_router(api_router, prefix="/api/v1")
    app.include_router(webhook_router, prefix="/ingest/webhook")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok", "version": _get_version()}

    @app.get("/metrics", tags=["observability"])
    async def metrics() -> Response:
        """Prometheus metrics endpoint."""
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        from oasisagent.metrics import REGISTRY, _update_callback_gauges

        _update_callback_gauges()
        body = generate_latest(REGISTRY)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    @app.get("/", response_class=HTMLResponse, tags=["ui"])
    async def index() -> str:
        """Placeholder UI — replaced by HTMX dashboard in v0.3.1."""
        return (
            "<!DOCTYPE html><html><head><title>OasisAgent</title></head>"
            "<body><h1>OasisAgent</h1>"
            f"<p>Version {_get_version()}</p>"
            "<p>Web admin UI coming in v0.3.1.</p>"
            "</body></html>"
        )

    return app
