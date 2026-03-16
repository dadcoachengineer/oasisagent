"""Preventive scanning framework for proactive failure detection.

Each scanner is its own IngestAdapter instance — the orchestrator manages
their lifecycle alongside other adapters. The ScannerIngestAdapter base
provides the polling loop and enqueue helper.
"""

from oasisagent.scanner.backup_freshness import BackupFreshnessScannerAdapter
from oasisagent.scanner.base import ScannerIngestAdapter
from oasisagent.scanner.cert_expiry import CertExpiryScannerAdapter
from oasisagent.scanner.disk_space import DiskSpaceScannerAdapter
from oasisagent.scanner.docker_health import DockerHealthScannerAdapter
from oasisagent.scanner.ha_health import HaHealthScannerAdapter

__all__ = [
    "BackupFreshnessScannerAdapter",
    "CertExpiryScannerAdapter",
    "DiskSpaceScannerAdapter",
    "DockerHealthScannerAdapter",
    "HaHealthScannerAdapter",
    "ScannerIngestAdapter",
]
