"""FastAPI middleware for the OasisAgent web application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response

# Paths that are always accessible, even during setup mode
_SETUP_ALLOWED_PREFIXES = (
    "/api/v1/setup",
    "/healthz",
    "/metrics",
    "/docs",
    "/openapi.json",
)


async def setup_guard_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Block non-setup API endpoints when the agent is in setup mode.

    If no admin user exists (setup not complete), only setup endpoints,
    health checks, and metrics are accessible. All other endpoints
    return 403 with a message directing the operator to complete setup.
    """
    # Check if the config store is available (not during tests with mocked state)
    store = getattr(request.app.state, "config_store", None)
    if store is None:
        return await call_next(request)

    # Allow setup-related paths through
    path = request.url.path
    if any(path.startswith(prefix) for prefix in _SETUP_ALLOWED_PREFIXES):
        return await call_next(request)

    # Allow the root path (UI placeholder)
    if path == "/":
        return await call_next(request)

    # Check if setup is needed
    if not await store.has_admin():
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Setup required. Complete the setup wizard at "
                "POST /api/v1/setup/admin before using the API.",
            },
        )

    return await call_next(request)
