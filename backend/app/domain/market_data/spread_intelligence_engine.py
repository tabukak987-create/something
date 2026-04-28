"""Spread intelligence engine for peer mispricing detection and score adjustments."""

from __future__ import annotations

from collections import defaultdict, deque
from math import sqrt
from typing import Any


_SPREAD_HISTORY: dict[tuple[str, str, str, str], deque[float]] = defaultdict(lambda: deque(maxlen=500))


def reset_spread_history() -> None:
    """Testing helper to clear in-memory spread baseline history."""
    _SPREAD_HISTORY.clear()


def _duration_bucket(duration: float) -> str:
    # 1Y granularity for the +/-1 year peer matching requirement.
    return f"Y{int(round(duration))}"


def _group_key(bond: dict[str, Any], duration_bucket: str | None = None) -> tuple[str, str, str, str]:
    rating = str((bond.get("credit_rating") or bond.get("rating") or "NR")).upper()
    sector = str((bond.get("sector") or "OTHER")).upper()
    issuer_type = str((bond.get("issuer_type") or "CORPORATE")).upper()
    duration = float(bond.get("modified_duration") or bond.get("duration") or 0.0)
    return rating, sector, duration_bucket or _duration_bucket(duration), issuer_type


def _yield_value(bond: dict[str, Any]) -> float:
    return float(bond.get("ytm") or bond.get("yield") or 0.0)


def _peer_yields(
    bonds: list[dict[str, Any]],
    idx: int,
) -> list[float]:
    base = bonds[idx]
    base_rating = str((base.get("credit_rating") or base.get("rating") or "NR")).upper()
    base_sector = str((base.get("sector") or "OTHER")).upper()
    base_issuer_type = str((base.get("issuer_type") or "CORPORATE")).upper()
    base_duration = float(base.get("modified_duration") or base.get("duration") or 0.0)

    peers: list[float] = []
    for j, peer in enumerate(bonds):
        if j == idx:
            continue
        y = _yield_value(peer)
        if y <= 0:
            continue
        if str((peer.get("credit_rating") or peer.get("rating") or "NR")).upper() != base_rating:
            continue
        if str((peer.get("sector") or "OTHER")).upper() != base_sector:
            continue
        if str((peer.get("issuer_type") or "CORPORATE")).upper() != base_issuer_type:
            continue
        peer_duration = float(peer.get("modified_duration") or peer.get("duration") or 0.0)
        if abs(peer_duration - base_duration) <= 1.0:
            peers.append(y)

    # Fallback to same broad group if strict matching leaves no peers.
    if peers:
        return peers

    for j, peer in enumerate(bonds):
        if j == idx:
            continue
        y = _yield_value(peer)
        if y <= 0:
            continue
        if str((peer.get("credit_rating") or peer.get("rating") or "NR")).upper() != base_rating:
            continue
        if str((peer.get("issuer_type") or "CORPORATE")).upper() != base_issuer_type:
            continue
        peers.append(y)
    return peers


def _mean(values: list[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def _std(values: list[float], mean: float) -> float:
    if not values:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return sqrt(max(var, 0.0))


def analyze_spread_intelligence(
    bonds: list[dict[str, Any]],
    spread_boost_factor: float = 0.03,
    anomaly_sigma: float = 1.5,
) -> dict[str, Any]:
    """
    Build peer-spread signals and identify undervaluation opportunities.

    Output fields are additive and backward-compatible with existing recommendation flow.
    """
    if not bonds:
        return {
            "bonds": [],
            "spread_signal": "NO_DATA",
            "opportunity_flag": False,
            "confidence_score": 0.0,
            "summary": {
                "total_bonds": 0,
                "opportunity_count": 0,
                "history_points_used": 0,
            },
        }

    enriched: list[dict[str, Any]] = []
    opportunity_count = 0
    confidence_sum = 0.0
    history_points_used = 0

    # Pre-compute peer averages and per-bond spreads.
    spreads_by_group: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    precomputed: list[tuple[list[float], float, tuple[str, str, str, str]]] = []
    for i, b in enumerate(bonds):
        y = _yield_value(b)
        if y <= 0:
            precomputed.append(([], 0.0, _group_key(b)))
            continue
        peers = _peer_yields(bonds, i)
        peer_avg = _mean(peers) if peers else y
        spread = y - peer_avg
        key = _group_key(b)
        precomputed.append((peers, spread, key))
        spreads_by_group[key].append(spread)

    for i, b in enumerate(bonds):
        peers, spread, key = precomputed[i]
        y = _yield_value(b)
        history = list(_SPREAD_HISTORY.get(key, []))
        hist_mean = _mean(history)
        hist_std = _std(history, hist_mean)

        # If history is thin, blend with current cohort spread stats to avoid unstable cold-start behavior.
        cohort = spreads_by_group.get(key, [])
        if len(history) < 5 and cohort:
            cohort_mean = _mean(cohort)
            cohort_std = _std(cohort, cohort_mean)
            weight_hist = min(len(history) / 5.0, 1.0)
            hist_mean = weight_hist * hist_mean + (1.0 - weight_hist) * cohort_mean
            hist_std = max(hist_std, cohort_std)

        threshold = hist_mean + anomaly_sigma * (hist_std if hist_std > 1e-9 else 0.0001)
        opportunity = bool(y > 0 and spread > threshold)

        z = (spread - hist_mean) / (hist_std if hist_std > 1e-9 else 0.0001)
        confidence = min(max((z - anomaly_sigma + 1.0) / 3.0, 0.0), 1.0)
        boost = spread_boost_factor * confidence if opportunity else 0.0

        out = dict(b)
        out["peer_avg_yield"] = _mean(peers) if peers else y
        out["spread"] = spread
        out["historical_avg_spread"] = hist_mean
        out["historical_std_dev"] = hist_std
        out["spread_zscore"] = z
        out["opportunity_flag"] = opportunity
        out["undervalue_opportunity"] = opportunity
        out["spread_boost_factor"] = boost
        out["relative_value_signal"] = min(max(confidence, 0.0), 1.0)
        enriched.append(out)

        if opportunity:
            opportunity_count += 1
        confidence_sum += confidence
        history_points_used += len(history)

    # Update baseline after scoring to avoid look-ahead bias in same invocation.
    for key, spreads in spreads_by_group.items():
        hist = _SPREAD_HISTORY[key]
        for s in spreads:
            hist.append(float(s))

    signal = "NO_OPPORTUNITY"
    if opportunity_count > 0:
        ratio = opportunity_count / max(len(enriched), 1)
        signal = "ELEVATED_OPPORTUNITY" if ratio >= 0.2 else "SELECTIVE_OPPORTUNITY"

    confidence_score = min(max(confidence_sum / max(len(enriched), 1), 0.0), 1.0)

    return {
        "bonds": enriched,
        "spread_signal": signal,
        "opportunity_flag": opportunity_count > 0,
        "confidence_score": round(confidence_score, 6),
        "summary": {
            "total_bonds": len(enriched),
            "opportunity_count": opportunity_count,
            "history_points_used": history_points_used,
            "spread_boost_factor": spread_boost_factor,
            "anomaly_sigma": anomaly_sigma,
        },
    }
