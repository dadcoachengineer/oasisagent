"""FastAPI middleware for the OasisAgent web application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, RedirectResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response

# Paths that are always accessible, even during setup mode
_SETUP_ALLOWED_PREFIXES = (
    "/api/v1/setup",
    "/ui/setup",
    "/ui/login",
    "/ui/logout",
    "/ui/static",
    "/healthz",
    "/metrics",
    "/docs",
    "/openapi.json",
)


async def setup_guard_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Block non-setup endpoints when the agent is in setup mode.

    During setup mode (no admin user exists):
    - Setup and auth UI paths are allowed through
    - API setup endpoints are allowed through
    - Health/metrics are allowed through
    - All other paths redirect to setup (UI) or return 403 (API)
    """
    # Check if the config store is available (not during tests with mocked state)
    store = getattr(request.app.state, "config_store", None)
    if store is None:
        return await call_next(request)

    # Allow setup-related and infrastructure paths through
    path = request.url.path
    if any(path.startswith(prefix) for prefix in _SETUP_ALLOWED_PREFIXES):
        return await call_next(request)

    # Allow the root path (redirects to /ui/dashboard)
    if path == "/":
        return await call_next(request)

    # Check if setup is needed
    if not await store.has_admin():
        # Redirect UI paths to setup wizard, block API paths
        if path.startswith("/ui/"):
            return RedirectResponse(url="/ui/setup", status_code=303)
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Setup required. Complete the setup wizard at "
                "/ui/setup before using the API.",
            },
        )

    return await call_next(request)
