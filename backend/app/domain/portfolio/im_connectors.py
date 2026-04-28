"""
IM Source Connector — Abstract Layer
======================================
Provides an abstract interface for IM document ingestion from multiple
sources. Current implementations:
  - ManualUploadConnector (active)
  - FutureNSEConnector    (stub — awaiting NSE API access)
  - SEBIConnector         (wrapper around existing scraper)
  - BSEConnector          (wrapper around existing scraper)

Environment-based activation:
  NSE_API_BASE_URL, NSE_API_KEY, NSE_CLIENT_ID → enables FutureNSEConnector
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..shared.time_utils import utcnow_iso

logger = logging.getLogger("im_connectors")

# ─── Storage paths ────────────────────────────────────────────────────────────

_BASE_DIR = Path(__file__).parent.parent
IM_STORAGE_DIR = _BASE_DIR / "data" / "im_storage"
IM_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Max file size: 50 MB
MAX_FILE_SIZE = 50 * 1024 * 1024

# ─── Config from environment ──────────────────────────────────────────────────

NSE_CONFIG = {
    "api_base_url": os.getenv("NSE_API_BASE_URL", ""),
    "api_key": os.getenv("NSE_API_KEY", ""),
    "client_id": os.getenv("NSE_CLIENT_ID", ""),
    "enabled": bool(os.getenv("NSE_API_BASE_URL", "")),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Abstract Connector Base
# ═══════════════════════════════════════════════════════════════════════════════

class IMSourceConnector(ABC):
    """Abstract base class for IM document sources."""

    source_name: str = "UNKNOWN"

    @abstractmethod
    async def validate_document(self, file_bytes: bytes, filename: str) -> Dict[str, Any]:
        """Validate an incoming document. Returns {"valid": bool, "error": str|None, "metadata": dict}."""
        ...

    @abstractmethod
    async def store_document(
        self,
        isin: str,
        file_bytes: bytes,
        filename: str,
        version: int = 1,
    ) -> Dict[str, Any]:
        """Store document to disk. Returns {"file_path": str, "checksum": str}."""
        ...

    @abstractmethod
    async def fetch_documents(self, **kwargs) -> List[Dict[str, Any]]:
        """Fetch document listings from the source. Returns list of document metadata dicts."""
        ...

    @staticmethod
    def compute_checksum(data: bytes) -> str:
        """SHA-256 checksum of file bytes."""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def is_pdf(data: bytes) -> bool:
        """Check PDF magic bytes."""
        return data[:5] == b'%PDF-'


# ═══════════════════════════════════════════════════════════════════════════════
# Manual Upload Connector (Active)
# ═══════════════════════════════════════════════════════════════════════════════

class ManualUploadConnector(IMSourceConnector):
    """Handles manual PDF uploads from the UI."""

    source_name = "MANUAL"

    async def validate_document(self, file_bytes: bytes, filename: str) -> Dict[str, Any]:
        errors = []

        if not filename.lower().endswith(".pdf"):
            errors.append("File must be a PDF")

        if not self.is_pdf(file_bytes):
            errors.append("File does not have valid PDF header")

        if len(file_bytes) > MAX_FILE_SIZE:
            errors.append(f"File too large ({len(file_bytes) / 1024 / 1024:.1f} MB). Max is {MAX_FILE_SIZE / 1024 / 1024:.0f} MB")

        if len(file_bytes) < 100:
            errors.append("File too small — likely corrupted")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "metadata": {
                "filename": filename,
                "size_bytes": len(file_bytes),
                "checksum": self.compute_checksum(file_bytes),
            },
        }

    async def store_document(
        self,
        isin: str,
        file_bytes: bytes,
        filename: str,
        version: int = 1,
    ) -> Dict[str, Any]:
        isin_dir = IM_STORAGE_DIR / isin.upper()
        isin_dir.mkdir(parents=True, exist_ok=True)

        ts = utcnow_iso().replace("-", "").replace(":", "")[:15]
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename)
        stored_name = f"v{version}_{ts}_{safe_name}"
        file_path = isin_dir / stored_name

        file_path.write_bytes(file_bytes)

        return {
            "file_path": str(file_path.relative_to(IM_STORAGE_DIR)),
            "checksum": self.compute_checksum(file_bytes),
            "stored_name": stored_name,
            "size_bytes": len(file_bytes),
        }

    async def fetch_documents(self, **kwargs) -> List[Dict[str, Any]]:
        """Manual connector doesn't fetch — returns empty list."""
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Future NSE Connector (Stub)
# ═══════════════════════════════════════════════════════════════════════════════

class FutureNSEConnector(IMSourceConnector):
    """
    Stub connector for future NSE API access.
    Currently NSE blocks all HTTP clients via Akamai Bot Manager.
    This stub will be activated when NSE_API_BASE_URL env var is set.

    Expected NSE API endpoints (when available):
      GET  /api/corporateFilings-offer-documents?category=debt
      GET  /api/document-download/<doc_id>

    Required env vars:
      NSE_API_BASE_URL  — e.g. https://www.nseindia.com/api
      NSE_API_KEY       — API key (if any)
      NSE_CLIENT_ID     — Client ID for authentication
    """

    source_name = "NSE_FUTURE"

    def __init__(self):
        self.config = NSE_CONFIG
        self.enabled = self.config["enabled"]

    async def validate_document(self, file_bytes: bytes, filename: str) -> Dict[str, Any]:
        if not self.enabled:
            return {"valid": False, "errors": ["NSE connector not configured"], "metadata": {}}
        return await ManualUploadConnector().validate_document(file_bytes, filename)

    async def store_document(
        self,
        isin: str,
        file_bytes: bytes,
        filename: str,
        version: int = 1,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise NotImplementedError("NSE connector not configured. Set NSE_API_BASE_URL env var.")
        return await ManualUploadConnector().store_document(isin, file_bytes, filename, version)

    async def fetch_documents(self, **kwargs) -> List[Dict[str, Any]]:
        """
        STUB: Will fetch IM document listings from NSE API when access is available.

        Expected response format:
        [
            {
                "isin": "INE...",
                "issuer_name": "...",
                "doc_title": "...",
                "doc_type": "IM",
                "filing_date": "2025-01-15",
                "pdf_url": "https://..."
            }
        ]
        """
        if not self.enabled:
            logger.info("NSE connector not enabled — returning empty results")
            return []

        # TODO: Implement when NSE API access is available
        # 1. Establish session with Akamai cookies
        # 2. GET /api/corporateFilings-offer-documents?category=debt
        # 3. Parse JSON response
        # 4. Return list of document metadata dicts
        logger.warning("NSE API connector not yet implemented — stub returns empty results")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Factory / Registry
# ═══════════════════════════════════════════════════════════════════════════════

_CONNECTOR_REGISTRY: Dict[str, type] = {
    "MANUAL": ManualUploadConnector,
    "NSE_FUTURE": FutureNSEConnector,
}


def get_connector(source: str = "MANUAL") -> IMSourceConnector:
    """Get the appropriate connector for a given source."""
    cls = _CONNECTOR_REGISTRY.get(source.upper(), ManualUploadConnector)
    return cls()


def list_connectors() -> List[Dict[str, Any]]:
    """Return info about all registered connectors."""
    result = []
    for name, cls in _CONNECTOR_REGISTRY.items():
        inst = cls()
        result.append({
            "source": name,
            "description": (cls.__doc__ or "").strip().split("\n")[0],
            "active": name == "MANUAL" or (name == "NSE_FUTURE" and getattr(inst, "enabled", False)),
        })
    return result
