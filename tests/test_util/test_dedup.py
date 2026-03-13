"""Tests for cross-source deduplication helpers."""

from __future__ import annotations

from oasisagent.util.dedup import CERT_DEDUP_PREFIX, normalize_cert_dedup_key


class TestNormalizeCertDedupKey:
    def test_plain_hostname(self) -> None:
        assert normalize_cert_dedup_key("example.com") == "cert_expiry:example.com"

    def test_uppercase_lowered(self) -> None:
        assert normalize_cert_dedup_key("Example.COM") == "cert_expiry:example.com"

    def test_port_443_stripped(self) -> None:
        assert normalize_cert_dedup_key("example.com:443") == "cert_expiry:example.com"

    def test_non_443_port_preserved(self) -> None:
        result = normalize_cert_dedup_key("nas.example.com:8443")
        assert result == "cert_expiry:nas.example.com:8443"

    def test_https_prefix_stripped(self) -> None:
        assert normalize_cert_dedup_key("https://example.com") == "cert_expiry:example.com"

    def test_https_with_path_stripped(self) -> None:
        assert normalize_cert_dedup_key("https://example.com/path") == "cert_expiry:example.com"

    def test_https_with_non_443_port(self) -> None:
        result = normalize_cert_dedup_key("https://example.com:8443")
        assert result == "cert_expiry:example.com:8443"

    def test_trailing_dot_stripped(self) -> None:
        assert normalize_cert_dedup_key("example.com.") == "cert_expiry:example.com"

    def test_trailing_slash_stripped(self) -> None:
        assert normalize_cert_dedup_key("example.com/") == "cert_expiry:example.com"

    def test_whitespace_stripped(self) -> None:
        assert normalize_cert_dedup_key("  example.com  ") == "cert_expiry:example.com"

    def test_prefix_constant(self) -> None:
        assert CERT_DEDUP_PREFIX == "cert_expiry:"

    def test_scanner_and_uk_produce_same_key(self) -> None:
        """Both sources should produce the same key for the same domain."""
        # Cert scanner passes "example.com" or "example.com:443"
        scanner_key = normalize_cert_dedup_key("example.com:443")
        # UK adapter extracts from URL "https://example.com"
        uk_key = normalize_cert_dedup_key("https://example.com")
        assert scanner_key == uk_key
