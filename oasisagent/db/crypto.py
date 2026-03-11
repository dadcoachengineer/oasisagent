"""Fernet encryption for secrets stored in SQLite.

Secrets (tokens, passwords, API keys) are encrypted at rest in the database
and decrypted in-memory only when adapters/handlers need them.

The encryption key is sourced from (in order):
1. ``OASIS_SECRET_KEY`` environment variable
2. ``{OASIS_DATA_DIR}/.secret_key`` file
3. Auto-generated on first run and persisted to the file above
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path  # noqa: TC003 — used at runtime in resolve_secret_key()
from typing import Any

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Permissions: owner read/write only
_SECRET_KEY_MODE = 0o600


class CryptoProvider:
    """Encrypt and decrypt secret dictionaries using Fernet symmetric encryption."""

    def __init__(self, secret_key: str | bytes) -> None:
        key = secret_key.encode() if isinstance(secret_key, str) else secret_key
        self._fernet = Fernet(key)

    def encrypt(self, data: dict[str, Any]) -> str:
        """Serialize *data* to JSON and return a Fernet-encrypted string.

        Returns an empty string if *data* is empty (no encryption needed).
        """
        if not data:
            return ""
        payload = json.dumps(data, sort_keys=True).encode()
        return self._fernet.encrypt(payload).decode()

    def decrypt(self, cipher_text: str) -> dict[str, Any]:
        """Decrypt a Fernet-encrypted string and return the original dict.

        Returns an empty dict if *cipher_text* is empty.

        Raises:
            cryptography.fernet.InvalidToken: If the key is wrong or data is
                corrupted.
        """
        if not cipher_text:
            return {}
        plaintext = self._fernet.decrypt(cipher_text.encode())
        result: dict[str, Any] = json.loads(plaintext)
        return result


def _check_key_file_permissions(path: Path) -> None:
    """Warn if the secret key file has permissions looser than 0600."""
    try:
        mode = path.stat().st_mode & 0o777
        if mode != _SECRET_KEY_MODE:
            logger.warning(
                "Secret key file %s has permissions %04o (expected %04o). "
                "Run: chmod 600 %s",
                path,
                mode,
                _SECRET_KEY_MODE,
                path,
            )
    except OSError:
        pass


def resolve_secret_key(data_dir: Path) -> str:
    """Resolve the Fernet secret key from env var, file, or auto-generation.

    Order of precedence:
    1. ``OASIS_SECRET_KEY`` environment variable
    2. ``{data_dir}/.secret_key`` file on disk
    3. Generate a new key, persist to file with 0600 permissions

    Returns:
        The Fernet-compatible secret key string.
    """
    env_key = os.environ.get("OASIS_SECRET_KEY")
    if env_key:
        logger.debug("Using OASIS_SECRET_KEY from environment")
        return env_key

    key_path = data_dir / ".secret_key"

    if key_path.exists():
        _check_key_file_permissions(key_path)
        key = key_path.read_text().strip()
        logger.debug("Loaded secret key from %s", key_path)
        return key

    # Auto-generate on first run
    key = Fernet.generate_key().decode()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Write with restricted permissions: create file with 0600 via opener
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _SECRET_KEY_MODE)
    try:
        os.write(fd, key.encode())
        os.write(fd, b"\n")
    finally:
        os.close(fd)

    # Belt-and-suspenders: ensure perms are correct even if umask interfered
    key_path.chmod(_SECRET_KEY_MODE)

    logger.info(
        "Generated new secret key at %s — back up this file, "
        "losing it requires re-entering all secrets",
        key_path,
    )
    return key
