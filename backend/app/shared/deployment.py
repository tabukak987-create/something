"""
Deployment Mode Helpers
=======================
Centralized runtime policy switches for cloud demo vs on-prem trust deployment.
"""

from __future__ import annotations

import os


_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def deployment_mode() -> str:
    """
    Return deployment mode.

    Supported modes:
      - cloud  : demo / hosted mode
      - onprem : trust-controlled on-prem mode
    """
    mode = str(os.getenv("DEPLOYMENT_MODE", "")).strip().lower()
    if mode in {"onprem", "on-prem", "on_prem"}:
        return "onprem"
    if mode == "cloud":
        return "cloud"

    if _is_truthy(os.getenv("ON_PREM_MODE", "false")):
        return "onprem"
    return "cloud"


def is_on_prem_mode() -> bool:
    return deployment_mode() == "onprem"


def external_data_egress_enabled() -> bool:
    """
    Whether outbound data sync/fetch calls are allowed.

    Default:
      - cloud  : enabled
      - onprem : disabled
    """
    default = "false" if is_on_prem_mode() else "true"
    return _is_truthy(os.getenv("ALLOW_EXTERNAL_DATA_EGRESS", default))


def strict_portfolio_versioning_enabled() -> bool:
    """
    If enabled, destructive replace-mode uploads are blocked.

    Default:
      - cloud  : disabled
      - onprem : enabled
    """
    default = "true" if is_on_prem_mode() else "false"
    return _is_truthy(os.getenv("STRICT_PORTFOLIO_VERSIONING", default))


def strict_audit_logging_enabled() -> bool:
    """
    If enabled, canonical trust audit write failures are fatal.

    Default:
      - cloud  : disabled
      - onprem : enabled
    """
    default = "true" if is_on_prem_mode() else "false"
    return _is_truthy(os.getenv("STRICT_AUDIT_LOGGING", default))


def mfa_required_for_all_roles() -> bool:
    """
    If enabled, MFA enrollment is mandatory for every role.

    Default:
      - cloud  : disabled
      - onprem : enabled
    """
    default = "true" if is_on_prem_mode() else "false"
    return _is_truthy(os.getenv("MFA_REQUIRED_FOR_ALL_ROLES", default))


def assert_external_data_egress(operation: str) -> None:
    """Raise PermissionError if current deployment blocks outbound data sync."""
    if not external_data_egress_enabled():
        raise PermissionError(
            f"Operation '{operation}' is disabled in on-prem mode because outbound data egress is blocked. "
            "Set ALLOW_EXTERNAL_DATA_EGRESS=true only after trust approval."
        )
