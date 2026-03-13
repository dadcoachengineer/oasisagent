"""Cross-source deduplication helpers.

Provides normalized dedup key generators so that events from different
ingestion sources (e.g., cert scanner and Uptime Kuma) that describe
the same underlying issue share the same dedup key.
"""

from __future__ import annotations

from urllib.parse import urlparse

CERT_DEDUP_PREFIX = "cert_expiry:"


def normalize_cert_dedup_key(host: str) -> str:
    """Build a canonical dedup key for certificate-related events.

    Normalizes the host by lowercasing, stripping protocol prefixes,
    port suffixes (443 is implied), and trailing dots/slashes.

    Examples:
        >>> normalize_cert_dedup_key("Example.COM:443")
        'cert_expiry:example.com'
        >>> normalize_cert_dedup_key("https://Example.COM/path")
        'cert_expiry:example.com'
        >>> normalize_cert_dedup_key("nas.example.com:8443")
        'cert_expiry:nas.example.com:8443'
    """
    h = host.strip()

    # Strip protocol prefix if present
    if "://" in h:
        parsed = urlparse(h)
        h = parsed.hostname or parsed.netloc
        port = parsed.port
        if port and port != 443:
            h = f"{h}:{port}"
    else:
        # Remove trailing slashes/dots
        h = h.rstrip("/.")

        # Strip port 443 (implied for TLS)
        if h.endswith(":443"):
            h = h[:-4]

    return f"{CERT_DEDUP_PREFIX}{h.lower()}"
