"""
Risk & Stress Testing Engine
=============================
Institutional-grade risk analytics for PF Trust bond portfolio:
 - Interest Rate Sensitivity (DV01, Duration-based PnL for rate shocks)
 - Credit Spread Simulation
 - Scenario Engine (RBI Rate Hike, Recession, Liquidity Freeze, etc.)
 - Parametric VaR (95%, 99%, Expected Shortfall)
 - Yield Curve Positioning
 - Duration Contribution Analysis

All calculations server-side. Tolerance: Â±0.01% yields, Â±â‚¹1 valuation.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.shared.database import fetch_all, fetch_one, execute_query
from app.shared.logger import get_logger
from app.domain.market_data.instrument_classification import INSTRUMENT_TYPE_SDS, classify_instrument
from app.domain.portfolio.analytics import (
    compute_bond_analytics,
    compute_market_value,
    RATING_SCORE,
    normalize_rating,
)
from app.shared.market_config import (
    GSEC_YIELD_CURVE,
    RATING_SPREAD_BPS,
    interpolate_gsec_yield,
)

log = get_logger("service.risk_engine")


RISK_REQUIRED_NUMERIC_FIELDS = (
    "market_value",
    "ytm",
    "modified_duration",
    "spread_bps",
)


def _state_payload(status: str, data: dict[str, Any] | None = None, message: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "data": data if isinstance(data, dict) else {},
        "message": message,
    }


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        numeric = float(value)
        if not math.isfinite(numeric):
            return default
        return numeric
    except Exception:
        return default


def _is_valid_number(value: Any) -> bool:
    try:
        numeric = float(value)
        return math.isfinite(numeric)
    except Exception:
        return False


def _portfolio_snapshot(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    total_market_value = round(sum(_safe_number(h.get("market_value"), 0.0) for h in holdings), 2)
    return {
        "holding_count": len(holdings),
        "total_market_value": total_market_value,
        "sample": [
            {
                "isin": h.get("isin"),
                "market_value": round(_safe_number(h.get("market_value"), 0.0), 2),
                "ytm": round(_safe_number(h.get("ytm"), 0.0), 6),
                "modified_duration": round(_safe_number(h.get("modified_duration"), 0.0), 6),
                "spread_bps": round(_safe_number(h.get("spread_bps"), 0.0), 4),
            }
            for h in holdings[:5]
        ],
    }


async def _load_validated_holdings(
    operation: str,
    *,
    required_numeric_fields: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    holdings = await _fetch_portfolio_with_analytics()
    snapshot = _portfolio_snapshot(holdings)
    log.info(
        "risk_holdings_loaded",
        operation=operation,
        input_payload={"required_numeric_fields": list(required_numeric_fields)},
        portfolio_snapshot=snapshot,
    )

    if not holdings:
        return [], _state_payload(
            "no_data",
            {"portfolio_snapshot": snapshot},
            "Insufficient portfolio data for risk computation",
        )

    required_fields = set(RISK_REQUIRED_NUMERIC_FIELDS).union(required_numeric_fields)
    valid_rows: list[dict[str, Any]] = []
    dropped_rows: list[dict[str, Any]] = []
    for row in holdings:
        missing_fields = [field for field in required_fields if not _is_valid_number(row.get(field))]
        if missing_fields:
            dropped_rows.append({"isin": row.get("isin"), "missing_fields": missing_fields})
            continue
        valid_rows.append(row)

    if dropped_rows:
        log.warning(
            "risk_holdings_dropped",
            operation=operation,
            dropped_count=len(dropped_rows),
            dropped_sample=dropped_rows[:5],
        )

    validated_snapshot = _portfolio_snapshot(valid_rows)
    if not valid_rows:
        return [], _state_payload(
            "no_data",
            {"portfolio_snapshot": validated_snapshot},
            "Insufficient portfolio data for risk computation",
        )

    total_market_value = _safe_number(validated_snapshot.get("total_market_value"), 0.0)
    if total_market_value <= 0:
        return [], _state_payload(
            "no_data",
            {"portfolio_snapshot": validated_snapshot},
            "Portfolio value is zero",
        )

    return valid_rows, None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# CONSTANTS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

# Historical daily yield volatility (bps) by rating tier â€” Indian bond market
YIELD_VOL_BPS = {
    "SOV": 4.5, "SOVEREIGN": 4.5,
    "AAA": 5.0,
    "AA+": 6.0, "AA": 7.0, "AA-": 8.0,
    "A+": 10.0, "A": 12.0, "A-": 14.0,
    "BBB+": 18.0, "BBB": 22.0,
    "NR": 10.0,
}

# Correlation assumptions (simplified)
INTRA_RATING_CORR = 0.85  # bonds within same rating
INTER_RATING_CORR = 0.60  # bonds across ratings
SOVEREIGN_CORP_CORR = 0.50

# Scenario definitions
SCENARIOS = {
    "RBI Rate Hike (+50bps)": {
        "type": "RATE_SHOCK",
        "rate_shift_bps": 50,
        "spread_shift_bps": 10,
        "description": "RBI raises repo rate by 50bps. Corporate spreads widen slightly.",
    },
    "RBI Rate Hike (+100bps)": {
        "type": "RATE_SHOCK",
        "rate_shift_bps": 100,
        "spread_shift_bps": 20,
        "description": "Aggressive tightening â€” rates up 100bps with spread widening.",
    },
    "Rate Cut (-50bps)": {
        "type": "RATE_SHOCK",
        "rate_shift_bps": -50,
        "spread_shift_bps": -5,
        "description": "RBI eases policy. Bonds rally, spreads tighten.",
    },
    "Recession / Flight to Quality": {
        "type": "MACRO",
        "rate_shift_bps": -75,
        "spread_shift_bps": 100,
        "description": "Rates fall but credit spreads blow out. Sovereign outperforms.",
    },
    "Liquidity Freeze": {
        "type": "MACRO",
        "rate_shift_bps": 25,
        "spread_shift_bps": 150,
        "description": "NBFC/HFC credit event triggers broad risk-off. Illiquid bonds hit hardest.",
    },
    "Curve Steepening": {
        "type": "RATE_SHOCK",
        "rate_shift_bps": 0,  # short end flat, long end up
        "spread_shift_bps": 0,
        "is_curve_shift": True,
        "short_shift_bps": -10,
        "long_shift_bps": 50,
        "description": "Yield curve steepens â€” short rates fall, long rates rise.",
    },
    "Curve Inversion": {
        "type": "RATE_SHOCK",
        "rate_shift_bps": 0,
        "spread_shift_bps": 0,
        "is_curve_shift": True,
        "short_shift_bps": 50,
        "long_shift_bps": -10,
        "description": "Invite yield curve. Short rates spike, long rates stable.",
    },
    "Credit Downgrade Wave": {
        "type": "CREDIT",
        "rate_shift_bps": 0,
        "spread_shift_bps": 75,
        "description": "Widespread credit downgrades. All non-SOV spreads widen 75bps.",
    },
    "Largest Issuer Default": {
        "type": "CREDIT",
        "rate_shift_bps": 0,
        "spread_shift_bps": 0,
        "description": "Total loss on largest non-sovereign issuer exposure.",
    },
}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# RATE SENSITIVITY (DV01 / DURATION IMPACT)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def compute_rate_sensitivity() -> dict:
    """
    Compute portfolio impact for parallel rate shocks:
    Î”P â‰ˆ -Duration Ã— Î”Y Ã— P + 0.5 Ã— Convexity Ã— (Î”Y)Â² Ã— P
    """
    shocks_bps = [-100, -50, -25, 25, 50, 100, 150, 200]
    try:
        holdings, error_state = await _load_validated_holdings(
            "rate_sensitivity",
            required_numeric_fields=("convexity",),
        )
        if error_state:
            return error_state

        total_mv = sum(h["market_value"] for h in holdings)
        if total_mv <= 0:
            return _state_payload("no_data", message="Portfolio value is zero")

        portfolio_weighted_ytm = sum(h["ytm"] * h["market_value"] for h in holdings) / total_mv

        results = []
        for shock in shocks_bps:
            dy = shock / 10000.0
            portfolio_pnl = 0.0
            for h in holdings:
                mv = h["market_value"]
                dur = h["modified_duration"]
                conv = h["convexity"]
                delta_p = (-dur * dy + 0.5 * conv * dy * dy) * mv
                portfolio_pnl += delta_p

            results.append({
                "shock_bps": shock,
                "post_shock_yield": round(portfolio_weighted_ytm + (shock / 100.0), 4),
                "pnl": round(portfolio_pnl, 2),
                "pnl_pct": round(portfolio_pnl / total_mv * 100, 4),
                "new_value": round(total_mv + portfolio_pnl, 2),
                "pnl_crores": round(portfolio_pnl / 1e7, 4),
            })

        dur_contrib = []
        for h in holdings:
            mv_weight = h["market_value"] / total_mv
            dur_contribution = h["modified_duration"] * mv_weight
            r = str(h.get("rating") or "").upper()
            rating_display = normalize_rating(r, h.get("category"), isin=h.get("isin"), issuer_name=h.get("issuer_name"))
            rating_display = "Sovereign" if rating_display == "SOVEREIGN" else (rating_display if rating_display != "NR" else "Unrated")
            dur_contrib.append({
                "isin": h["isin"],
                "issuer": h["issuer_name"][:40],
                "category": h.get("category") or h.get("bond_issuer_type") or "",
                "rating": rating_display,
                "market_value": round(h["market_value"], 2),
                "weight_pct": round(mv_weight * 100, 2),
                "modified_duration": h["modified_duration"],
                "duration_contribution": round(dur_contribution, 4),
                "dv01": round(h["market_value"] * h["modified_duration"] / 10000, 2),
            })

        dur_contrib.sort(key=lambda x: -x["duration_contribution"])
        payload = {
            "rate_shocks": results,
            "total_market_value": round(total_mv, 2),
            "portfolio_duration": round(sum(d["duration_contribution"] for d in dur_contrib), 4),
            "portfolio_dv01": round(sum(d["dv01"] for d in dur_contrib), 2),
            "duration_contributions": dur_contrib[:20],
            "portfolio_snapshot": _portfolio_snapshot(holdings),
        }
        log.info("risk_compute_complete", operation="rate_sensitivity", portfolio_snapshot=payload["portfolio_snapshot"])
        return _state_payload("ok", payload)
    except Exception as exc:
        log.exception("risk_compute_failed", operation="rate_sensitivity", input_payload={"shocks_bps": shocks_bps})
        return _state_payload("error", message=str(exc))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# CREDIT SPREAD SIMULATION
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def compute_spread_simulation() -> dict:
    """
    Credit spread scenarios:
    1. Parallel spread widening (+50bps on all non-SOV)
    2. Rating-tiered widening (lower ratings widen more)
    3. Single issuer downgrade (largest non-SOV issuer)
    """
    tier_spread = {"AAA": 20, "AA+": 35, "AA": 50, "AA-": 70, "A+": 100, "A": 130, "A-": 160, "BBB+": 200}
    try:
        holdings, error_state = await _load_validated_holdings(
            "spread_simulation",
            required_numeric_fields=("convexity",),
        )
        if error_state:
            return error_state

        total_mv = sum(h["market_value"] for h in holdings)
        if total_mv <= 0:
            return _state_payload("no_data", message="Portfolio value is zero")

        pnl_parallel = 0.0
        for h in holdings:
            if h["rating"] not in ("SOV", "SOVEREIGN"):
                dy = 50 / 10000.0
                delta = (-h["modified_duration"] * dy + 0.5 * h["convexity"] * dy * dy) * h["market_value"]
                pnl_parallel += delta

        pnl_tiered = 0.0
        for h in holdings:
            rating = h["rating"]
            if rating in ("SOV", "SOVEREIGN"):
                continue
            widen = tier_spread.get(rating, 100)
            dy = widen / 10000.0
            delta = (-h["modified_duration"] * dy + 0.5 * h["convexity"] * dy * dy) * h["market_value"]
            pnl_tiered += delta

        issuer_exposure = {}
        for h in holdings:
            if h["rating"] not in ("SOV", "SOVEREIGN"):
                iss = h["issuer_name"]
                issuer_exposure[iss] = issuer_exposure.get(iss, 0) + h["market_value"]

        worst_issuer = max(issuer_exposure.items(), key=lambda x: x[1]) if issuer_exposure else ("N/A", 0)
        pnl_default = -worst_issuer[1]

        payload = {
            "parallel_50bps": {
                "description": "All non-sovereign spreads widen 50bps",
                "pnl": round(pnl_parallel, 2),
                "pnl_pct": round(pnl_parallel / total_mv * 100, 4),
            },
            "tiered_widening": {
                "description": "Rating-tiered spread widening (AAA +20, AA +50, A +130bps)",
                "pnl": round(pnl_tiered, 2),
                "pnl_pct": round(pnl_tiered / total_mv * 100, 4),
            },
            "largest_default": {
                "description": f"Complete default by '{worst_issuer[0]}' (100% loss)",
                "issuer": worst_issuer[0],
                "exposure": round(worst_issuer[1], 2),
                "pnl": round(pnl_default, 2),
                "pnl_pct": round(pnl_default / total_mv * 100, 4),
            },
            "total_market_value": round(total_mv, 2),
            "portfolio_snapshot": _portfolio_snapshot(holdings),
        }
        log.info("risk_compute_complete", operation="spread_simulation", portfolio_snapshot=payload["portfolio_snapshot"])
        return _state_payload("ok", payload)
    except Exception as exc:
        log.exception("risk_compute_failed", operation="spread_simulation")
        return _state_payload("error", message=str(exc))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# SCENARIO ENGINE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def run_scenarios() -> dict:
    """Run all predefined macro/credit/rate scenarios."""
    try:
        holdings, error_state = await _load_validated_holdings(
            "scenario_analysis",
            required_numeric_fields=("convexity", "years_to_maturity"),
        )
        if error_state:
            return error_state

        total_mv = sum(h["market_value"] for h in holdings)
        if total_mv <= 0:
            return _state_payload("no_data", message="Portfolio value is zero")

        today_str = str(date.today())
        results = []

        for name, scenario in SCENARIOS.items():
            pnl = _apply_scenario(holdings, scenario, total_mv)

            result = {
                "scenario_name": name,
                "scenario_type": scenario["type"],
                "description": scenario["description"],
                "rate_shift_bps": scenario.get("rate_shift_bps", 0),
                "spread_shift_bps": scenario.get("spread_shift_bps", 0),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / total_mv * 100, 4),
                "new_value": round(total_mv + pnl, 2),
                "pnl_crores": round(pnl / 1e7, 4),
            }
            results.append(result)

            try:
                await execute_query("""
                    INSERT INTO scenario_results (
                        calc_date, scenario_name, scenario_type,
                        rate_shift_bps, spread_shift_bps,
                        portfolio_pnl, portfolio_pnl_pct, new_nav, duration_impact, details_json
                    ) VALUES (
                        :cd, :sn, :st, :rs, :ss, :pnl, :pnl_pct, :nav, :di, :dj
                    ) ON CONFLICT(calc_date, scenario_name) DO UPDATE SET
                        portfolio_pnl = excluded.portfolio_pnl,
                        portfolio_pnl_pct = excluded.portfolio_pnl_pct,
                        new_nav = excluded.new_nav,
                        duration_impact = excluded.duration_impact,
                        details_json = excluded.details_json
                """, {
                    "cd": today_str, "sn": name, "st": scenario["type"],
                    "rs": scenario.get("rate_shift_bps", 0),
                    "ss": scenario.get("spread_shift_bps", 0),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / total_mv * 100, 4),
                    "nav": round(total_mv + pnl, 2),
                    "di": round(sum(h["modified_duration"] * h["market_value"] for h in holdings) / total_mv, 4),
                    "dj": json.dumps(result),
                })
            except Exception as e:
                log.warning("persist_scenario_failed", scenario=name, error=str(e))

        results.sort(key=lambda x: x["pnl"])
        payload = {
            "scenarios": results,
            "total_market_value": round(total_mv, 2),
            "worst_case": results[0] if results else None,
            "best_case": results[-1] if results else None,
            "calc_date": today_str,
            "portfolio_snapshot": _portfolio_snapshot(holdings),
        }
        log.info("risk_compute_complete", operation="scenario_analysis", portfolio_snapshot=payload["portfolio_snapshot"])
        return _state_payload("ok", payload)
    except Exception as exc:
        log.exception("risk_compute_failed", operation="scenario_analysis")
        return _state_payload("error", message=str(exc))


def _apply_scenario(holdings: list, scenario: dict, total_mv: float) -> float:
    """Apply a scenario to the portfolio and return PnL."""

    # Special case: Largest issuer default
    if scenario.get("description", "").startswith("Total loss"):
        issuer_exposure = {}
        for h in holdings:
            if h["rating"] not in ("SOV", "SOVEREIGN"):
                iss = h["issuer_name"]
                issuer_exposure[iss] = issuer_exposure.get(iss, 0) + h["market_value"]
        if issuer_exposure:
            return -max(issuer_exposure.values())
        return 0

    pnl = 0.0
    is_curve = scenario.get("is_curve_shift", False)

    for h in holdings:
        mv = h["market_value"]
        dur = h["modified_duration"]
        conv = h["convexity"]
        is_sov = h["rating"] in ("SOV", "SOVEREIGN")
        years = h["years_to_maturity"]

        # Rate shift
        if is_curve:
            # Interpolate between short and long shift based on maturity
            short_shift = scenario.get("short_shift_bps", 0)
            long_shift = scenario.get("long_shift_bps", 0)
            if years <= 2:
                rate_shift = short_shift
            elif years >= 10:
                rate_shift = long_shift
            else:
                frac = (years - 2) / 8
                rate_shift = short_shift + (long_shift - short_shift) * frac
        else:
            rate_shift = scenario.get("rate_shift_bps", 0)

        # Spread shift â€” only for non-sovereign
        spread_shift = 0
        if not is_sov:
            spread_shift = scenario.get("spread_shift_bps", 0)

        total_shift_bps = rate_shift + spread_shift
        dy = total_shift_bps / 10000.0

        # Duration-convexity approximation
        delta = (-dur * dy + 0.5 * conv * dy * dy) * mv
        pnl += delta

    return pnl


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# VALUE AT RISK (VaR)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def compute_var() -> dict:
    """
    Parametric VaR using:
      VaR = Z Ã— Ïƒ_portfolio Ã— âˆšt Ã— Portfolio_Value

    Ïƒ_portfolio estimated from:
      - Individual bond yield volatilities (rating-based)
      - Modified duration for price sensitivity
      - Correlation structure (block diagonal)

    Reports: 1-day, 10-day VaR at 95% and 99%, plus Expected Shortfall.
    """
    try:
        holdings, error_state = await _load_validated_holdings("value_at_risk")
        if error_state:
            return error_state

        total_mv = sum(h["market_value"] for h in holdings)
        if total_mv <= 0:
            return _state_payload("no_data", message="Portfolio value is zero")

        bond_vols = []
        weights = []
        for h in holdings:
            rating = str(h.get("rating") or "NR")
            yield_vol_bps = YIELD_VOL_BPS.get(rating, 10.0) / 10000.0
            price_vol = h["modified_duration"] * yield_vol_bps
            w = h["market_value"] / total_mv
            bond_vols.append(price_vol)
            weights.append(w)

        n = len(holdings)
        portfolio_var = sum(w * w * v * v for w, v in zip(weights, bond_vols))
        rating_buckets: dict[str, float] = {}
        for i in range(n):
            r = str(holdings[i].get("rating") or "NR")
            rating_buckets[r] = rating_buckets.get(r, 0.0) + weights[i] * bond_vols[i]

        bucket_keys = list(rating_buckets.keys())
        sov_set = {"SOV", "SOVEREIGN"}

        for rk1 in bucket_keys:
            wv1 = rating_buckets[rk1]
            for rk2 in bucket_keys:
                wv2 = rating_buckets[rk2]
                if rk1 == rk2:
                    self_var = sum(
                        weights[k] ** 2 * bond_vols[k] ** 2
                        for k in range(n) if str(holdings[k].get("rating") or "NR") == rk1
                    )
                    portfolio_var += INTRA_RATING_CORR * (wv1 * wv2 - self_var)
                elif (rk1 in sov_set) != (rk2 in sov_set):
                    portfolio_var += SOVEREIGN_CORP_CORR * wv1 * wv2
                else:
                    portfolio_var += INTER_RATING_CORR * wv1 * wv2

        portfolio_vol_daily = math.sqrt(max(portfolio_var, 0.0))

        z_95 = 1.6449
        z_99 = 2.3263
        phi_95 = 0.10314
        phi_99 = 0.02665

        var_95_1d = round(z_95 * portfolio_vol_daily * total_mv, 2)
        var_99_1d = round(z_99 * portfolio_vol_daily * total_mv, 2)
        var_95_10d = round(z_95 * portfolio_vol_daily * math.sqrt(10) * total_mv, 2)
        var_99_10d = round(z_99 * portfolio_vol_daily * math.sqrt(10) * total_mv, 2)
        es_95_1d = round(phi_95 / 0.05 * portfolio_vol_daily * total_mv, 2)
        es_99_1d = round(phi_99 / 0.01 * portfolio_vol_daily * total_mv, 2)

        category_stats: dict[str, dict[str, float]] = {}
        for h in holdings:
            category = str(h.get("category") or h.get("rating") or "UNKNOWN").strip() or "UNKNOWN"
            rating = str(h.get("rating") or "NR")
            yield_vol = YIELD_VOL_BPS.get(rating, 10.0) / 10000.0
            daily_price_vol = h["modified_duration"] * yield_vol
            daily_vol_amount = daily_price_vol * h["market_value"]
            cat = category_stats.setdefault(category, {
                "market_value": 0.0,
                "daily_vol": 0.0,
                "var_95": 0.0,
            })
            cat["market_value"] += h["market_value"]
            cat["daily_vol"] += daily_vol_amount
            cat["var_95"] += z_95 * daily_vol_amount

        total_var95 = sum(c["var_95"] for c in category_stats.values())
        var_by_category = [
            {
                "category": name,
                "market_value": round(vals["market_value"], 2),
                "daily_vol": round(vals["daily_vol"], 2),
                "var_95": round(vals["var_95"], 2),
                "pct_of_total": round((vals["var_95"] / total_var95 * 100.0), 2) if total_var95 > 0 else 0.0,
            }
            for name, vals in sorted(category_stats.items(), key=lambda item: item[1]["var_95"], reverse=True)
        ]

        try:
            today_str = str(date.today())
            await execute_query("""
                UPDATE risk_metrics SET
                    var_95_1d = :v95,
                    var_99_1d = :v99,
                    expected_shortfall_95 = :es95
                WHERE calc_date = :cd
            """, {"v95": var_95_1d, "v99": var_99_1d, "es95": es_95_1d, "cd": today_str})
            row = await fetch_one("SELECT 1 FROM risk_metrics WHERE calc_date = :cd", {"cd": today_str})
            if not row:
                await execute_query("""
                    INSERT INTO risk_metrics (calc_date, var_95_1d, var_99_1d, expected_shortfall_95)
                    VALUES (:cd, :v95, :v99, :es95)
                """, {"cd": today_str, "v95": var_95_1d, "v99": var_99_1d, "es95": es_95_1d})
        except Exception as e:
            log.warning("var_persist_failed", error=str(e))

        payload = {
            "portfolio_value": round(total_mv, 2),
            "portfolio_daily_vol_pct": round(portfolio_vol_daily * 100, 6),
            "var": {
                "var_95_1d": var_95_1d,
                "var_95_1d_pct": round(z_95 * portfolio_vol_daily * 100, 4),
                "var_99_1d": var_99_1d,
                "var_99_1d_pct": round(z_99 * portfolio_vol_daily * 100, 4),
                "var_95_10d": var_95_10d,
                "var_99_10d": var_99_10d,
                "var_95_1d_crores": round(var_95_1d / 1e7, 4),
                "var_99_1d_crores": round(var_99_1d / 1e7, 4),
            },
            "expected_shortfall": {
                "es_95_1d": es_95_1d,
                "es_95_1d_pct": round(es_95_1d / total_mv * 100, 4),
                "es_99_1d": es_99_1d,
                "es_99_1d_pct": round(es_99_1d / total_mv * 100, 4),
            },
            "var_by_category": var_by_category,
            "methodology": "Parametric VaR (variance-covariance), rating-based yield volatilities, block-diagonal correlation",
            "calc_date": str(date.today()),
            "portfolio_snapshot": _portfolio_snapshot(holdings),
        }
        log.info("risk_compute_complete", operation="value_at_risk", portfolio_snapshot=payload["portfolio_snapshot"])
        return _state_payload("ok", payload)
    except Exception as exc:
        log.exception("risk_compute_failed", operation="value_at_risk")
        return _state_payload("error", message=str(exc))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# YIELD CURVE POSITIONING
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def compute_yield_curve_position() -> dict:
    """
    Map portfolio holdings on the yield curve.
    Shows actual YTM vs G-Sec benchmark yield at each tenor.
    """
    try:
        holdings, error_state = await _load_validated_holdings(
            "yield_curve_position",
            required_numeric_fields=("years_to_maturity",),
        )
        if error_state:
            return error_state

        total_mv = sum(h["market_value"] for h in holdings)
        if total_mv <= 0:
            return _state_payload("no_data", message="Portfolio value is zero")

        curve_points = []
        for tenor, gsec_yield in sorted(GSEC_YIELD_CURVE.items()):
            curve_points.append({
                "tenor": tenor,
                "gsec_yield": gsec_yield,
            })

        tenor_buckets = {t: {"gsec_yield": y, "holdings": [], "total_mv": 0, "avg_ytm": 0, "count": 0}
                         for t, y in GSEC_YIELD_CURVE.items()}

        for h in holdings:
            yrs = h["years_to_maturity"]
            tenors = sorted(GSEC_YIELD_CURVE.keys())
            nearest = min(tenors, key=lambda t: abs(t - yrs))

            bucket = tenor_buckets[nearest]
            bucket["holdings"].append({
                "isin": h["isin"],
                "issuer": h["issuer_name"][:30],
                "ytm": h["ytm"],
                "spread": h["spread_bps"],
                "weight": h["market_value"] / total_mv * 100,
            })
            bucket["total_mv"] += h["market_value"]
            bucket["avg_ytm"] = (bucket["avg_ytm"] * bucket["count"] + h["ytm"]) / (bucket["count"] + 1)
            bucket["count"] += 1

        curve_data = []
        for tenor in sorted(GSEC_YIELD_CURVE.keys()):
            b = tenor_buckets[tenor]
            curve_data.append({
                "tenor": tenor,
                "gsec_yield": b["gsec_yield"],
                "portfolio_yield": round(b["avg_ytm"], 4) if b["count"] > 0 else None,
                "spread_bps": round((b["avg_ytm"] - b["gsec_yield"]) * 100, 2) if b["count"] > 0 else None,
                "weight_pct": round(b["total_mv"] / total_mv * 100, 2),
                "count": b["count"],
            })

        payload = {
            "yield_curve": curve_data,
            "gsec_curve": curve_points,
            "portfolio_weighted_yield": round(sum(h["ytm"] * h["market_value"] for h in holdings) / total_mv, 4),
            "portfolio_spread_bps": round(sum(h["spread_bps"] * h["market_value"] for h in holdings) / total_mv, 2),
            "calc_date": str(date.today()),
            "portfolio_snapshot": _portfolio_snapshot(holdings),
        }
        log.info("risk_compute_complete", operation="yield_curve_position", portfolio_snapshot=payload["portfolio_snapshot"])
        return _state_payload("ok", payload)
    except Exception as exc:
        log.exception("risk_compute_failed", operation="yield_curve_position")
        return _state_payload("error", message=str(exc))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# COMPREHENSIVE RISK REPORT
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def compute_full_risk_analytics() -> dict:
    """Master function â€” computes everything for the Risk & Analysis tab."""
    rate_sensitivity_state = await compute_rate_sensitivity()
    spread_sim_state = await compute_spread_simulation()
    scenarios_state = await run_scenarios()
    var_state = await compute_var()
    yield_curve_state = await compute_yield_curve_position()

    rate_sensitivity = (rate_sensitivity_state.get("data") or {}) if rate_sensitivity_state.get("status") == "ok" else {}
    spread_sim = (spread_sim_state.get("data") or {}) if spread_sim_state.get("status") == "ok" else {}
    scenarios = (scenarios_state.get("data") or {}) if scenarios_state.get("status") == "ok" else {}
    var_data = (var_state.get("data") or {}) if var_state.get("status") == "ok" else {}
    yield_curve = (yield_curve_state.get("data") or {}) if yield_curve_state.get("status") == "ok" else {}

    # Update stress fields in risk_metrics
    try:
        today_str = str(date.today())
        rate_shocks = {r["shock_bps"]: r["pnl"] for r in rate_sensitivity.get("rate_shocks", []) if isinstance(r, dict)}
        await execute_query("""
            UPDATE risk_metrics SET
                stress_rate_up_25 = :u25,
                stress_rate_up_50 = :u50,
                stress_rate_up_100 = :u100,
                stress_rate_down_50 = :d50,
                stress_spread_50 = :s50,
                stress_largest_default = :ld
            WHERE calc_date = :cd
        """, {
            "u25": rate_shocks.get(25, 0),
            "u50": rate_shocks.get(50, 0),
            "u100": rate_shocks.get(100, 0),
            "d50": rate_shocks.get(-50, 0),
            "s50": spread_sim.get("parallel_50bps", {}).get("pnl", 0),
            "ld": spread_sim.get("largest_default", {}).get("pnl", 0),
            "cd": today_str,
        })
    except Exception as e:
        log.warning("stress_persist_failed", error=str(e))

    return {
        "rate_sensitivity": rate_sensitivity,
        "spread_simulation": spread_sim,
        "scenarios": scenarios,
        "var": var_data,
        "yield_curve": yield_curve,
        "component_states": {
            "rate_sensitivity": rate_sensitivity_state.get("status"),
            "spread_simulation": spread_sim_state.get("status"),
            "scenarios": scenarios_state.get("status"),
            "var": var_state.get("status"),
            "yield_curve": yield_curve_state.get("status"),
        },
        "calc_date": str(date.today()),
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# INTERNAL: Fetch portfolio with pre-computed analytics
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _fetch_portfolio_with_analytics() -> list[dict]:
    """Fetch all portfolio holdings with latest market data and compute per-bond analytics."""
    today = date.today()

    query = """
    SELECT
        p.isin, p.issuer_name, p.face_value, p.book_value, p.quantity,
        p.purchase_date, p.maturity_date, p.coupon_rate, p.ytm_at_purchase,
        p.category, p.sector,
        COALESCE(
            NULLIF(UPPER(TRIM(p.rating)), ''),
            NULLIF(UPPER(TRIM(b.credit_rating)), ''),
            CASE WHEN b.issuer_type IN ('G-SEC','SDL','T-BILL','CENTRAL_GOVT','STATE_GOVT','SOVEREIGN')
                      OR (SUBSTR(p.isin,1,2) = 'IN' AND SUBSTR(p.isin,3,1) BETWEEN '0' AND '9' AND p.isin NOT LIKE 'INE%')
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%STATE DEVELOPMENT LOAN%'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%SDL%'
                      OR UPPER(COALESCE(b.issuer_name,'')) LIKE '%STATE DEVELOPMENT LOAN%'
                      OR UPPER(COALESCE(b.issuer_name,'')) LIKE '%SDL%'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '% GOI %'
                      OR UPPER(COALESCE(b.issuer_name,'')) LIKE '%GOVT OF %'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%UDAY BOND%'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%UDAY %'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%GUJARAT STATE INVESTMENTS%'
                 THEN 'SOVEREIGN' ELSE NULL END
        ) AS rating,
        b.issuer_type  AS bond_issuer_type,
        m.clean_price, m.ytm, m.modified_duration, m.dv01, m.convexity,
        m.spread_over_gsec, m.traded_volume, m.liquidity_score,
        m.data_source, m.price_date,
        b.coupon_frequency, b.issue_date
    FROM portfolio_current_positions p
    LEFT JOIN (
        SELECT isin, clean_price, ytm, modified_duration, dv01, convexity,
               spread_over_gsec, traded_volume, liquidity_score, data_source, price_date
        FROM unified_market_data
        WHERE (isin, price_date) IN (
            SELECT isin, MAX(price_date) FROM unified_market_data GROUP BY isin
        )
    ) m ON p.isin = m.isin
    LEFT JOIN universal_bond_master b ON p.isin = b.isin
    WHERE p.quantity > 0
    """

    try:
        rows = await fetch_all(query)
    except Exception:
        log.exception("risk_portfolio_fetch_failed")
        return []

    holdings = []

    for h in rows:
        face = _safe_number(h.get("face_value"), 1000.0)
        qty = _safe_number(h.get("quantity"), 0.0)
        if qty <= 0:
            continue
        coupon = _safe_number(h.get("coupon_rate"), 0.0)
        clean_price = _safe_number(h.get("clean_price"), 0.0)
        ytm = _safe_number(h.get("ytm"), coupon if coupon > 0 else 7.0)
        freq = int(_safe_number(h.get("coupon_frequency"), 2.0)) or 2
        raw_r = str(h["rating"] or "").upper().strip()
        bond_itype = str(h.get("bond_issuer_type") or h.get("category") or "").upper()
        rating = normalize_rating(raw_r, bond_itype, isin=h["isin"], issuer_name=h.get("issuer_name"))
        classification = classify_instrument(h)

        try:
            mat_date = date.fromisoformat(str(h["maturity_date"])[:10])
        except Exception:
            mat_date = today + timedelta(days=365 * 5)

        try:
            issue_dt = date.fromisoformat(str(h["issue_date"])[:10]) if h.get("issue_date") else None
        except Exception:
            issue_dt = None

        try:
            analytics = compute_bond_analytics(
                coupon_rate=coupon, face_value=face, clean_price=clean_price,
                ytm=ytm, maturity_date=mat_date, coupon_frequency=freq,
                issue_date=issue_dt, settlement_date=today,
            )
        except Exception:
            log.warning("risk_holding_analytics_failed", isin=h.get("isin"), issuer=h.get("issuer_name"))
            years_to_maturity = max((mat_date - today).days / 365.25, 0.0)
            fallback_modified_duration = _safe_number(h.get("modified_duration"), 0.0)
            if fallback_modified_duration <= 0.0:
                fallback_modified_duration = max(years_to_maturity / (1.0 + max(ytm, 0.0) / 100.0), 0.0)
            fallback_convexity = _safe_number(h.get("convexity"), 0.0)
            analytics = {
                "dirty_price": clean_price,
                "modified_duration": fallback_modified_duration,
                "macaulay_duration": max(fallback_modified_duration, years_to_maturity),
                "convexity": fallback_convexity,
                "dv01": 0.0,
                "years_to_maturity": years_to_maturity,
                "spread_over_gsec_bps": _safe_number(h.get("spread_over_gsec"), 0.0),
            }

        mv = _safe_number(compute_market_value(clean_price, face, qty), 0.0)

        holdings.append({
            "isin": h["isin"],
            "issuer_name": h.get("issuer_name", ""),
            "rating": rating,
            "category": h.get("category") or h.get("bond_issuer_type") or "",
            "instrument_type": classification.get("type"),
            "sub_type": classification.get("sub_type"),
            "face_value": face,
            "quantity": qty,
            "coupon_rate": coupon,
            "clean_price": clean_price,
            "dirty_price": _safe_number(analytics.get("dirty_price"), 0.0),
            "ytm": ytm,
            "market_value": mv,
            "modified_duration": _safe_number(analytics.get("modified_duration"), 0.0),
            "macaulay_duration": _safe_number(analytics.get("macaulay_duration"), 0.0),
            "convexity": _safe_number(analytics.get("convexity"), 0.0),
            "dv01": _safe_number(analytics.get("dv01"), 0.0),
            "spread_bps": _safe_number(analytics.get("spread_over_gsec_bps"), 0.0),
            "years_to_maturity": _safe_number(analytics.get("years_to_maturity"), 0.0),
            "liquidity_score": _safe_number(h.get("liquidity_score"), 50.0),
        })

    return holdings

