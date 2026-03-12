"""Login, logout, and 2FA routes for the web admin UI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jwt as pyjwt
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from oasisagent.ui.auth import (
    _COOKIE_NAME,
    _JWT_ALGORITHM,
    calculate_lockout,
    check_rate_limit,
    clear_auth_cookies,
    create_access_token,
    generate_backup_codes,
    generate_totp_secret,
    generate_totp_uri,
    hash_backup_codes,
    make_qr_data_url,
    set_auth_cookies,
    verify_backup_code,
    verify_password,
    verify_totp,
)

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

    from oasisagent.db.config_store import ConfigStore

router = APIRouter(tags=["auth"])

# Short-lived pending token for 2FA flow (5 minutes)
_PENDING_TOKEN_EXPIRY_SECONDS = 300


def _get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _get_store(request: Request) -> ConfigStore:
    return request.app.state.config_store


def _get_signing_key(request: Request) -> str:
    return request.app.state.jwt_signing_key


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> HTMLResponse:
    """Render the login form."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "auth/login.html",
        {"request": request, "error": error, "username": ""},
    )


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:  # type: ignore[override]
    """Handle login form submission."""
    templates = _get_templates(request)
    store = _get_store(request)
    signing_key = _get_signing_key(request)

    user = await store.get_user_by_username(username)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Invalid username or password", "username": username},
            status_code=401,
        )

    # Rate limiting
    try:
        check_rate_limit(user["failed_attempts"], user["locked_until"])
    except Exception:
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": "Account temporarily locked. Try again later.",
                "username": username,
            },
            status_code=429,
        )

    if not verify_password(password, user["password_hash"]):
        # Increment failed attempts
        new_count = await store.increment_failed_attempts(user["id"])
        lockout = calculate_lockout(new_count)
        if lockout:
            await store.set_lockout(user["id"], lockout)
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Invalid username or password", "username": username},
            status_code=401,
        )

    # Reset failed attempts on successful password check
    await store.reset_failed_attempts(user["id"])

    # Check if TOTP is required
    totp_required = user["role"] in ("admin", "operator")
    has_totp = user.get("totp_confirmed", False)

    if totp_required and has_totp:
        # Issue a short-lived pending token for 2FA step
        pending_payload = {
            "sub": user["id"],
            "username": user["username"],
            "purpose": "2fa_pending",
        }
        import time

        pending_payload["exp"] = time.time() + _PENDING_TOKEN_EXPIRY_SECONDS
        pending_token = pyjwt.encode(pending_payload, signing_key, algorithm=_JWT_ALGORITHM)

        return templates.TemplateResponse(
            "auth/totp_verify.html",
            {"request": request, "pending_token": pending_token, "error": ""},
        )

    if totp_required and not has_totp:
        # Admin/operator without TOTP enrolled — force enrollment
        # Issue pending token, redirect to enrollment
        pending_payload = {
            "sub": user["id"],
            "username": user["username"],
            "purpose": "totp_enroll",
        }
        import time

        pending_payload["exp"] = time.time() + _PENDING_TOKEN_EXPIRY_SECONDS
        pending_token = pyjwt.encode(pending_payload, signing_key, algorithm=_JWT_ALGORITHM)

        secret = generate_totp_secret()
        uri = generate_totp_uri(secret, user["username"])
        qr_data_url = make_qr_data_url(uri)

        response = templates.TemplateResponse(
            "auth/totp_enroll.html",
            {
                "request": request,
                "pending_token": pending_token,
                "totp_secret": secret,
                "qr_data_url": qr_data_url,
                "mandatory": True,
                "role": user["role"],
                "confirm_url": "/ui/login/totp-enroll-confirm",
                "error": "",
            },
        )
        response.set_cookie(
            "oasis_pending_token", pending_token,
            httponly=True, max_age=_PENDING_TOKEN_EXPIRY_SECONDS,
        )
        return response

    # Viewer with optional TOTP — or TOTP-enabled viewer — check TOTP if enrolled
    if has_totp:
        pending_payload = {
            "sub": user["id"],
            "username": user["username"],
            "purpose": "2fa_pending",
        }
        import time

        pending_payload["exp"] = time.time() + _PENDING_TOKEN_EXPIRY_SECONDS
        pending_token = pyjwt.encode(pending_payload, signing_key, algorithm=_JWT_ALGORITHM)
        return templates.TemplateResponse(
            "auth/totp_verify.html",
            {"request": request, "pending_token": pending_token, "error": ""},
        )

    # No TOTP required and not enrolled — issue session directly
    jwt_token, csrf_token = create_access_token(
        user["id"], user["username"], user["role"],
        user["jwt_generation"], signing_key,
    )
    response = RedirectResponse(url="/ui/dashboard", status_code=303)
    set_auth_cookies(response, jwt_token, csrf_token)
    return response


@router.post("/login/2fa", response_model=None)
async def login_2fa(
    request: Request,
    pending_token: str = Form(...),
    totp_code: str = Form(...),
) -> HTMLResponse | RedirectResponse:  # type: ignore[override]
    """Verify TOTP code and complete login."""
    templates = _get_templates(request)
    store = _get_store(request)
    signing_key = _get_signing_key(request)

    # Validate pending token
    try:
        payload = pyjwt.decode(
            pending_token, signing_key,
            algorithms=[_JWT_ALGORITHM], options={"verify_sub": False},
        )
        if payload.get("purpose") != "2fa_pending":
            raise ValueError("Wrong token purpose")
    except Exception:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Session expired. Please sign in again.", "username": ""},
            status_code=401,
        )

    user = await store.get_user_by_id(payload["sub"])
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "User not found.", "username": ""},
            status_code=401,
        )

    # Try TOTP first
    totp_secret = await store.get_user_totp_secret(user["id"])
    if totp_secret and verify_totp(totp_secret, totp_code):
        jwt_token, csrf_token = create_access_token(
            user["id"], user["username"], user["role"],
            user["jwt_generation"], signing_key,
        )
        response = RedirectResponse(url="/ui/dashboard", status_code=303)
        set_auth_cookies(response, jwt_token, csrf_token)
        return response

    # Try backup code
    backup_hashes = user.get("backup_codes_hash", [])
    if backup_hashes:
        match_idx = verify_backup_code(totp_code, backup_hashes)
        if match_idx is not None:
            # Delete used backup code
            remaining = [h for i, h in enumerate(backup_hashes) if i != match_idx]
            await store.update_user(user["id"], {"backup_codes_hash": remaining})

            jwt_token, csrf_token = create_access_token(
                user["id"], user["username"], user["role"],
                user["jwt_generation"], signing_key,
            )
            response = RedirectResponse(url="/ui/dashboard", status_code=303)
            set_auth_cookies(response, jwt_token, csrf_token)
            return response

    return templates.TemplateResponse(
        "auth/totp_verify.html",
        {"request": request, "pending_token": pending_token, "error": "Invalid code. Try again."},
        status_code=401,
    )


@router.post("/login/totp-enroll-confirm", response_model=None)
async def login_totp_enroll_confirm(
    request: Request,
    totp_secret: str = Form(...),
    totp_code: str = Form(...),
) -> HTMLResponse | RedirectResponse:  # type: ignore[override]
    """Confirm TOTP enrollment during forced setup and complete login.

    This is used when an admin/operator logs in without TOTP enrolled.
    """
    templates = _get_templates(request)
    store = _get_store(request)
    signing_key = _get_signing_key(request)

    if not verify_totp(totp_secret, totp_code):
        # Re-render enrollment page with error
        uri = generate_totp_uri(totp_secret, "")
        qr_data_url = make_qr_data_url(uri)
        return templates.TemplateResponse(
            "auth/totp_enroll.html",
            {
                "request": request,
                "totp_secret": totp_secret,
                "qr_data_url": qr_data_url,
                "mandatory": True,
                "role": "",
                "confirm_url": "/ui/login/totp-enroll-confirm",
                "error": "Invalid code. Make sure you scanned the QR code correctly.",
            },
        )

    # Identify the user from the pending token cookie
    pending_token = request.cookies.get("oasis_pending_token", "")
    if not pending_token:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Session expired. Please sign in again.", "username": ""},
            status_code=401,
        )

    try:
        payload = pyjwt.decode(
            pending_token, signing_key,
            algorithms=[_JWT_ALGORITHM], options={"verify_sub": False},
        )
        if payload.get("purpose") != "totp_enroll":
            raise ValueError("Wrong token purpose")
        user = await store.get_user_by_id(payload["sub"])
    except Exception:
        user = None

    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Session expired. Please sign in again.", "username": ""},
            status_code=401,
        )

    # Save TOTP secret and generate backup codes
    backup_codes = generate_backup_codes()
    hashed_codes = hash_backup_codes(backup_codes)
    await store.update_user(user["id"], {
        "totp_secret": totp_secret,
        "totp_confirmed": 1,
        "backup_codes_hash": hashed_codes,
    })

    # Show backup codes page, then redirect to login
    response = templates.TemplateResponse(
        "auth/backup_codes.html",
        {
            "request": request,
            "backup_codes": backup_codes,
            "next_url": "/ui/login",
        },
    )
    response.delete_cookie("oasis_pending_token")
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear auth cookies and increment jwt_generation to invalidate all sessions."""
    store = _get_store(request)
    signing_key = _get_signing_key(request)

    # Try to increment jwt_generation for the current user
    token = request.cookies.get(_COOKIE_NAME)
    if token:
        try:
            payload = pyjwt.decode(
                token, signing_key,
                algorithms=[_JWT_ALGORITHM], options={"verify_sub": False},
            )
            user = await store.get_user_by_id(payload["sub"])
            if user:
                await store.update_user(
                    user["id"],
                    {"jwt_generation": user["jwt_generation"] + 1},
                )
        except Exception:
            pass  # Token already invalid — just clear cookies

    response = RedirectResponse(url="/ui/login", status_code=303)
    clear_auth_cookies(response)
    return response
