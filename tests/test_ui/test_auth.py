"""Tests for the auth module — JWT, TOTP, passwords, rate limiting."""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from oasisagent.ui.auth import (
    _RATE_LIMIT_MAX_ATTEMPTS,
    TokenPayload,
    calculate_lockout,
    check_rate_limit,
    create_access_token,
    decode_token,
    derive_jwt_key,
    generate_backup_codes,
    generate_totp_secret,
    generate_totp_uri,
    hash_backup_codes,
    hash_password,
    reissue_token,
    should_reissue,
    verify_backup_code,
    verify_password,
    verify_totp,
)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_hash_and_verify(self) -> None:
        pw_hash = hash_password("mysecretpassword")
        assert verify_password("mysecretpassword", pw_hash)

    def test_wrong_password(self) -> None:
        pw_hash = hash_password("correct")
        assert not verify_password("wrong", pw_hash)

    def test_bcrypt_legacy_hash(self) -> None:
        """Old bcrypt hashes from setup wizard should still verify."""
        import bcrypt

        legacy_hash = bcrypt.hashpw(b"legacypass", bcrypt.gensalt()).decode()
        assert verify_password("legacypass", legacy_hash)

    def test_bcrypt_legacy_wrong_password(self) -> None:
        import bcrypt

        legacy_hash = bcrypt.hashpw(b"legacypass", bcrypt.gensalt()).decode()
        assert not verify_password("wrongpass", legacy_hash)

    def test_invalid_hash_returns_false(self) -> None:
        assert not verify_password("anything", "not-a-valid-hash")


# ---------------------------------------------------------------------------
# JWT key derivation
# ---------------------------------------------------------------------------


class TestJWTKeyDerivation:
    def test_derived_key_is_deterministic(self) -> None:
        key1 = derive_jwt_key("master-secret")
        key2 = derive_jwt_key("master-secret")
        assert key1 == key2

    def test_different_master_different_derived(self) -> None:
        key1 = derive_jwt_key("secret-a")
        key2 = derive_jwt_key("secret-b")
        assert key1 != key2

    def test_derived_key_is_hex_string(self) -> None:
        key = derive_jwt_key("master")
        assert len(key) == 64  # 32 bytes hex-encoded
        int(key, 16)  # Should not raise


# ---------------------------------------------------------------------------
# JWT token management
# ---------------------------------------------------------------------------


class TestJWTTokens:
    def test_create_and_decode(self) -> None:
        signing_key = derive_jwt_key("test-master")
        token, csrf = create_access_token(
            user_id=1, username="admin", role="admin",
            jwt_generation=0, signing_key=signing_key,
        )
        payload = decode_token(token, signing_key)
        assert payload.sub == 1
        assert payload.username == "admin"
        assert payload.role == "admin"
        assert payload.gen == 0
        assert payload.csrf == csrf

    def test_expired_token_raises(self) -> None:
        import jwt

        signing_key = derive_jwt_key("test-master")
        expired_payload = {
            "sub": 1, "username": "admin", "role": "admin",
            "gen": 0, "csrf": "abc", "iat": time.time() - 7200,
            "exp": time.time() - 3600,
        }
        token = jwt.encode(expired_payload, signing_key, algorithm="HS256")
        with pytest.raises(Exception, match="expired"):
            decode_token(token, signing_key)

    def test_invalid_token_raises(self) -> None:
        signing_key = derive_jwt_key("test-master")
        with pytest.raises(Exception, match="Invalid"):
            decode_token("not.a.token", signing_key)

    def test_wrong_key_raises(self) -> None:
        signing_key = derive_jwt_key("correct-key")
        wrong_key = derive_jwt_key("wrong-key")
        token, _ = create_access_token(
            user_id=1, username="admin", role="admin",
            jwt_generation=0, signing_key=signing_key,
        )
        with pytest.raises(HTTPException):
            decode_token(token, wrong_key)

    def test_should_reissue_fresh_token(self) -> None:
        signing_key = derive_jwt_key("test")
        token, _ = create_access_token(
            user_id=1, username="admin", role="admin",
            jwt_generation=0, signing_key=signing_key,
        )
        payload = decode_token(token, signing_key)
        # Fresh token should not need reissue
        assert not should_reissue(payload)

    def test_should_reissue_old_token(self) -> None:
        payload = TokenPayload(
            sub=1, username="admin", role="admin", gen=0,
            csrf="abc", iat=time.time() - 400, exp=time.time() + 1000,
        )
        assert should_reissue(payload)

    def test_reissue_preserves_iat(self) -> None:
        signing_key = derive_jwt_key("test")
        original_iat = time.time() - 400
        payload = TokenPayload(
            sub=1, username="admin", role="admin", gen=0,
            csrf="abc", iat=original_iat, exp=time.time() + 1000,
        )
        result = reissue_token(payload, signing_key)
        assert result is not None
        new_token, new_csrf = result
        new_payload = decode_token(new_token, signing_key)
        assert new_payload.iat == original_iat
        assert new_payload.csrf == new_csrf

    def test_reissue_returns_none_past_max_lifetime(self) -> None:
        signing_key = derive_jwt_key("test")
        old_iat = time.time() - (5 * 3600)  # 5 hours ago
        payload = TokenPayload(
            sub=1, username="admin", role="admin", gen=0,
            csrf="abc", iat=old_iat, exp=time.time() + 1000,
        )
        assert reissue_token(payload, signing_key) is None


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


class TestTOTP:
    def test_generate_secret(self) -> None:
        secret = generate_totp_secret()
        assert len(secret) > 0

    def test_generate_uri(self) -> None:
        uri = generate_totp_uri("JBSWY3DPEHPK3PXP", "admin")
        assert "otpauth://totp/" in uri
        assert "OasisAgent" in uri

    def test_verify_valid_code(self) -> None:
        import pyotp

        secret = pyotp.random_base32()
        code = pyotp.TOTP(secret).now()
        assert verify_totp(secret, code)

    def test_verify_invalid_code(self) -> None:
        secret = generate_totp_secret()
        assert not verify_totp(secret, "000000")


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------


class TestBackupCodes:
    def test_generate_correct_count(self) -> None:
        codes = generate_backup_codes()
        assert len(codes) == 10

    def test_codes_are_unique(self) -> None:
        codes = generate_backup_codes()
        assert len(set(codes)) == len(codes)

    def test_hash_and_verify(self) -> None:
        codes = generate_backup_codes()
        hashed = hash_backup_codes(codes)
        # First code should verify
        idx = verify_backup_code(codes[0], hashed)
        assert idx == 0

    def test_verify_invalid_code(self) -> None:
        codes = generate_backup_codes()
        hashed = hash_backup_codes(codes)
        assert verify_backup_code("invalid-code-xxxx", hashed) is None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_under_threshold_no_lockout(self) -> None:
        assert calculate_lockout(3) is None

    def test_at_threshold_returns_lockout(self) -> None:
        lockout = calculate_lockout(_RATE_LIMIT_MAX_ATTEMPTS)
        assert lockout is not None

    def test_exponential_backoff(self) -> None:
        from datetime import datetime

        lock1 = calculate_lockout(_RATE_LIMIT_MAX_ATTEMPTS)
        lock2 = calculate_lockout(_RATE_LIMIT_MAX_ATTEMPTS + 2)
        assert lock1 is not None
        assert lock2 is not None
        t1 = datetime.fromisoformat(lock1)
        t2 = datetime.fromisoformat(lock2)
        assert t2 > t1

    def test_check_rate_limit_locked(self) -> None:
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()
        with pytest.raises(Exception, match="locked"):
            check_rate_limit(10, future)

    def test_check_rate_limit_expired_lock(self) -> None:
        from datetime import UTC, datetime, timedelta

        past = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
        # Should not raise
        check_rate_limit(10, past)

    def test_check_rate_limit_no_lock(self) -> None:
        check_rate_limit(3, None)  # Should not raise
