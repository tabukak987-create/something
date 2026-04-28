"""Portfolio Analyzer for recommendation workflows."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def portfolio_yield(positions: list[dict[str, Any]]) -> float:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions)
    if total <= 0:
        return 0.0
    return sum(float(p.get("ytm") or p.get("yield") or 0.0) * float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions) / total


def portfolio_duration(positions: list[dict[str, Any]]) -> float:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions)
    if total <= 0:
        return 0.0
    return sum(float(p.get("modified_duration") or p.get("duration") or 0.0) * float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions) / total


def rating_distribution(positions: list[dict[str, Any]]) -> dict[str, float]:
    return _bucket_pct(positions, key="credit_rating", fallback_key="rating")


def sector_exposure(positions: list[dict[str, Any]]) -> dict[str, float]:
    return _bucket_pct(positions, key="sector")


def issuer_exposure(positions: list[dict[str, Any]]) -> dict[str, float]:
    return _bucket_pct(positions, key="issuer_name", fallback_key="issuer")


def liquidity_ratio(positions: list[dict[str, Any]], threshold: float = 50.0) -> float:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions)
    if total <= 0:
        return 0.0
    liquid_value = sum(
        float(p.get("market_value") or p.get("allocation_amount") or 0.0)
        for p in positions
        if float(p.get("liquidity_score") or 0.0) >= threshold
    )
    return liquid_value / total


def cash_available(summary: dict[str, Any] | None = None) -> float:
    if not summary:
        return 0.0
    # Fallback hierarchy to avoid assumptions across deployments.
    return float(
        summary.get("cash_available")
        or summary.get("cash")
        or summary.get("cash_balance")
        or 0.0
    )


def analyze_portfolio(positions: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "portfolio_yield": round(portfolio_yield(positions), 6),
        "portfolio_duration": round(portfolio_duration(positions), 6),
        "rating_distribution": rating_distribution(positions),
        "sector_exposure": sector_exposure(positions),
        "issuer_exposure": issuer_exposure(positions),
        "liquidity_ratio": round(liquidity_ratio(positions), 6),
        "cash_available": round(cash_available(summary), 2),
    }


def _bucket_pct(positions: list[dict[str, Any]], key: str, fallback_key: str | None = None) -> dict[str, float]:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions) or 1.0
    bucket: dict[str, float] = defaultdict(float)
    for p in positions:
        name = p.get(key)
        if not name and fallback_key:
            name = p.get(fallback_key)
        if not name:
            name = "UNKNOWN"
        bucket[str(name)] += float(p.get("market_value") or p.get("allocation_amount") or 0.0)
    return {k: round((v / total) * 100.0, 4) for k, v in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)}
