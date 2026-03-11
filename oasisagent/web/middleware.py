"""FastAPI middleware for the OasisAgent web application."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, RedirectResponse

from oasisagent.ui.auth import (
    _COOKIE_NAME,
    decode_token,
    reissue_token,
    set_auth_cookies,
    should_reissue,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

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

    Post-setup: redirects unauthenticated UI requests to the login page
    instead of returning raw JSON 401 errors.
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

    response = await call_next(request)

    # Redirect unauthenticated UI requests to login instead of showing JSON 401
    if (
        response.status_code == 401
        and path.startswith("/ui/")
        and "hx-request" not in request.headers
    ):
        return RedirectResponse(url="/ui/login", status_code=303)

    return response


async def sliding_window_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Reissue JWT tokens on authenticated requests to implement sliding window expiry.

    Checks the current JWT cookie on each response. If the token is valid and
    older than 5 minutes (but within the 4-hour max lifetime), reissues it with
    a fresh 30-minute expiry window. This keeps active sessions alive while
    enforcing the max lifetime.
    """
    response = await call_next(request)

    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        return response

    signing_key = getattr(request.app.state, "jwt_signing_key", None)
    if not signing_key:
        return response

    try:
        payload = decode_token(token, signing_key)
        if should_reissue(payload):
            result = reissue_token(payload, signing_key)
            if result:
                new_token, new_csrf = result
                set_auth_cookies(response, new_token, new_csrf)
    except Exception:
        pass  # Invalid/expired token — let the auth dependency handle it

    return response
