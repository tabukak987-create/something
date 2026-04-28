"""UTC time helpers used across gateway runtime code."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current aware UTC datetime."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Return the current aware UTC time as an ISO-8601 string."""
    return utcnow().isoformat()