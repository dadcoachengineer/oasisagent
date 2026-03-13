"""Main UI router — combines all UI sub-routers."""

from __future__ import annotations

from fastapi import APIRouter

from oasisagent.ui.routes.approvals import router as approvals_router
from oasisagent.ui.routes.auth_routes import router as auth_router
from oasisagent.ui.routes.connectors import router as connectors_router
from oasisagent.ui.routes.dashboard import router as dashboard_router
from oasisagent.ui.routes.events import router as events_router
from oasisagent.ui.routes.known_fixes import router as known_fixes_router
from oasisagent.ui.routes.notifications import router as notifications_router
from oasisagent.ui.routes.setup_routes import router as setup_router
from oasisagent.ui.routes.users import router as users_router

ui_router = APIRouter()

# Auth (login/logout) — no prefix, available at /ui/login, /ui/logout
ui_router.include_router(auth_router)

# Setup wizard — /ui/setup/*
ui_router.include_router(setup_router)

# Dashboard — /ui/dashboard, /ui/events/stream
ui_router.include_router(dashboard_router)

# Config CRUD pages — /ui/connectors, /ui/services, /ui/channels
ui_router.include_router(connectors_router)

# Notification feed — /ui/notifications
ui_router.include_router(notifications_router)

# Approvals — /ui/approvals
ui_router.include_router(approvals_router)

# Events — /ui/events
ui_router.include_router(events_router)

# Known fixes — /ui/known-fixes
ui_router.include_router(known_fixes_router)

# Users — /ui/users
ui_router.include_router(users_router)
