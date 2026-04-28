"""Shared issuer/group normalization helpers for concentration checks."""

from __future__ import annotations

import re
from typing import Mapping

_ISSUER_SUFFIXES = re.compile(
    r"\b(limited|ltd|pvt|private|corporation|corp|incorporated|inc|"
    r"company|co|llp|llc|plc|nv|sa|ag|group|holdings?)\b",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[.\-,&/()]+")
_WHITESPACE = re.compile(r"\s+")

_GOI_GROUP_KEYS = {
    "GOI_GROUP",
    "GOI",
    "GOVT_OF_INDIA",
    "GOVERNMENT_OF_INDIA",
    "GOVERNMENT",
}


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", str(value).strip()).upper()


def normalize_issuer_name(name: str | None) -> str:
    """
    Canonical issuer identity used for issuer/group concentration matching.

    This strips legal suffixes and punctuation so variants like
    "ABC Finance Limited" and "ABC Finance Ltd." resolve to one key.
    """

    cleaned = _clean_text(name)
    if not cleaned:
        return ""
    cleaned = _ISSUER_SUFFIXES.sub("", cleaned)
    cleaned = _PUNCTUATION.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned


def normalize_group_name(name: str | None) -> str:
    """Normalize group labels without stripping meaningful group tokens."""

    cleaned = _clean_text(name)
    return cleaned


def group_key(name: str | None) -> str:
    """Canonical key for group comparisons and special-case checks."""

    return normalize_group_name(name).replace(" ", "_")


def is_goi_group(name: str | None) -> bool:
    return group_key(name) in _GOI_GROUP_KEYS


def build_issuer_group_lookup(rows: list[dict]) -> dict[str, str]:
    """Build normalized issuer->group mapping from issuer_group_map rows."""

    lookup: dict[str, str] = {}
    for row in rows or []:
        issuer_key = normalize_issuer_name((row or {}).get("issuer_name"))
        if not issuer_key:
            continue
        group_label = normalize_group_name((row or {}).get("group_name"))
        lookup[issuer_key] = group_label or issuer_key
    return lookup


def resolve_group_name(issuer_name: str | None, issuer_to_group: Mapping[str, str]) -> str:
    """Resolve a normalized group label for an issuer with safe fallback."""

    issuer_key = normalize_issuer_name(issuer_name)
    if not issuer_key:
        return "UNKNOWN"
    return issuer_to_group.get(issuer_key, issuer_key)
