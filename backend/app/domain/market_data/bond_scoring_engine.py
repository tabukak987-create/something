"""Bond scoring engine for recommendation ranking."""

from __future__ import annotations

from typing import Any

RATING_QUALITY_MAP = {
    "SOVEREIGN": 1.00,
    "AAA": 0.95,
    "AA+": 0.90,
    "AA": 0.85,
    "AA-": 0.80,
    "A+": 0.70,
    "A": 0.60,
    "A-": 0.50,
    "BBB+": 0.30,
    "BBB": 0.20,
    "NR": 0.10,
}


def score_bonds(
    bonds: list[dict[str, Any]],
    target_duration: float = 6.0,
) -> list[dict[str, Any]]:
    if not bonds:
        return []

    max_abs_rel = max(abs(float(b.get("relative_value") or 0.0)) for b in bonds) or 0.01
    max_ytm = max(float(b.get("ytm") or b.get("yield") or 0.0) for b in bonds) or 0.01
    scored: list[dict[str, Any]] = []
    for b in bonds:
        relative_value = float(b.get("relative_value") or 0.0)
        yield_spread = (relative_value / max_abs_rel + 1.0) / 2.0
        absolute_yield = min(1.0, max(0.0, float(b.get("ytm") or b.get("yield") or 0.0) / max_ytm))
        liquidity = min(1.0, max(0.0, float(b.get("liquidity_score") or 0.0) / 100.0))
        rating = str((b.get("credit_rating") or b.get("rating") or "NR")).upper()
        rating_quality = RATING_QUALITY_MAP.get(rating, 0.1)
        duration = float(b.get("modified_duration") or b.get("duration") or 0.0)
        duration_fit = max(0.0, 1.0 - (abs(duration - target_duration) / max(target_duration, 1.0)))
        market_trend = _market_trend_score(b)

        score = (
            0.30 * yield_spread
            + 0.15 * absolute_yield
            + 0.20 * liquidity
            + 0.20 * rating_quality
            + 0.15 * duration_fit
            + 0.10 * market_trend
        )

        out = dict(b)
        out["score"] = round(score, 6)
        out["score_components"] = {
            "yield_spread": round(yield_spread, 6),
            "absolute_yield": round(absolute_yield, 6),
            "liquidity": round(liquidity, 6),
            "rating_quality": round(rating_quality, 6),
            "duration_fit": round(duration_fit, 6),
            "market_trend": round(market_trend, 6),
        }
        scored.append(out)

    scored.sort(key=lambda x: (x["score"], float(x.get("ytm") or x.get("yield") or 0.0)), reverse=True)
    return scored


def _market_trend_score(bond: dict[str, Any]) -> float:
    # A tightening spread or strong traded volume is treated as positive trend.
    spread = float(bond.get("spread_over_gsec") or bond.get("spread_over_gsec_bps") or 0.0)
    volume = float(bond.get("traded_volume") or 0.0)
    vscore = 1.0 if volume >= 1_00_00_000 else 0.7 if volume >= 10_00_000 else 0.4
    sscore = 0.7 if spread > 0 else 0.5
    return min(1.0, (vscore + sscore) / 2.0)
