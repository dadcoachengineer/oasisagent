"""Tests for Docker secret file loading (_load_file_secrets)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

from oasisagent.__main__ import _load_file_secrets


class TestLoadFileSecrets:
    """Tests for _load_file_secrets()."""

    def test_reads_file_and_sets_env_var(self, tmp_path: Path):
        """_FILE env var loads file contents into the base env var."""
        secret_file = tmp_path / "my_secret"
        secret_file.write_text("s3cret_value\n")

        env = {"MY_VAR_FILE": str(secret_file)}
        with patch.dict(os.environ, env, clear=True):
            _load_file_secrets()
            assert os.environ["MY_VAR"] == "s3cret_value"

    def test_strips_whitespace(self, tmp_path: Path):
        """File contents are stripped (trailing newline, spaces)."""
        secret_file = tmp_path / "token"
        secret_file.write_text("  token123  \n\n")

        env = {"HA_TOKEN_FILE": str(secret_file)}
        with patch.dict(os.environ, env, clear=True):
            _load_file_secrets()
            assert os.environ["HA_TOKEN"] == "token123"

    def test_missing_file_skipped(self):
        """Non-existent file is silently skipped."""
        env = {"SOME_KEY_FILE": "/nonexistent/path/secret"}
        with patch.dict(os.environ, env, clear=True):
            _load_file_secrets()
            assert "SOME_KEY" not in os.environ

    def test_does_not_affect_non_file_vars(self, tmp_path: Path):
        """Env vars not ending in _FILE are left alone."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("value")

        env = {"NORMAL_VAR": "keep_me", "OTHER_FILE": str(secret_file)}
        with patch.dict(os.environ, env, clear=True):
            _load_file_secrets()
            assert os.environ["NORMAL_VAR"] == "keep_me"
            assert os.environ["OTHER"] == "value"

    def test_multiple_file_vars(self, tmp_path: Path):
        """Multiple *_FILE vars are all processed."""
        f1 = tmp_path / "a"
        f1.write_text("val_a")
        f2 = tmp_path / "b"
        f2.write_text("val_b")

        env = {"A_FILE": str(f1), "B_FILE": str(f2)}
        with patch.dict(os.environ, env, clear=True):
            _load_file_secrets()
            assert os.environ["A"] == "val_a"
            assert os.environ["B"] == "val_b"

    def test_overwrites_existing_env_var(self, tmp_path: Path):
        """File value overwrites any pre-existing env var."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("from_file")

        env = {"MY_KEY": "original", "MY_KEY_FILE": str(secret_file)}
        with patch.dict(os.environ, env, clear=True):
            _load_file_secrets()
            assert os.environ["MY_KEY"] == "from_file"

    def test_empty_file_sets_empty_string(self, tmp_path: Path):
        """An empty secret file results in an empty string."""
        secret_file = tmp_path / "empty"
        secret_file.write_text("")

        env = {"TOKEN_FILE": str(secret_file)}
        with patch.dict(os.environ, env, clear=True):
            _load_file_secrets()
            assert os.environ["TOKEN"] == ""
