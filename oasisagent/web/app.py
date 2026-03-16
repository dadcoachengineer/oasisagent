"""FastAPI application factory — single process for web UI, API, and webhooks.

ARCHITECTURE.md §17.1 defines the single-process design:
- ``/ui/`` — Web admin UI (HTMX + Jinja2)
- ``/api/v1/`` — REST API
- ``/ingest/webhook/`` — Webhook receiver
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
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from oasisagent.ui.auth import derive_jwt_key
from oasisagent.ui.router import ui_router
from oasisagent.web.api import router as api_router
from oasisagent.web.api_config import (
    connectors_router,
    notifications_router,
    services_router,
)
from oasisagent.web.api_setup import router as setup_router
from oasisagent.web.middleware import setup_guard_middleware, sliding_window_middleware
from oasisagent.web.webhook import router as webhook_router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

# Template and static file paths
_UI_DIR = Path(__file__).parent.parent / "ui"
_TEMPLATES_DIR = _UI_DIR / "templates"
_STATIC_DIR = _UI_DIR / "static"


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
    from oasisagent.audit.reader import AuditReader
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

    # Load config: SQLite is the single source of truth.
    # On first run with a virgin DB, import config.yaml into SQLite
    # so both the orchestrator and the web UI read the same data.
    if await store.is_virgin():
        yaml_path = Path("config.yaml")
        if yaml_path.exists():
            logger.info("Virgin database — importing %s into SQLite", yaml_path)
            yaml_config = load_config(yaml_path)
            await store.import_yaml(yaml_config)
        else:
            logger.info("Virgin database, no config.yaml — using defaults")
    config = await store.load_config()

    configure_logging(config)

    orchestrator = Orchestrator(config, db=db, config_store=store)
    await orchestrator.start()

    loop_task = asyncio.create_task(orchestrator.run_loop(), name="orchestrator-loop")
    app.state.orchestrator = orchestrator
    app.state.config_store = store
    app.state.crypto = crypto
    app.state.db = db
    app.state.start_time = time.monotonic()
    app.state.jwt_signing_key = derive_jwt_key(secret_key)
    app.state.notification_store = orchestrator._notification_store
    app.state.web_notification_channel = orchestrator._web_channel
    app.state.topology_store = orchestrator._topology_store
    app.state.service_graph = orchestrator._service_graph

    # Audit reader for Event Explorer UI (None if InfluxDB disabled)
    audit_reader: AuditReader | None = None
    if config.audit.influxdb.enabled:
        audit_reader = AuditReader(config.audit)
        await audit_reader.start()
    app.state.audit_reader = audit_reader

    yield

    # Shutdown
    if audit_reader is not None:
        await audit_reader.stop()
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

    # --- Jinja2 templates ---
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    # --- Middleware (outermost first) ---
    app.middleware("http")(setup_guard_middleware)
    app.middleware("http")(sliding_window_middleware)

    # --- Static files ---
    app.mount("/ui/static", StaticFiles(directory=str(_STATIC_DIR)), name="ui-static")

    # --- Routes ---

    # REST API
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(setup_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(services_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(webhook_router, prefix="/ingest/webhook")

    # Web UI
    app.include_router(ui_router, prefix="/ui")

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

    @app.get("/", tags=["ui"])
    async def index() -> RedirectResponse:
        """Redirect root to the web UI dashboard."""
        return RedirectResponse(url="/ui/dashboard", status_code=303)

    return app
