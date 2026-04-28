"""Relative value engine for peer spread comparison."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def compute_relative_value(
    bonds: list[dict[str, Any]],
    spread_threshold_bps: float = 10.0,
) -> list[dict[str, Any]]:
    peers: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for b in bonds:
        y = float(b.get("ytm") or b.get("yield") or 0.0)
        if y <= 0:
            continue
        peers[_group_key(b)].append(y)

    results: list[dict[str, Any]] = []
    for b in bonds:
        key = _group_key(b)
        y = float(b.get("ytm") or b.get("yield") or 0.0)
        group = peers.get(key, [])
        if not group or y <= 0:
            continue
        peer_avg = sum(group) / len(group)
        relative_value = y - peer_avg
        advantage_bps = relative_value * 100.0
        out = dict(b)
        out["peer_avg_yield"] = round(peer_avg, 6)
        out["relative_value"] = round(relative_value, 6)
        out["relative_value_bps"] = round(advantage_bps, 2)
        out["undervalued_bond"] = advantage_bps > spread_threshold_bps
        results.append(out)
    return results


def _group_key(bond: dict[str, Any]) -> tuple[str, str, str, str]:
    rating = str((bond.get("credit_rating") or bond.get("rating") or "NR")).upper()
    sector = str((bond.get("sector") or "OTHER")).upper()
    issuer_type = str((bond.get("issuer_type") or "CORPORATE")).upper()
    dur = float(bond.get("modified_duration") or bond.get("duration") or 0.0)
    if dur < 3:
        bucket = "SHORT"
    elif dur < 7:
        bucket = "MEDIUM"
    else:
        bucket = "LONG"
    return rating, sector, bucket, issuer_type
