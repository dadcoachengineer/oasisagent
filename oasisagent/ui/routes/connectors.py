"""Connector, service, and notification CRUD UI routes.

Factory-based registration: ``_build_ui_crud`` registers all seven routes
(list, type picker, create, edit form, update, delete, toggle) for each
category.  Form data is parsed against ``FieldSpec`` definitions from
``oasisagent.ui.form_specs``.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from oasisagent.db.registry import (
    CONNECTOR_TYPES,
    CORE_SERVICE_TYPES,
    NOTIFICATION_TYPES,
)
from oasisagent.ui.auth import TokenPayload, require_admin, require_viewer
from oasisagent.ui.form_specs import (
    SINGLE_INSTANCE_TYPES,
    FieldSpec,
    get_description,
    get_display_name,
    get_form_specs,
)

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

    from oasisagent.db.config_store import ConfigStore

router = APIRouter(tags=["connectors-ui"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _get_store(request: Request) -> ConfigStore:
    return request.app.state.config_store


def _base_context(
    request: Request, current_user: TokenPayload,
) -> dict[str, Any]:
    """Context keys required by every template."""
    return {
        "request": request,
        "current_user": current_user,
        "csrf_token": current_user.csrf,
        "version": request.app.version,
    }


def _parse_form_config(
    form_data: dict[str, Any],
    specs: list[FieldSpec],
    *,
    is_edit: bool = False,
) -> dict[str, Any]:
    """Build a config dict from submitted form data using the FieldSpec list.

    Handles type coercion, checkbox absence, list_str splitting, and
    password-on-edit semantics (empty password → omit to preserve existing).
    """
    config: dict[str, Any] = {}

    for spec in specs:
        raw = form_data.get(spec.name)

        if spec.input_type == "checkbox":
            # Checkbox: present in form data → True, absent → False
            config[spec.name] = spec.name in form_data
            continue

        if spec.input_type == "password":
            value = (raw or "").strip() if raw else ""
            if is_edit and value == "":
                # On edit, empty password means "keep existing" — omit
                continue
            config[spec.name] = value
            continue

        if spec.input_type == "number":
            value = (raw or "").strip() if raw else ""
            if value == "":
                if spec.default is not None:
                    config[spec.name] = spec.default
                continue
            config[spec.name] = int(value)
            continue

        if spec.input_type == "float":
            value = (raw or "").strip() if raw else ""
            if value == "":
                if spec.default is not None:
                    config[spec.name] = spec.default
                continue
            config[spec.name] = float(value)
            continue

        if spec.input_type == "list_str":
            text = (raw or "").strip() if raw else ""
            if text:
                config[spec.name] = [
                    line.strip() for line in text.split("\n") if line.strip()
                ]
            else:
                config[spec.name] = []
            continue

        # text, select, textarea — pass as string
        value = (raw or "").strip() if raw else ""
        if value:
            config[spec.name] = value
        elif spec.default is not None:
            config[spec.name] = spec.default

    return config


def _form_errors_from_validation(exc: ValidationError) -> list[str]:
    """Extract human-readable error messages from a Pydantic ValidationError."""
    errors: list[str] = []
    for err in exc.errors():
        loc = " → ".join(str(p) for p in err["loc"]) if err["loc"] else "config"
        errors.append(f"{loc}: {err['msg']}")
    return errors


# ---------------------------------------------------------------------------
# Generic CRUD factory
# ---------------------------------------------------------------------------


def _build_ui_crud(
    *,
    url_prefix: str,
    table: str,
    title: str,
    type_registry: dict[str, Any],
    list_method: str,
    get_method: str,
    create_method: str,
    update_method: str,
    delete_method: str,
) -> None:
    """Register all CRUD UI routes for a category on the module-level router."""

    # -----------------------------------------------------------------------
    # GET /{category} — list
    # -----------------------------------------------------------------------

    @router.get(f"/{url_prefix}", response_class=HTMLResponse, name=f"{url_prefix}_list")
    async def list_page(
        request: Request,
        current_user: TokenPayload = Depends(require_viewer),
    ) -> HTMLResponse:
        store = _get_store(request)
        rows = await getattr(store, list_method)()
        masked = [store.mask_row(table, r) for r in rows]
        templates = _get_templates(request)
        return templates.TemplateResponse(
            "connectors/list.html",
            {
                **_base_context(request, current_user),
                "rows": masked,
                "table": url_prefix,
                "title": title,
            },
        )

    # -----------------------------------------------------------------------
    # GET /{category}/new — type picker or create form
    # -----------------------------------------------------------------------

    @router.get(f"/{url_prefix}/new", response_class=HTMLResponse, name=f"{url_prefix}_new")
    async def new_page(
        request: Request,
        type: str | None = None,
        current_user: TokenPayload = Depends(require_admin),
    ) -> HTMLResponse:
        templates = _get_templates(request)
        store = _get_store(request)
        ctx = _base_context(request, current_user)

        if type is None:
            # Show type picker — build list with existing-instance info
            existing_rows = await getattr(store, list_method)()
            existing_types = {r["type"] for r in existing_rows}
            type_list = [
                {
                    "type": t,
                    "display_name": get_display_name(t),
                    "description": get_description(t),
                    "exists": t in existing_types,
                    "single_instance": t in SINGLE_INSTANCE_TYPES,
                }
                for t in type_registry
            ]
            return templates.TemplateResponse(
                "connectors/_type_picker.html",
                {
                    **ctx,
                    "table": url_prefix,
                    "title": title,
                    "types": type_list,
                },
            )

        # Validate the requested type
        if type not in type_registry:
            raise HTTPException(status_code=404, detail=f"Unknown type: {type}")

        specs = get_form_specs(type)
        return templates.TemplateResponse(
            "connectors/form.html",
            {
                **ctx,
                "table": url_prefix,
                "title": title,
                "mode": "create",
                "type_name": type,
                "type_display_name": get_display_name(type),
                "specs": specs,
                "config": {},
                "name": "",
                "errors": [],
            },
        )

    # -----------------------------------------------------------------------
    # POST /{category} — create
    # -----------------------------------------------------------------------

    @router.post(f"/{url_prefix}", response_class=HTMLResponse, name=f"{url_prefix}_create")
    async def create_item(
        request: Request,
        current_user: TokenPayload = Depends(require_admin),
    ) -> Response:
        store = _get_store(request)
        templates = _get_templates(request)
        form = await request.form()
        form_data = dict(form)

        type_name = str(form_data.get("type", ""))
        name = str(form_data.get("name", "")).strip()

        if type_name not in type_registry:
            raise HTTPException(status_code=422, detail=f"Unknown type: {type_name}")

        specs = get_form_specs(type_name)
        config = _parse_form_config(form_data, specs, is_edit=False)

        try:
            await getattr(store, create_method)(type_name, name, config)
        except (ValueError, ValidationError) as exc:
            errors = (
                _form_errors_from_validation(exc)
                if isinstance(exc, ValidationError)
                else [str(exc)]
            )
            # Re-render form with errors — clear password fields
            config_display = {
                k: v for k, v in config.items()
                if not any(s.name == k and s.input_type == "password" for s in specs)
            }
            return templates.TemplateResponse(
                "connectors/form.html",
                {
                    **_base_context(request, current_user),
                    "table": url_prefix,
                    "title": title,
                    "mode": "create",
                    "type_name": type_name,
                    "type_display_name": get_display_name(type_name),
                    "specs": specs,
                    "config": config_display,
                    "name": name,
                    "errors": errors,
                },
                status_code=422,
            )
        except sqlite3.IntegrityError:
            config_display = {
                k: v for k, v in config.items()
                if not any(s.name == k and s.input_type == "password" for s in specs)
            }
            return templates.TemplateResponse(
                "connectors/form.html",
                {
                    **_base_context(request, current_user),
                    "table": url_prefix,
                    "title": title,
                    "mode": "create",
                    "type_name": type_name,
                    "type_display_name": get_display_name(type_name),
                    "specs": specs,
                    "config": config_display,
                    "name": name,
                    "errors": [f"Name already exists: {name}"],
                },
                status_code=422,
            )

        # Success — redirect to list via HTMX or standard redirect
        response = HTMLResponse(content="", status_code=204)
        response.headers["HX-Redirect"] = f"/ui/{url_prefix}"
        return response

    # -----------------------------------------------------------------------
    # GET /{category}/{id}/edit — edit form
    # -----------------------------------------------------------------------

    @router.get(
        f"/{url_prefix}/{{row_id}}/edit",
        response_class=HTMLResponse,
        name=f"{url_prefix}_edit",
    )
    async def edit_page(
        request: Request,
        row_id: int,
        current_user: TokenPayload = Depends(require_admin),
    ) -> HTMLResponse:
        store = _get_store(request)
        row = await getattr(store, get_method)(row_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")

        type_name = row["type"]
        specs = get_form_specs(type_name)
        # Mask secrets in config for display
        masked = store.mask_row(table, row)
        templates = _get_templates(request)
        return templates.TemplateResponse(
            "connectors/form.html",
            {
                **_base_context(request, current_user),
                "table": url_prefix,
                "title": title,
                "mode": "edit",
                "row_id": row_id,
                "type_name": type_name,
                "type_display_name": get_display_name(type_name),
                "specs": specs,
                "config": masked["config"],
                "name": row["name"],
                "errors": [],
            },
        )

    # -----------------------------------------------------------------------
    # POST /{category}/{id} — update
    # -----------------------------------------------------------------------

    @router.post(
        f"/{url_prefix}/{{row_id}}",
        response_class=HTMLResponse,
        name=f"{url_prefix}_update",
    )
    async def update_item(
        request: Request,
        row_id: int,
        current_user: TokenPayload = Depends(require_admin),
    ) -> Response:
        store = _get_store(request)
        templates = _get_templates(request)

        existing = await getattr(store, get_method)(row_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Not found")

        form = await request.form()
        form_data = dict(form)
        type_name = existing["type"]
        name = str(form_data.get("name", existing["name"])).strip()
        specs = get_form_specs(type_name)
        config = _parse_form_config(form_data, specs, is_edit=True)

        # Build the update payload
        updates: dict[str, Any] = {"name": name, "config": config}

        try:
            result = await getattr(store, update_method)(row_id, updates)
        except (ValueError, ValidationError) as exc:
            errors = (
                _form_errors_from_validation(exc)
                if isinstance(exc, ValidationError)
                else [str(exc)]
            )
            # Re-render form with errors — show current values, clear passwords
            config_display = {**existing["config"], **config}
            for spec in specs:
                if spec.input_type == "password":
                    config_display.pop(spec.name, None)
            return templates.TemplateResponse(
                "connectors/form.html",
                {
                    **_base_context(request, current_user),
                    "table": url_prefix,
                    "title": title,
                    "mode": "edit",
                    "row_id": row_id,
                    "type_name": type_name,
                    "type_display_name": get_display_name(type_name),
                    "specs": specs,
                    "config": config_display,
                    "name": name,
                    "errors": errors,
                },
                status_code=422,
            )

        if result is None:
            raise HTTPException(status_code=404, detail="Not found")

        response = HTMLResponse(content="", status_code=204)
        response.headers["HX-Redirect"] = f"/ui/{url_prefix}"
        return response

    # -----------------------------------------------------------------------
    # POST /{category}/{id}/delete — delete
    # -----------------------------------------------------------------------

    @router.post(
        f"/{url_prefix}/{{row_id}}/delete",
        response_class=HTMLResponse,
        name=f"{url_prefix}_delete",
    )
    async def delete_item(
        request: Request,
        row_id: int,
        current_user: TokenPayload = Depends(require_admin),
    ) -> Response:
        store = _get_store(request)
        deleted = await getattr(store, delete_method)(row_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Not found")
        response = HTMLResponse(content="", status_code=204)
        response.headers["HX-Redirect"] = f"/ui/{url_prefix}"
        return response

    # -----------------------------------------------------------------------
    # POST /{category}/{id}/toggle — enable/disable (HTMX partial)
    # -----------------------------------------------------------------------

    @router.post(
        f"/{url_prefix}/{{row_id}}/toggle",
        response_class=HTMLResponse,
        name=f"{url_prefix}_toggle",
    )
    async def toggle_item(
        request: Request,
        row_id: int,
        current_user: TokenPayload = Depends(require_admin),
    ) -> HTMLResponse:
        store = _get_store(request)
        existing = await getattr(store, get_method)(row_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Not found")

        new_enabled = not existing["enabled"]
        updated = await getattr(store, update_method)(
            row_id, {"enabled": new_enabled},
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Not found")

        masked = store.mask_row(table, updated)
        templates = _get_templates(request)
        return templates.TemplateResponse(
            "connectors/_row.html",
            {
                **_base_context(request, current_user),
                "row": masked,
                "table": url_prefix,
            },
        )


# ---------------------------------------------------------------------------
# Register CRUD routes for all three categories
# ---------------------------------------------------------------------------

_build_ui_crud(
    url_prefix="connectors",
    table="connectors",
    title="Connectors",
    type_registry=CONNECTOR_TYPES,
    list_method="list_connectors",
    get_method="get_connector",
    create_method="create_connector",
    update_method="update_connector",
    delete_method="delete_connector",
)

_build_ui_crud(
    url_prefix="services",
    table="core_services",
    title="Services",
    type_registry=CORE_SERVICE_TYPES,
    list_method="list_services",
    get_method="get_service",
    create_method="create_service",
    update_method="update_service",
    delete_method="delete_service",
)

_build_ui_crud(
    url_prefix="notifications",
    table="notification_channels",
    title="Notification Channels",
    type_registry=NOTIFICATION_TYPES,
    list_method="list_notifications",
    get_method="get_notification",
    create_method="create_notification",
    update_method="update_notification",
    delete_method="delete_notification",
)
