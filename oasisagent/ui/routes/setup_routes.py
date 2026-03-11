"""Setup wizard UI routes — first-run flow for new OasisAgent instances."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jwt as pyjwt
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from oasisagent.ui.auth import (
    _JWT_ALGORITHM,
    generate_backup_codes,
    generate_totp_secret,
    generate_totp_uri,
    hash_backup_codes,
    hash_password,
    make_qr_data_url,
    verify_totp,
)

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

    from oasisagent.db.config_store import ConfigStore

router = APIRouter(prefix="/setup", tags=["setup-ui"])


def _get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _get_store(request: Request) -> ConfigStore:
    return request.app.state.config_store


def _get_signing_key(request: Request) -> str:
    return request.app.state.jwt_signing_key


# ---------------------------------------------------------------------------
# Wizard page
# ---------------------------------------------------------------------------


@router.get("", response_model=None)
async def setup_page(request: Request, error: str = "") -> HTMLResponse | RedirectResponse:  # type: ignore[override]
    """Render the setup wizard. Returns 404 if setup is already complete."""
    store = _get_store(request)
    if await store.has_admin():
        return RedirectResponse(url="/ui/login", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "setup/wizard.html",
        {"request": request, "error": error},
    )


@router.post("/admin", response_model=None)
async def setup_create_admin(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> HTMLResponse | RedirectResponse:  # type: ignore[override]
    """Step 1: Create admin account, then redirect to TOTP enrollment."""
    store = _get_store(request)
    templates = _get_templates(request)
    signing_key = _get_signing_key(request)

    if await store.has_admin():
        return RedirectResponse(url="/ui/login", status_code=303)

    # Validation
    if len(username.strip()) == 0:
        return templates.TemplateResponse(
            "setup/wizard.html",
            {"request": request, "error": "Username is required"},
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "setup/wizard.html",
            {"request": request, "error": "Password must be at least 8 characters"},
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            "setup/wizard.html",
            {"request": request, "error": "Passwords do not match"},
        )

    pw_hash = hash_password(password)
    user_id = await store.create_user(
        username=username.strip(),
        password_hash=pw_hash,
        role="admin",
    )

    # Generate TOTP secret and redirect to enrollment page
    secret = generate_totp_secret()
    uri = generate_totp_uri(secret, username.strip())
    qr_data_url = make_qr_data_url(uri)

    # Create a pending token so we can identify the user during enrollment
    import time

    pending_payload = {
        "sub": user_id,
        "username": username.strip(),
        "purpose": "totp_enroll",
        "exp": time.time() + 600,  # 10 minutes
    }
    pending_token = pyjwt.encode(pending_payload, signing_key, algorithm=_JWT_ALGORITHM)

    response = templates.TemplateResponse(
        "auth/totp_enroll.html",
        {
            "request": request,
            "pending_token": pending_token,
            "totp_secret": secret,
            "qr_data_url": qr_data_url,
            "mandatory": True,
            "role": "admin",
            "confirm_url": "/ui/setup/totp-confirm",
            "error": "",
        },
    )
    response.set_cookie("oasis_pending_token", pending_token, httponly=True, max_age=600)
    return response


@router.post("/totp-confirm", response_model=None)
async def setup_totp_confirm(
    request: Request,
    totp_secret: str = Form(...),
    totp_code: str = Form(...),
) -> HTMLResponse | RedirectResponse:  # type: ignore[override]
    """Verify TOTP during setup and save it to the admin account."""
    store = _get_store(request)
    templates = _get_templates(request)
    signing_key = _get_signing_key(request)

    if not verify_totp(totp_secret, totp_code):
        uri = generate_totp_uri(totp_secret, "admin")
        qr_data_url = make_qr_data_url(uri)

        pending_token = request.cookies.get("oasis_pending_token", "")
        return templates.TemplateResponse(
            "auth/totp_enroll.html",
            {
                "request": request,
                "pending_token": pending_token,
                "totp_secret": totp_secret,
                "qr_data_url": qr_data_url,
                "mandatory": True,
                "role": "admin",
                "confirm_url": "/ui/setup/totp-confirm",
                "error": "Invalid code. Scan the QR code and try again.",
            },
        )

    # Find the user from the pending token
    pending_token = request.cookies.get("oasis_pending_token", "")
    user_id = None
    if pending_token:
        try:
            payload = pyjwt.decode(pending_token, signing_key, algorithms=[_JWT_ALGORITHM])
            user_id = payload["sub"]
        except Exception:
            pass

    if not user_id:
        return RedirectResponse(url="/ui/setup", status_code=303)

    # Save TOTP and generate backup codes
    backup_codes = generate_backup_codes()
    hashed_codes = hash_backup_codes(backup_codes)
    await store.update_user(user_id, {
        "totp_secret": totp_secret,
        "totp_confirmed": 1,
        "backup_codes_hash": hashed_codes,
    })

    response = templates.TemplateResponse(
        "auth/backup_codes.html",
        {
            "request": request,
            "backup_codes": backup_codes,
            "next_url": "/ui/setup/core-services",
        },
    )
    response.delete_cookie("oasis_pending_token")
    return response


# ---------------------------------------------------------------------------
# Core services (Step 3)
# ---------------------------------------------------------------------------


@router.get("/core-services", response_class=HTMLResponse)
async def setup_core_services_page(request: Request) -> HTMLResponse:
    """Render the core services configuration step."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "setup/wizard.html",
        {"request": request, "error": "", "step": 3},
    )


@router.post("/core-services", response_model=None)
async def setup_core_services_submit(
    request: Request,
    mqtt_broker: str = Form("mqtt://localhost:1883"),
    mqtt_username: str = Form(""),
    mqtt_password: str = Form(""),
    influxdb_url: str = Form("http://localhost:8086"),
    influxdb_token: str = Form(""),
    influxdb_org: str = Form("myorg"),
    influxdb_bucket: str = Form("oasisagent"),
) -> RedirectResponse:
    """Save core services config and redirect to completion."""
    store = _get_store(request)

    async with store.transaction():
        # MQTT connector
        existing = await store.list_connectors()
        for c in existing:
            if c["name"] == "mqtt-primary":
                await store.delete_connector(c["id"])
        await store.create_connector("mqtt", "mqtt-primary", {
            "broker": mqtt_broker,
            "username": mqtt_username,
            "password": mqtt_password,
        })

        # InfluxDB service
        existing_services = await store.list_services()
        for s in existing_services:
            if s["name"] == "influxdb-primary":
                await store.delete_service(s["id"])
        await store.create_service("influxdb", "influxdb-primary", {
            "url": influxdb_url,
            "token": influxdb_token,
            "org": influxdb_org,
            "bucket": influxdb_bucket,
        })

    return RedirectResponse(url="/ui/setup/complete", status_code=303)


@router.get("/complete")
async def setup_complete(request: Request) -> RedirectResponse:
    """Setup is complete — redirect to login."""
    return RedirectResponse(url="/ui/login", status_code=303)
