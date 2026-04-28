"""Market timing layer to classify regime and adjust tactical scoring weights."""

from __future__ import annotations

from typing import Any


def infer_market_signals(
    bonds: list[dict[str, Any]],
    current_positions: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    rows = list(bonds or []) + list(current_positions or [])
    if not rows:
        return {
            "yield_curve_slope": 0.0,
            "gsec_yield_movement": 0.0,
            "credit_spread_trend": 0.0,
            "liquidity_indicator": 0.5,
        }

    short = [float(r.get("ytm") or r.get("yield") or 0.0) for r in rows if float(r.get("modified_duration") or r.get("duration") or 0.0) <= 3.0]
    long = [float(r.get("ytm") or r.get("yield") or 0.0) for r in rows if float(r.get("modified_duration") or r.get("duration") or 0.0) >= 7.0]
    short_avg = (sum(short) / len(short)) if short else 0.0
    long_avg = (sum(long) / len(long)) if long else short_avg
    slope = long_avg - short_avg

    gsec_moves = [
        float(r.get("gsec_yield_change_bps") or r.get("gsec_trend_bps") or 0.0)
        for r in rows
        if r.get("gsec_yield_change_bps") is not None or r.get("gsec_trend_bps") is not None
    ]
    spread_moves = [
        float(r.get("credit_spread_trend_bps") or r.get("spread_trend_bps") or 0.0)
        for r in rows
        if r.get("credit_spread_trend_bps") is not None or r.get("spread_trend_bps") is not None
    ]
    liq = [float(r.get("liquidity_score") or 0.0) for r in rows if r.get("liquidity_score") is not None]

    gsec_movement = (sum(gsec_moves) / len(gsec_moves)) if gsec_moves else 0.0

    # Fallback spread trend proxy from absolute spread level if explicit trend absent.
    if spread_moves:
        spread_trend = sum(spread_moves) / len(spread_moves)
    else:
        level = [float(r.get("spread_over_gsec") or r.get("spread_over_gsec_bps") or 0.0) for r in rows]
        spread_trend = ((sum(level) / len(level)) - 120.0) if level else 0.0

    liquidity = min(max(((sum(liq) / len(liq)) / 100.0) if liq else 0.5, 0.0), 1.0)

    return {
        "yield_curve_slope": round(slope, 6),
        "gsec_yield_movement": round(gsec_movement, 6),
        "credit_spread_trend": round(spread_trend, 6),
        "liquidity_indicator": round(liquidity, 6),
    }


def determine_market_mode(signals: dict[str, float]) -> dict[str, Any]:
    gsec_move = float(signals.get("gsec_yield_movement") or 0.0)
    spread_trend = float(signals.get("credit_spread_trend") or 0.0)
    slope = float(signals.get("yield_curve_slope") or 0.0)
    liq = float(signals.get("liquidity_indicator") or 0.5)

    if gsec_move < 0 and spread_trend > 0:
        mode = "AGGRESSIVE"
    elif gsec_move > 0 and spread_trend < 0:
        mode = "DEFENSIVE"
    else:
        mode = "NEUTRAL"

    confidence = min(
        1.0,
        (
            min(abs(gsec_move) / 10.0, 0.4)
            + min(abs(spread_trend) / 40.0, 0.4)
            + min(abs(slope) / 2.0, 0.1)
            + (0.1 if liq >= 0.6 else 0.05)
        ),
    )

    return {
        "mode": mode,
        "confidence_score": round(confidence, 6),
        "signals": signals,
    }


def apply_market_mode_overrides(mode_info: dict[str, Any], risk_flags: dict[str, Any]) -> dict[str, Any]:
    mode = str(mode_info.get("mode") or "NEUTRAL").upper()
    out = dict(mode_info)

    downgrade = int(risk_flags.get("credit_downgrade") or 0)
    liquidity_drop = int(risk_flags.get("liquidity_drop") or 0)
    spread_widening = int(risk_flags.get("spread_widening") or 0)

    if mode == "AGGRESSIVE" and (downgrade > 0 or liquidity_drop > 0 or spread_widening > 0):
        out["mode"] = "DEFENSIVE"
        out["override_applied"] = True
        out["override_reason"] = "Risk safety override disabled aggressive mode"
    else:
        out["override_applied"] = False
    return out


def adjust_scoring_weights_for_market_mode(
    base_weights: dict[str, float],
    market_mode: str,
) -> dict[str, float]:
    weights = {
        "yield_spread": float(base_weights.get("yield_spread") or 0.0),
        "liquidity": float(base_weights.get("liquidity") or 0.0),
        "rating_quality": float(base_weights.get("rating_quality") or 0.0),
        "duration_fit": float(base_weights.get("duration_fit") or 0.0),
        "market_trend": float(base_weights.get("market_trend") or 0.0),
        "relative_value": float(base_weights.get("relative_value") or 0.05),
    }

    mode = (market_mode or "NEUTRAL").upper()
    if mode == "AGGRESSIVE":
        weights["yield_spread"] += 0.07
        weights["relative_value"] += 0.08
        weights["duration_fit"] += 0.03
        weights["rating_quality"] = max(0.0, weights["rating_quality"] - 0.04)
    elif mode == "DEFENSIVE":
        weights["rating_quality"] += 0.09
        weights["liquidity"] += 0.06
        weights["duration_fit"] = max(0.0, weights["duration_fit"] - 0.04)
        weights["yield_spread"] = max(0.0, weights["yield_spread"] - 0.04)
        weights["relative_value"] = max(0.0, weights["relative_value"] - 0.02)

    total = sum(weights.values()) or 1.0
    return {k: v / total for k, v in weights.items()}


def strategy_mode_label(tactical_label: str, market_mode: str, has_spread_opportunity: bool) -> str:
    mode = (market_mode or "NEUTRAL").upper()
    if mode == "AGGRESSIVE" and has_spread_opportunity:
        return "Tactical Yield + Spread Opportunity"
    if mode == "DEFENSIVE":
        return "Defensive Quality Preservation"
    return tactical_label or "Balanced Tactical Allocation"
