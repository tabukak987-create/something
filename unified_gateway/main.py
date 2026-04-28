"""Compatibility wrapper for the cleaned gateway application.

This keeps the legacy launch command working:
    uvicorn unified_gateway.main:app --port 9000

The actual implementation lives in backend.main.
"""

from __future__ import annotations

from backend.main import app

__all__ = ["app"]
