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
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from oasisagent.web.api import router as api_router
from oasisagent.web.api_config import (
    connectors_router,
    notifications_router,
    services_router,
)
from oasisagent.web.api_setup import router as setup_router
from oasisagent.web.middleware import setup_guard_middleware
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
    """Manage orchestrator lifecycle within the FastAPI process.

    Config loading order:
    1. Initialize SQLite database (run migrations)
    2. If DB is virgin (no config rows) AND config.yaml exists on disk,
       fall back to YAML loader. This is a one-way door: once any config
       is written to SQLite, YAML is permanently ignored.
    3. Otherwise, load config from SQLite.
    """
    from oasisagent.bootstrap import configure_logging, load_file_secrets
    from oasisagent.config import load_config
    from oasisagent.db.config_store import ConfigStore
    from oasisagent.db.crypto import CryptoProvider, resolve_secret_key
    from oasisagent.db.schema import run_migrations
    from oasisagent.orchestrator import Orchestrator

    load_file_secrets()

    # Bootstrap env vars
    data_dir = Path(os.environ.get("OASIS_DATA_DIR", "/data"))
    secret_key = resolve_secret_key(data_dir)
    crypto = CryptoProvider(secret_key)

    # Initialize database
    db = await run_migrations(data_dir / "oasisagent.db")
    store = ConfigStore(db, crypto)

    # Load config: SQLite or YAML fallback for virgin databases
    if await store.is_virgin():
        yaml_path = Path("config.yaml")
        if yaml_path.exists():
            logger.info("Virgin database — loading config from %s", yaml_path)
            config = load_config(yaml_path)
        else:
            logger.info("Virgin database, no config.yaml — using defaults")
            config = await store.load_config()
    else:
        config = await store.load_config()

    configure_logging(config)

    orchestrator = Orchestrator(config)
    await orchestrator.start()

    loop_task = asyncio.create_task(orchestrator.run_loop(), name="orchestrator-loop")
    app.state.orchestrator = orchestrator
    app.state.config_store = store
    app.state.db = db
    app.state.start_time = time.monotonic()

    yield

    # Shutdown
    await orchestrator.stop()
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    await db.close()


def create_app() -> FastAPI:
    """Create the FastAPI application with all route mounts."""
    app = FastAPI(
        title="OasisAgent",
        version=_get_version(),
        lifespan=lifespan,
    )

    # --- Middleware ---
    app.middleware("http")(setup_guard_middleware)

    # --- Routes ---

    app.include_router(api_router, prefix="/api/v1")
    app.include_router(setup_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(services_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
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
