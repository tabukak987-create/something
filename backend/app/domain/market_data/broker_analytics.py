"""
Broker Analytics Engine
========================
Computes per-broker behaviour patterns, pricing trends, and reliability scores
from historical broker_daily_quotes data.

For each broker the engine derives:
  • avg_ytm_premium_bps   — how many bps above/below the same-day peer average
  • avg_spread_bps        — average bid-ask spread in bps
  • pricing_consistency   — stdev of daily ytm_premium (lower = more predictable)
  • trend_direction       — TIGHTENING | WIDENING | STABLE  (7-day slope)
  • reliability_score     — 0-100 blended from coverage + consistency + recency
  • pattern_summary       — human-readable one-liner
  • behaviour_tags        — JSON list of categorical tags

Results are stored in broker_profiles.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.shared.database import fetch_all, execute_query
from app.shared.logger import get_logger

log = get_logger("services.broker_analytics")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_RECENCY_DECAY_DAYS = 30    # score decays to 0 if broker last seen > 30 days ago
_TREND_WINDOW_DAYS  = 7     # days to use for trend slope
_STALE_DAYS         = 7     # a broker is "stale" if no quotes for this many days


# ─────────────────────────────────────────────────────────────────────────────
# Per-day comparison: build same-day peer average YTM per ISIN
# ─────────────────────────────────────────────────────────────────────────────

async def _build_peer_averages(start_date: str, end_date: str) -> Dict[str, Dict[str, float]]:
    """
    Returns {quote_date: {isin: avg_ytm_across_all_brokers}}.
    Only considers rows where ytm IS NOT NULL.
    """
    rows = await fetch_all(
        """
        SELECT isin, quote_date, AVG(ytm) as avg_ytm
        FROM broker_daily_quotes
        WHERE ytm IS NOT NULL
          AND quote_date BETWEEN :start AND :end
        GROUP BY isin, quote_date
        """,
        {"start": start_date, "end": end_date},
    )
    result: Dict[str, Dict[str, float]] = {}
    for row in rows:
        d = row["quote_date"]
        if d not in result:
            result[d] = {}
        if row["avg_ytm"]:
            result[d][row["isin"]] = row["avg_ytm"]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Broker-level statistics
# ─────────────────────────────────────────────────────────────────────────────

async def _compute_broker_stats(broker: str, peer_avg: Dict[str, Dict[str, float]]) -> Dict:
    """
    For a single broker, compute all analytical metrics.
    """
    rows = await fetch_all(
        """
        SELECT isin, quote_date, ytm, bid_yield, ask_yield, clean_price
        FROM broker_daily_quotes
        WHERE broker_name = :broker AND ytm IS NOT NULL
        ORDER BY quote_date ASC
        """,
        {"broker": broker},
    )

    if not rows:
        return {}

    # ── Coverage info ──────────────────
    isins = set(r["isin"] for r in rows)
    dates = sorted(set(r["quote_date"] for r in rows))
    first_date = dates[0] if dates else None
    last_date  = dates[-1] if dates else None

    # ── YTM premium vs peers (bps) ─────
    premiums: List[float] = []
    daily_premiums: Dict[str, List[float]] = {}  # date → list of premiums

    for row in rows:
        peer = peer_avg.get(row["quote_date"], {}).get(row["isin"])
        if peer and peer > 0:
            prem_bps = (row["ytm"] - peer) * 10_000
            premiums.append(prem_bps)
            daily_premiums.setdefault(row["quote_date"], []).append(prem_bps)

    avg_premium = round(statistics.mean(premiums), 2) if premiums else None
    stdev_premium = round(statistics.stdev(premiums), 2) if len(premiums) > 1 else 0.0

    # ── Per-day average premium for trend ─────
    day_avg_premiums: List[Tuple[str, float]] = sorted(
        [(d, statistics.mean(v)) for d, v in daily_premiums.items() if v],
        key=lambda x: x[0],
    )

    # ── Trend (7-day slope via linear regression) ─────
    trend_direction = "STABLE"
    trend_slope_bps = 0.0
    recent = [x for x in day_avg_premiums if x[0] >= (date.today() - timedelta(days=_TREND_WINDOW_DAYS)).isoformat()]
    if len(recent) >= 2:
        ys = [x[1] for x in recent]
        xs = list(range(len(ys)))
        slope = _linear_slope(xs, ys)
        trend_slope_bps = round(slope, 3)
        if abs(slope) < 0.5:
            trend_direction = "STABLE"
        elif slope < 0:
            trend_direction = "TIGHTENING"   # premium shrinking → pricing more competitive
        else:
            trend_direction = "WIDENING"     # premium growing → getting more expensive

    # ── Bid-Ask spread (bps) ───────────
    spreads: List[float] = []
    for row in rows:
        br = row.get("bid_yield") or 0
        ar = row.get("ask_yield") or 0
        if br and ar and ar != br:
            spreads.append(abs(ar - br) * 10_000)
    avg_spread = round(statistics.mean(spreads), 2) if spreads else None

    # ── Recency factor ──────────────────
    days_since_last = (date.today() - _parse_date(last_date)).days if last_date else 999
    recency_score = max(0.0, 1.0 - days_since_last / _RECENCY_DECAY_DAYS)

    # ── Coverage score (log scale) ─────
    coverage_score = min(1.0, math.log1p(len(isins)) / math.log1p(200))

    # ── Consistency score (lower stdev = better) ─
    consistency_score = max(0.0, 1.0 - min(stdev_premium / 20.0, 1.0)) if stdev_premium else 1.0

    # ── Reliability = weighted blend ──
    reliability_score = round(
        (recency_score * 0.35 + coverage_score * 0.40 + consistency_score * 0.25) * 100, 1
    )

    # ── Pattern summary & tags ────────────────
    pattern_summary, tags = _generate_pattern(
        broker, avg_premium, avg_spread, trend_direction, trend_slope_bps,
        len(isins), days_since_last, stdev_premium, reliability_score,
    )

    return {
        "broker_name": broker,
        "total_isins_quoted": len(isins),
        "active_days": len(dates),
        "first_seen_date": first_date,
        "last_seen_date": last_date,
        "avg_ytm_premium_bps": avg_premium,
        "avg_spread_bps": avg_spread,
        "pricing_consistency": stdev_premium,
        "trend_direction": trend_direction,
        "trend_slope_bps": trend_slope_bps,
        "reliability_score": reliability_score,
        "pattern_summary": pattern_summary,
        "behaviour_tags": json.dumps(tags),
    }


def _generate_pattern(
    broker: str, avg_premium: Optional[float], avg_spread: Optional[float],
    trend: str, slope: float, n_isins: int, days_stale: int,
    stdev: float, reliability: float,
) -> Tuple[str, List[str]]:
    """Generate human-readable pattern summary and behaviour tags."""
    tags: List[str] = []
    parts: List[str] = []

    # ── Coverage tag
    if n_isins >= 100:
        tags.append("high_coverage")
        parts.append(f"Covers {n_isins} ISINs")
    elif n_isins >= 30:
        tags.append("medium_coverage")
        parts.append(f"Covers {n_isins} ISINs")
    else:
        tags.append("low_coverage")
        parts.append(f"Limited coverage ({n_isins} ISINs)")

    # ── Pricing bias tag
    if avg_premium is not None:
        if avg_premium > 10:
            tags.append("expensive_pricer")
            parts.append(f"quotes {abs(avg_premium):.0f}bps above peer average (expensive)")
        elif avg_premium > 3:
            tags.append("slightly_expensive")
            parts.append(f"quotes {avg_premium:.0f}bps above peers")
        elif avg_premium < -10:
            tags.append("aggressive_pricer")
            parts.append(f"quotes {abs(avg_premium):.0f}bps below peer average (competitive)")
        elif avg_premium < -3:
            tags.append("competitive_pricer")
            parts.append(f"quotes {abs(avg_premium):.0f}bps below peers (competitive)")
        else:
            tags.append("market_pricer")
            parts.append("prices closely track peer average")

    # ── Spread tag
    if avg_spread is not None:
        if avg_spread > 15:
            tags.append("wide_spreads")
            parts.append(f"wide bid-ask spread ({avg_spread:.0f}bps)")
        elif avg_spread < 5:
            tags.append("tight_spreads")
            parts.append(f"tight spreads ({avg_spread:.0f}bps)")

    # ── Trend tag
    if trend == "TIGHTENING":
        tags.append("improving_prices")
        parts.append(f"prices improving ({abs(slope):.1f}bps/day tightening)")
    elif trend == "WIDENING":
        tags.append("deteriorating_prices")
        parts.append(f"prices widening ({abs(slope):.1f}bps/day)")

    # ── Consistency tag
    if stdev < 3 and avg_premium is not None:
        tags.append("highly_consistent")
        parts.append("very consistent pricing")
    elif stdev > 15:
        tags.append("erratic_pricing")
        parts.append("pricing varies widely day-to-day")

    # ── Recency/staleness tag
    if days_stale > _STALE_DAYS:
        tags.append("stale_data")
        parts.append(f"last quoted {days_stale} days ago")

    summary = f"{broker}: " + ", ".join(parts) if parts else f"{broker}: insufficient data"
    return summary, tags


def _linear_slope(xs: List[float], ys: List[float]) -> float:
    """Compute OLS slope of ys vs xs."""
    n = len(xs)
    if n < 2:
        return 0.0
    sx = sum(xs); sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _parse_date(s: Optional[str]) -> date:
    if not s:
        return date.min
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return date.min


# ─────────────────────────────────────────────────────────────────────────────
# Same-day broker comparison for a specific ISIN
# ─────────────────────────────────────────────────────────────────────────────

async def get_broker_comparison_for_isin(isin: str, target_date: Optional[str] = None) -> Dict:
    """
    For a given ISIN return all broker quotes on a single date with ranking.
    Default date = today; falls back to latest date with quotes.
    """
    if not target_date:
        row = await fetch_all(
            "SELECT MAX(quote_date) as d FROM broker_daily_quotes WHERE isin = :isin AND ytm IS NOT NULL",
            {"isin": isin},
        )
        target_date = row[0]["d"] if row else date.today().isoformat()

    quotes = await fetch_all(
        """
        SELECT broker_name, clean_price, ytm, ytm_semi, ytm_screen,
               bid_price, ask_price, bid_yield, ask_yield,
               rating, quantity_lacs, lot_size, quote_date, source_format
        FROM broker_daily_quotes
        WHERE isin = :isin AND quote_date = :date
        ORDER BY ytm DESC
        """,
        {"isin": isin, "date": target_date},
    )
    if not quotes:
        return {"isin": isin, "date": target_date, "brokers": [], "best_ytm_broker": None}

    # Rank by YTM descending (higher yield = better for buyer)
    profiles = {p["broker_name"]: p for p in await fetch_all("SELECT * FROM broker_profiles")}

    ranked = []
    for i, q in enumerate(sorted(quotes, key=lambda x: (x.get("ytm") or 0), reverse=True)):
        prof = profiles.get(q["broker_name"], {})
        ranked.append({
            **q,
            "rank": i + 1,
            "is_best": i == 0,
            "reliability_score": prof.get("reliability_score"),
            "avg_ytm_premium_bps": prof.get("avg_ytm_premium_bps"),
            "trend_direction": prof.get("trend_direction"),
            "pattern_summary": prof.get("pattern_summary"),
            "behaviour_tags": json.loads(prof.get("behaviour_tags", "[]")) if prof.get("behaviour_tags") else [],
            "reason": _build_reason(q, prof, i == 0),
        })

    return {
        "isin": isin,
        "date": target_date,
        "brokers": ranked,
        "best_ytm_broker": ranked[0]["broker_name"] if ranked else None,
        "best_ytm": ranked[0].get("ytm") if ranked else None,
    }


def _build_reason(quote: Dict, profile: Dict, is_best: bool) -> str:
    """Generate a one-line recommendation reason for a specific broker quote."""
    broker = quote["broker_name"]
    ytm = quote.get("ytm")
    premium = profile.get("avg_ytm_premium_bps")
    trend = profile.get("trend_direction", "STABLE")
    tags = json.loads(profile.get("behaviour_tags", "[]")) if profile.get("behaviour_tags") else []

    reasons = []

    if is_best:
        reasons.append("Offers highest yield on this date")
    if premium is not None:
        if premium < -5:
            reasons.append(f"historically competitive — avg {abs(premium):.0f}bps below peers")
        elif premium > 10:
            reasons.append(f"historically expensive — avg {premium:.0f}bps above peers")
    if trend == "TIGHTENING":
        reasons.append("pricing has been tightening (improving) recently")
    elif trend == "WIDENING":
        reasons.append("spreads widening — monitor for better timing")
    if "highly_consistent" in tags:
        reasons.append("very consistent pricing — low execution risk")
    if "high_coverage" in tags:
        reasons.append(f"broad coverage ({profile.get('total_isins_quoted',0)} ISINs)")
    if "stale_data" in tags:
        reasons.append(f"⚠ last quoted {(date.today() - _parse_date(profile.get('last_seen_date'))).days}d ago")

    if not reasons:
        return f"{broker}: limited history — exercise judgement"
    return "; ".join(reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Main compute/refresh function (called after each ingest)
# ─────────────────────────────────────────────────────────────────────────────

async def refresh_broker_profiles() -> Dict:
    """
    Recompute broker_profiles for all brokers that have data.
    Also persists per-day premium rows into broker_daily_premiums.
    Called automatically after every PDF/Excel ingest.
    """
    brokers_row = await fetch_all(
        "SELECT DISTINCT broker_name FROM broker_daily_quotes WHERE ytm IS NOT NULL"
    )
    brokers = [r["broker_name"] for r in brokers_row]
    if not brokers:
        return {"updated": 0, "brokers": []}

    # Build peer averages for last 90 days
    start = (date.today() - timedelta(days=90)).isoformat()
    end = date.today().isoformat()
    peer_avg = await _build_peer_averages(start, end)

    updated = 0
    for broker in brokers:
        try:
            stats = await _compute_broker_stats(broker, peer_avg)
            if not stats:
                continue
            await execute_query(
                """
                INSERT INTO broker_profiles
                    (broker_name, total_isins_quoted, active_days, first_seen_date, last_seen_date,
                     avg_ytm_premium_bps, avg_spread_bps, pricing_consistency,
                     trend_direction, trend_slope_bps, reliability_score,
                     pattern_summary, behaviour_tags, last_computed)
                VALUES
                    (:broker_name, :total_isins_quoted, :active_days, :first_seen_date, :last_seen_date,
                     :avg_ytm_premium_bps, :avg_spread_bps, :pricing_consistency,
                     :trend_direction, :trend_slope_bps, :reliability_score,
                     :pattern_summary, :behaviour_tags, NOW())
                ON CONFLICT(broker_name) DO UPDATE SET
                    total_isins_quoted  = excluded.total_isins_quoted,
                    active_days         = excluded.active_days,
                    first_seen_date     = excluded.first_seen_date,
                    last_seen_date      = excluded.last_seen_date,
                    avg_ytm_premium_bps = excluded.avg_ytm_premium_bps,
                    avg_spread_bps      = excluded.avg_spread_bps,
                    pricing_consistency = excluded.pricing_consistency,
                    trend_direction     = excluded.trend_direction,
                    trend_slope_bps     = excluded.trend_slope_bps,
                    reliability_score   = excluded.reliability_score,
                    pattern_summary     = excluded.pattern_summary,
                    behaviour_tags      = excluded.behaviour_tags,
                    last_computed       = NOW()
                """,
                stats,
            )

            # ── Persist daily premium series ────────────────────────────────
            await _persist_daily_premiums(broker, peer_avg)

            updated += 1
            log.info("broker_profile_updated", broker=broker, score=stats.get("reliability_score"))
        except Exception as exc:
            log.warning("broker_profile_failed", broker=broker, error=str(exc))

    return {"updated": updated, "brokers": brokers}


async def _persist_daily_premiums(broker: str, peer_avg: Dict[str, Dict[str, float]]) -> None:
    """Upsert per-day premium averages into broker_daily_premiums for charting."""
    rows = await fetch_all(
        """
        SELECT isin, quote_date, ytm, bid_yield, ask_yield
        FROM broker_daily_quotes
        WHERE broker_name = :b AND ytm IS NOT NULL
        ORDER BY quote_date
        """,
        {"b": broker},
    )
    by_date: Dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["quote_date"], []).append(r)

    for qdate, day_rows in by_date.items():
        premiums: List[float] = []
        ytms: List[float] = []
        spreads: List[float] = []
        peer_ytms: List[float] = []

        for r in day_rows:
            ytm = float(r["ytm"])
            ytms.append(ytm)
            peer = peer_avg.get(qdate, {}).get(r["isin"])
            if peer and peer > 0:
                premiums.append((ytm - peer) * 10_000)
                peer_ytms.append(peer)
            by = float(r.get("bid_yield") or 0)
            ay = float(r.get("ask_yield") or 0)
            if by and ay and ay != by:
                spreads.append(abs(ay - by) * 10_000)

        avg_prem = round(statistics.mean(premiums), 3) if premiums else None
        avg_ytm  = round(statistics.mean(ytms), 4)    if ytms else None
        avg_sprd = round(statistics.mean(spreads), 3)  if spreads else None
        peer_ytm = round(statistics.mean(peer_ytms), 4) if peer_ytms else None

        try:
            await execute_query(
                """
                INSERT INTO broker_daily_premiums
                    (broker_name, quote_date, avg_premium_bps, num_isins, avg_ytm, avg_spread_bps, peer_avg_ytm)
                VALUES (:broker, :qdate, :prem, :n, :ytm, :sprd, :peer)
                ON CONFLICT (broker_name, quote_date) DO UPDATE SET
                    avg_premium_bps = excluded.avg_premium_bps,
                    num_isins       = excluded.num_isins,
                    avg_ytm         = excluded.avg_ytm,
                    avg_spread_bps  = excluded.avg_spread_bps,
                    peer_avg_ytm    = excluded.peer_avg_ytm,
                    computed_at     = NOW()
                """,
                {"broker": broker, "qdate": qdate, "prem": avg_prem,
                 "n": len(day_rows), "ytm": avg_ytm, "sprd": avg_sprd, "peer": peer_ytm},
            )
        except Exception as exc:
            log.warning("daily_premium_persist_fail", broker=broker, date=qdate, error=str(exc))


async def get_all_broker_profiles() -> List[Dict]:
    """Return all broker profiles sorted by reliability score."""
    return await fetch_all(
        """
        SELECT bp.*,
            (SELECT COUNT(*) FROM broker_daily_quotes bdq
             WHERE bdq.broker_name = bp.broker_name
               AND bdq.quote_date = date('now')) AS todays_quotes
        FROM broker_profiles bp
        ORDER BY reliability_score DESC
        """
    )
