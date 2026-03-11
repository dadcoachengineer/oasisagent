"""Tests for Fernet crypto provider and secret key resolution."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet, InvalidToken

from oasisagent.db.crypto import _SECRET_KEY_MODE, CryptoProvider, resolve_secret_key


class TestCryptoProvider:
    def test_encrypt_decrypt_round_trip(self, crypto: CryptoProvider) -> None:
        data = {"password": "s3cret", "token": "abc123"}
        cipher = crypto.encrypt(data)
        assert cipher != ""
        assert "s3cret" not in cipher
        assert crypto.decrypt(cipher) == data

    def test_empty_dict_returns_empty_string(self, crypto: CryptoProvider) -> None:
        assert crypto.encrypt({}) == ""

    def test_decrypt_empty_string_returns_empty_dict(self, crypto: CryptoProvider) -> None:
        assert crypto.decrypt("") == {}

    def test_wrong_key_raises_invalid_token(self, crypto: CryptoProvider) -> None:
        cipher = crypto.encrypt({"key": "value"})
        other_crypto = CryptoProvider(Fernet.generate_key())
        with pytest.raises(InvalidToken):
            other_crypto.decrypt(cipher)

    def test_various_types_in_dict(self, crypto: CryptoProvider) -> None:
        data = {"str": "hello", "int": 42, "bool": True, "none": None, "list": [1, 2]}
        assert crypto.decrypt(crypto.encrypt(data)) == data

    def test_unicode_values(self, crypto: CryptoProvider) -> None:
        data = {"name": "日本語テスト", "emoji": "🔐"}
        assert crypto.decrypt(crypto.encrypt(data)) == data


class TestResolveSecretKey:
    def test_reads_from_env_var(self, tmp_path: Path) -> None:
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"OASIS_SECRET_KEY": key}):
            assert resolve_secret_key(tmp_path) == key

    def test_reads_from_file(self, tmp_path: Path) -> None:
        key = Fernet.generate_key().decode()
        key_path = tmp_path / ".secret_key"
        key_path.write_text(key + "\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OASIS_SECRET_KEY", None)
            assert resolve_secret_key(tmp_path) == key

    def test_auto_generates_when_missing(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OASIS_SECRET_KEY", None)
            key = resolve_secret_key(tmp_path)

        # Key is valid Fernet key
        Fernet(key.encode())

        # Key was persisted to file
        key_path = tmp_path / ".secret_key"
        assert key_path.exists()
        assert key_path.read_text().strip() == key

    def test_auto_generated_file_has_0600_permissions(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OASIS_SECRET_KEY", None)
            resolve_secret_key(tmp_path)

        key_path = tmp_path / ".secret_key"
        mode = key_path.stat().st_mode & 0o777
        assert mode == _SECRET_KEY_MODE

    def test_warns_on_loose_permissions(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        key = Fernet.generate_key().decode()
        key_path = tmp_path / ".secret_key"
        key_path.write_text(key)
        key_path.chmod(0o644)  # too permissive

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OASIS_SECRET_KEY", None)
            resolve_secret_key(tmp_path)

        assert "permissions" in caplog.text.lower()
        assert "0644" in caplog.text

    def test_creates_data_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "dir"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OASIS_SECRET_KEY", None)
            resolve_secret_key(nested)
        assert (nested / ".secret_key").exists()

    def test_env_var_takes_precedence_over_file(self, tmp_path: Path) -> None:
        file_key = Fernet.generate_key().decode()
        env_key = Fernet.generate_key().decode()
        (tmp_path / ".secret_key").write_text(file_key)
        with patch.dict(os.environ, {"OASIS_SECRET_KEY": env_key}):
            assert resolve_secret_key(tmp_path) == env_key
