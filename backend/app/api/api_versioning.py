"""
API Versioning Strategy — L4 Remediation
=========================================
Provides structured API versioning for the Unified Gateway.

Strategy: URI-path versioning (/api/v1, /api/v2) with header-based override.

Usage in main.py:
    from .routers.api_versioning import api_v2_router, VersionNegotiationMiddleware
    app.include_router(api_v2_router, prefix="/api/v2")
    app.add_middleware(VersionNegotiationMiddleware)

The v2 API provides:
    - All v1 endpoints (backward compatible)
    - Enhanced response envelopes with metadata
    - Deprecation headers on v1 responses
    - API version discovery endpoint
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Response, Depends
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..shared.auth import require_role, Roles
from ..shared.logger import get_logger
from ..shared.time_utils import utcnow_iso

log = get_logger("api.versioning")

# ── Version Registry ──────────────────────────────────────────────────────
API_VERSIONS = {
    "v1": {
        "version": "1.0.0",
        "status": "stable",
        "released": "2025-06-01",
        "sunset_date": "2027-06-01",
        "description": "Original API — full bond intelligence, compliance, portfolio, governance",
    },
    "v2": {
        "version": "2.0.0",
        "status": "current",
        "released": "2026-02-01",
        "sunset_date": None,
        "description": "Enhanced API — structured envelopes, pagination, signing metadata",
        "breaking_changes": [
            "All responses wrapped in {data, meta, errors} envelope",
            "Pagination uses cursor-based tokens instead of offset",
            "Timestamps use ISO 8601 with timezone (always UTC)",
            "Error responses use RFC 7807 Problem Details format",
        ],
    },
}

CURRENT_VERSION = "v2"
MINIMUM_SUPPORTED = "v1"


# ── Response Envelope ─────────────────────────────────────────────────────
def envelope(
    data: Any,
    meta: Optional[Dict] = None,
    errors: Optional[list] = None,
    status: str = "success",
) -> Dict[str, Any]:
    """Wrap response in v2 standard envelope."""
    return {
        "status": status,
        "data": data,
        "meta": {
            "api_version": "2.0.0",
            "timestamp": utcnow_iso(),
            "request_id": None,  # Set by middleware
            **(meta or {}),
        },
        "errors": errors or [],
    }


# ── Version Negotiation Middleware ────────────────────────────────────────
class VersionNegotiationMiddleware(BaseHTTPMiddleware):
    """
    Adds API versioning headers to all responses:
      - X-API-Version: Current version served
      - X-API-Deprecated: true (if v1)
      - X-API-Sunset: Date when version will be retired
      - X-Request-Id: Unique request identifier
    """

    async def dispatch(self, request: Request, call_next):
        import uuid

        request_id = str(uuid.uuid4())
        start_time = time.time()

        # Determine API version from path or header
        path = request.url.path
        accept_version = request.headers.get("X-API-Version", "").lower()

        if "/api/v2" in path:
            version = "v2"
        elif "/api/v1" in path:
            version = "v1"
        elif accept_version in API_VERSIONS:
            version = accept_version
        else:
            version = "v1"  # Default

        # Store on request state
        request.state.api_version = version
        request.state.request_id = request_id

        response: Response = await call_next(request)

        # Add version headers
        elapsed_ms = round((time.time() - start_time) * 1000, 2)
        response.headers["X-API-Version"] = API_VERSIONS[version]["version"]
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)

        if version == "v1":
            response.headers["X-API-Deprecated"] = "true"
            response.headers["X-API-Sunset"] = API_VERSIONS["v1"]["sunset_date"]
            response.headers["Link"] = '</api/v2>; rel="successor-version"'

        return response


# ── V2 Router ─────────────────────────────────────────────────────────────
api_v2_router = APIRouter(tags=["API v2"])


@api_v2_router.get("/version")
async def api_version_info():
    """Get API version information and migration guide."""
    return envelope(
        data={
            "versions": API_VERSIONS,
            "current": CURRENT_VERSION,
            "minimum_supported": MINIMUM_SUPPORTED,
            "migration_guide": {
                "v1_to_v2": {
                    "response_format": "All responses wrapped in {status, data, meta, errors}",
                    "pagination": "Use 'cursor' parameter instead of 'offset'",
                    "timestamps": "All timestamps are ISO 8601 UTC",
                    "errors": "Error responses follow RFC 7807",
                    "authentication": "Same JWT mechanism; MFA support added",
                    "deprecation_timeline": "v1 supported until 2027-06-01",
                },
            },
        },
        meta={"endpoint": "/api/v2/version"},
    )


@api_v2_router.get("/health")
async def v2_health():
    """V2 health check with enhanced metadata."""
    from ..shared.health import get_health_summary

    health = await get_health_summary()
    return envelope(
        data=health,
        meta={"endpoint": "/api/v2/health"},
    )


@api_v2_router.get("/capabilities")
async def api_capabilities(user: dict = Depends(require_role(Roles.VIEWER))):
    """List all available API capabilities for the authenticated user."""
    return envelope(
        data={
            "services": [
                {"name": "intelligence", "prefix": "/api/v2/intelligence", "status": "active"},
                {"name": "compliance", "prefix": "/api/v2/compliance", "status": "active"},
                {"name": "portfolio", "prefix": "/api/v2/portfolio", "status": "active"},
                {"name": "quotes", "prefix": "/api/v2/quotes", "status": "active"},
                {"name": "reports", "prefix": "/api/v2/reports", "status": "active"},
                {"name": "governance", "prefix": "/api/v2/governance", "status": "active"},
                {"name": "risk", "prefix": "/api/v2/risk", "status": "active"},
                {"name": "market-data", "prefix": "/api/v2/market-data", "status": "active"},
                {"name": "alerts", "prefix": "/api/v2/alerts", "status": "active"},
            ],
            "authentication": {
                "method": "Bearer JWT",
                "mfa": "TOTP (optional)",
                "token_lifetime_minutes": 480,
            },
            "rate_limits": {
                "api": "30 requests/minute",
                "auth": "5 requests/minute",
            },
        },
        meta={"user": user.get("username", "unknown")},
    )
