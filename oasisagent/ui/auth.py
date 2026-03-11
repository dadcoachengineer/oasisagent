"""Authentication and authorization for the web admin UI.

JWT tokens stored in httpOnly cookies with sliding window expiry.
TOTP mandatory for admin + operator roles, optional for viewer.
CSRF protection via per-session token validated on state-changing requests.
Rate limiting via failed_attempts + locked_until columns in users table.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import argon2
import jwt
import pyotp
from fastapi import HTTPException, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.responses import Response

    from oasisagent.db.config_store import ConfigStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JWT_ALGORITHM = "HS256"
_JWT_MAX_LIFETIME_HOURS = 4
_JWT_INACTIVITY_MINUTES = 30
_COOKIE_NAME = "oasis_access_token"
_CSRF_COOKIE_NAME = "oasis_csrf_token"
_CSRF_HEADER_NAME = "x-csrf-token"
_RATE_LIMIT_MAX_ATTEMPTS = 5
_RATE_LIMIT_BASE_SECONDS = 30
_RATE_LIMIT_MAX_SECONDS = 900  # 15 minutes
_BACKUP_CODE_COUNT = 10
_BACKUP_CODE_LENGTH = 8

VALID_ROLES = frozenset({"admin", "operator", "viewer"})

# Argon2 password hasher (default params are secure)
_hasher = argon2.PasswordHasher()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TokenPayload(BaseModel):
    """Claims encoded in the JWT."""

    sub: int  # user ID
    username: str
    role: str
    gen: int  # jwt_generation — for session invalidation
    csrf: str  # CSRF token bound to this session
    iat: float  # issued at (unix timestamp)
    exp: float  # expiry (unix timestamp)


# ---------------------------------------------------------------------------
# Password hashing (argon2id)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against an argon2id hash.

    Returns False on mismatch. Also handles bcrypt hashes for migration
    from the old setup wizard (PR #78 used bcrypt).
    """
    # Handle legacy bcrypt hashes (start with $2b$)
    if password_hash.startswith("$2b$"):
        import bcrypt

        return bcrypt.checkpw(password.encode(), password_hash.encode())

    try:
        return _hasher.verify(password_hash, password)
    except (argon2.exceptions.VerifyMismatchError, argon2.exceptions.InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# JWT signing key derivation
# ---------------------------------------------------------------------------


def derive_jwt_key(master_key: str) -> str:
    """Derive a JWT signing key from the master secret via HMAC-SHA256.

    Never signs JWTs with the raw Fernet key — derived keys limit the
    blast radius if a JWT is compromised.
    """
    derived = hmac.new(
        master_key.encode(), b"oasis-jwt-signing", hashlib.sha256,
    ).digest()
    return derived.hex()


# ---------------------------------------------------------------------------
# JWT token management
# ---------------------------------------------------------------------------


def create_access_token(
    user_id: int,
    username: str,
    role: str,
    jwt_generation: int,
    signing_key: str,
) -> tuple[str, str]:
    """Create a JWT access token with CSRF token embedded.

    Returns:
        Tuple of (jwt_token, csrf_token).
    """
    csrf_token = secrets.token_hex(32)
    now = datetime.now(tz=UTC)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "gen": jwt_generation,
        "csrf": csrf_token,
        "iat": now.timestamp(),
        "exp": (now + timedelta(minutes=_JWT_INACTIVITY_MINUTES)).timestamp(),
    }
    token = jwt.encode(payload, signing_key, algorithm=_JWT_ALGORITHM, headers={"typ": "JWT"})
    return token, csrf_token


def decode_token(token: str, signing_key: str) -> TokenPayload:
    """Decode and validate a JWT token.

    Raises:
        HTTPException: On expired, invalid, or malformed tokens.
    """
    try:
        payload = jwt.decode(
            token, signing_key, algorithms=[_JWT_ALGORITHM],
            options={"verify_sub": False},
        )
        return TokenPayload.model_validate(payload)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired") from None
    except (jwt.InvalidTokenError, Exception):
        raise HTTPException(status_code=401, detail="Invalid session") from None


def should_reissue(payload: TokenPayload) -> bool:
    """Check if the token should be reissued (sliding window).

    Reissues if more than 5 minutes have passed since issuance,
    and the max lifetime hasn't been exceeded.
    """
    now = datetime.now(tz=UTC).timestamp()
    age_seconds = now - payload.iat
    max_lifetime = _JWT_MAX_LIFETIME_HOURS * 3600

    if age_seconds > max_lifetime:
        return False  # Force re-login

    # Reissue if token is more than 5 minutes old
    return age_seconds > 300


def reissue_token(
    payload: TokenPayload,
    signing_key: str,
) -> tuple[str, str] | None:
    """Reissue a token with refreshed expiry if within max lifetime.

    Preserves the original iat to enforce max lifetime. Returns None
    if the max lifetime has been exceeded.
    """
    now = datetime.now(tz=UTC).timestamp()
    age_seconds = now - payload.iat
    max_lifetime = _JWT_MAX_LIFETIME_HOURS * 3600

    if age_seconds > max_lifetime:
        return None

    csrf_token = secrets.token_hex(32)
    new_payload = {
        "sub": payload.sub,
        "username": payload.username,
        "role": payload.role,
        "gen": payload.gen,
        "csrf": csrf_token,
        "iat": payload.iat,  # preserve original issue time
        "exp": (datetime.now(tz=UTC) + timedelta(minutes=_JWT_INACTIVITY_MINUTES)).timestamp(),
    }
    token = jwt.encode(new_payload, signing_key, algorithm=_JWT_ALGORITHM, headers={"typ": "JWT"})
    return token, csrf_token


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------


def validate_csrf(request: Request, token_csrf: str) -> None:
    """Validate CSRF token on state-changing requests.

    Compares the X-CSRF-Token header against the CSRF token embedded
    in the JWT. Only enforced on POST/PUT/DELETE/PATCH.

    Raises:
        HTTPException: 403 on CSRF mismatch.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    header_csrf = request.headers.get(_CSRF_HEADER_NAME, "")
    if not hmac.compare_digest(header_csrf, token_csrf):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


# ---------------------------------------------------------------------------
# TOTP helpers
# ---------------------------------------------------------------------------


def generate_totp_secret() -> str:
    """Generate a new TOTP secret."""
    return pyotp.random_base32()


def generate_totp_uri(secret: str, username: str) -> str:
    """Generate an otpauth:// URI for QR code generation."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name="OasisAgent",
    )


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code (±1 window for clock skew)."""
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def make_qr_data_url(uri: str) -> str:
    """Generate a QR code as a base64 data URL for embedding in HTML."""
    import base64
    import io

    import qrcode
    import qrcode.constants

    qr = qrcode.make(uri, error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------


def generate_backup_codes() -> list[str]:
    """Generate single-use backup codes for 2FA recovery."""
    return [secrets.token_hex(_BACKUP_CODE_LENGTH // 2) for _ in range(_BACKUP_CODE_COUNT)]


def hash_backup_codes(codes: list[str]) -> list[str]:
    """Hash each backup code individually with argon2."""
    return [_hasher.hash(code) for code in codes]


def verify_backup_code(code: str, hashed_codes: list[str]) -> int | None:
    """Check a backup code against stored hashes.

    Returns the index of the matched code (for deletion), or None.
    """
    for i, hashed in enumerate(hashed_codes):
        try:
            if _hasher.verify(hashed, code):
                return i
        except (argon2.exceptions.VerifyMismatchError, argon2.exceptions.InvalidHashError):
            continue
    return None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def check_rate_limit(failed_attempts: int, locked_until: str | None) -> None:
    """Check if the account is locked due to rate limiting.

    Raises:
        HTTPException: 429 if the account is locked.
    """
    if locked_until:
        lock_time = datetime.fromisoformat(locked_until)
        if datetime.now(tz=UTC) < lock_time:
            remaining = (lock_time - datetime.now(tz=UTC)).total_seconds()
            raise HTTPException(
                status_code=429,
                detail=f"Account locked. Try again in {int(remaining)} seconds.",
            )


def calculate_lockout(failed_attempts: int) -> str | None:
    """Calculate lockout timestamp based on failed attempt count.

    Exponential backoff: 30s, 60s, 120s, 240s, ... up to 15 minutes.

    Returns:
        ISO timestamp for locked_until, or None if under threshold.
    """
    if failed_attempts < _RATE_LIMIT_MAX_ATTEMPTS:
        return None

    # Exponential backoff from base
    exponent = failed_attempts - _RATE_LIMIT_MAX_ATTEMPTS
    lockout_seconds = min(
        _RATE_LIMIT_BASE_SECONDS * (2 ** exponent),
        _RATE_LIMIT_MAX_SECONDS,
    )
    lock_time = datetime.now(tz=UTC) + timedelta(seconds=lockout_seconds)
    return lock_time.isoformat()


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def _get_signing_key_from_app(request: Request) -> str:
    """Extract JWT signing key from app state."""
    return request.app.state.jwt_signing_key


async def get_current_user(request: Request) -> TokenPayload:
    """FastAPI dependency: extract and validate the current user from JWT cookie.

    Also validates jwt_generation against the database to catch
    invalidated sessions.

    Raises:
        HTTPException: 401 if not authenticated or session invalidated.
    """
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    signing_key = _get_signing_key_from_app(request)
    payload = decode_token(token, signing_key)

    # Validate jwt_generation against database
    store: ConfigStore = request.app.state.config_store
    user = await store.get_user_by_username(payload.username)
    if not user or user.get("jwt_generation", 0) != payload.gen:
        raise HTTPException(status_code=401, detail="Session invalidated")

    return payload


def require_role(
    *roles: str,
) -> Callable[[Request], Awaitable[TokenPayload]]:
    """Factory for FastAPI dependencies that enforce RBAC.

    Usage::

        @router.get("/admin-only", dependencies=[Depends(require_role("admin"))])
        async def admin_page(): ...
    """

    async def _check_role(request: Request) -> TokenPayload:
        payload = await get_current_user(request)
        if payload.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires role: {', '.join(roles)}",
            )
        # Validate CSRF on state-changing requests
        validate_csrf(request, payload.csrf)
        return payload

    return _check_role


# Convenience dependencies
require_admin = require_role("admin")
require_operator = require_role("admin", "operator")
require_viewer = require_role("admin", "operator", "viewer")


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------


def set_auth_cookies(
    response: Response,
    jwt_token: str,
    csrf_token: str,
    *,
    secure: bool = False,
) -> None:
    """Set the JWT and CSRF cookies on a response."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=_JWT_INACTIVITY_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        key=_CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,  # JS needs to read this for HTMX headers
        secure=secure,
        samesite="lax",
        max_age=_JWT_INACTIVITY_MINUTES * 60,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear auth cookies on a response."""
    response.delete_cookie(key=_COOKIE_NAME, path="/")
    response.delete_cookie(key=_CSRF_COOKIE_NAME, path="/")
