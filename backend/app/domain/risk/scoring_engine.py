"""
Enhanced Scoring Engine — Unified Gateway
Institutional Suitability Score (ISS) with 5 dimensions:
  1. Value Score (25%)     — Spread vs rating peers, YTM attractiveness
  2. Liquidity Score (15%) — Volume, bid-ask, exchange listing
  3. Credit Score (15%)    — Rating quality, outlook, momentum
  4. ALM Fit Score (25%)   — Maturity match to PF liability curve
  5. Broker Score (20%)    — Broker quote discount vs market, arbitrage
"""

import math
from datetime import date
from typing import List, Dict, Any, Optional
from ..shared.database import fetch_all, fetch_one, execute_query
from ..shared.logger import get_logger

log = get_logger("enhanced.scoring")

# ─── Weights ──────────────────────────────────────────────────────────────────
WEIGHTS = {
    "value": 0.25,
    "liquidity": 0.15,
    "credit": 0.15,
    "alm_fit": 0.25,
    "broker": 0.20,
}

# ─── Rating Hierarchy ─────────────────────────────────────────────────────────
# SOVEREIGN is above AAA — used for G-Sec, T-Bills, SDLs (government bonds)
RATING_RANK = {
    "SOVEREIGN": 0, "SOV": 0, "G-SEC": 0, "GSEC": 0,  # Government / Sovereign
    "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7, "BBB+": 8, "BBB": 9,
}

# ─── Typical Credit Spreads (bps) by rating for value comparison ──────────────
# Sovereign = 0 bps (the risk-free benchmark itself)
TYPICAL_SPREAD_BPS = {
    "SOVEREIGN": 0, "SOV": 0, "G-SEC": 0, "GSEC": 0,
    "AAA": 45, "AA+": 75, "AA": 110, "AA-": 140,
    "A+": 180, "A": 220, "A-": 270, "BBB+": 340, "BBB": 420,
}


def _normalize_rating(raw: str | None, issuer_type: str | None = None) -> str:
    """Canonical rating string. Government issuer_types resolve to SOVEREIGN."""
    if not raw:
        if issuer_type and issuer_type.upper() in ("G-SEC", "GSEC", "T-BILL", "TBILL", "SDL", "CENTRAL-GOVT", "STATE-GOVT"):
            return "SOVEREIGN"
        return "UNRATED"
    r = raw.upper().strip()
    return "SOVEREIGN" if r in ("SOV", "G-SEC", "GSEC") else r

# ─── PF liability profile defaults ───────────────────────────────────────────
PF_TARGET_DURATION = 5.5
PF_OPTIMAL_MATURITY_MIN = 3.0
PF_OPTIMAL_MATURITY_MAX = 12.0


class UnifiedScorer:
    """
    Institutional suitability scoring for the Unified Bond Gateway.
    5-dimension composite score for 20,000+ bonds.
    """

    async def score_all(self, limit: int = 2000) -> Dict[str, Any]:
        """Score bonds that have recent market data."""
        log.info("scoring_started", limit=limit)

        bonds = await fetch_all(
            """
            SELECT b.isin, b.issuer_name, b.issuer_type, b.credit_rating,
                   b.maturity_date, b.coupon_rate, b.face_value, b.bond_name,
                   b.coupon_type, b.coupon_frequency, b.is_pf_eligible,
                   b.listing_exchange,
                   m.ytm, m.clean_price, m.spread_over_gsec, m.liquidity_score as mkt_liq,
                   m.modified_duration, m.dv01, m.bid_yield, m.ask_yield,
                   m.traded_volume, m.data_source
            FROM   universal_bond_master b
            JOIN   unified_market_data   m ON b.isin = m.isin
            WHERE  m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = b.isin)
              AND  b.is_active = 1
            ORDER  BY m.ytm DESC
            LIMIT  :limit
            """,
            {"limit": limit}
        )

        if not bonds:
            log.warn("no_bonds_for_scoring")
            return {"count": 0, "top_picks": []}

        scored_count = 0
        results = []
        today = date.today().isoformat()

        for bond in bonds:
            isin = bond["isin"]
            try:
                # Calculate all 5 sub-scores
                vs = self._calc_value_score(bond)
                ls = self._calc_liquidity_score(bond)
                cs = self._calc_credit_score(bond)
                alm = self._calc_alm_fit_score(bond)
                bs = await self._calc_broker_score(bond)

                # Composite ISS
                iss_score = (
                    vs * WEIGHTS["value"] +
                    ls * WEIGHTS["liquidity"] +
                    cs * WEIGHTS["credit"] +
                    alm * WEIGHTS["alm_fit"] +
                    bs * WEIGHTS["broker"]
                )
                iss_score = min(100, max(0, iss_score))

                recommendation = self._get_recommendation(iss_score)
                explanation = self._generate_explanation(bond, iss_score, recommendation, vs, ls, cs, alm, bs)

                # Upsert into unified_bond_scores
                await execute_query(
                    """
                    INSERT INTO unified_bond_scores (
                        isin, score_date, value_score, liquidity_score, credit_score,
                        alm_fit_score, broker_score,
                        iss_score, recommendation, overall_explanation
                    ) VALUES (:isin, :score_date, :v, :l, :c, :alm, :bs, :iss, :rec, :exp)
                    ON CONFLICT(isin, score_date) DO UPDATE SET
                        value_score = excluded.value_score,
                        liquidity_score = excluded.liquidity_score,
                        credit_score = excluded.credit_score,
                        alm_fit_score = excluded.alm_fit_score,
                        broker_score = excluded.broker_score,
                        iss_score = excluded.iss_score,
                        recommendation = excluded.recommendation,
                        overall_explanation = excluded.overall_explanation
                    """,
                    {
                        "isin": isin, "score_date": today,
                        "v": round(vs, 1), "l": round(ls, 1), "c": round(cs, 1),
                        "alm": round(alm, 1), "bs": round(bs, 1),
                        "iss": round(iss_score, 1), "rec": recommendation, "exp": explanation
                    }
                )
                scored_count += 1
                results.append({
                    **bond, "iss_score": round(iss_score, 1), "recommendation": recommendation,
                    "value_score": round(vs, 1), "liquidity_score": round(ls, 1),
                    "credit_score": round(cs, 1), "alm_fit_score": round(alm, 1),
                    "broker_score": round(bs, 1),
                })
            except Exception as exc:
                log.warning("scoring_bond_failed", isin=isin, error=str(exc))

        log.info("scoring_complete", count=scored_count)
        results.sort(key=lambda x: x["iss_score"], reverse=True)

        # Assign ranks
        for i, r in enumerate(results, 1):
            r["iss_rank"] = i

        return {
            "count": scored_count,
            "top_picks": results[:20],
            "summary": {
                "strong_buy": sum(1 for r in results if r["recommendation"] == "STRONG_BUY"),
                "buy": sum(1 for r in results if r["recommendation"] == "BUY"),
                "hold": sum(1 for r in results if r["recommendation"] == "HOLD"),
                "sell": sum(1 for r in results if r["recommendation"] == "SELL"),
                "avoid": sum(1 for r in results if r["recommendation"] == "AVOID"),
            }
        }

    # ─── Sub-Score Calculators ────────────────────────────────────────────────

    def _calc_value_score(self, bond: dict) -> float:
        """Value: spread vs peers, YTM attractiveness, accrued interest advantage."""
        score = 50.0
        rating = _normalize_rating(bond.get("credit_rating"), bond.get("issuer_type"))
        ytm = bond.get("ytm") or 0
        spread = bond.get("spread_over_gsec") or 0
        issuer_type = (bond.get("issuer_type") or "").upper()

        # Spread vs peers (0-40)
        typical = TYPICAL_SPREAD_BPS.get(rating, 100)
        actual_bps = spread * 100 if spread < 10 else spread  # handle both % and bps
        premium = actual_bps - typical

        if premium > 30:
            score += 35
        elif premium > 15:
            score += 25
        elif premium > 0:
            score += 15
        elif premium > -15:
            score += 5

        # YTM attractiveness (0-25)
        if issuer_type in ("G-SEC", "T-BILL"):
            if ytm > 7.2: score += 15
            elif ytm > 7.0: score += 10
        else:
            if ytm > 8.5: score += 25
            elif ytm > 8.0: score += 20
            elif ytm > 7.5: score += 15
            elif ytm > 7.0: score += 10

        # Coupon premium (0-10)
        coupon = bond.get("coupon_rate") or 0
        if coupon > 8.5: score += 10
        elif coupon > 7.5: score += 5

        return min(100, max(0, score))

    def _calc_liquidity_score(self, bond: dict) -> float:
        """Liquidity: volume, bid-ask, exchange listing, issuer type."""
        liq = bond.get("mkt_liq") or 0
        issuer_type = (bond.get("issuer_type") or "").upper()
        exchange = (bond.get("listing_exchange") or "").upper()
        volume = bond.get("traded_volume") or 0

        score = max(liq, 30.0)  # Base from market data engine

        # Issuer type boost
        if issuer_type == "G-SEC":
            score = max(score, 90)
        elif issuer_type in ("T-BILL",):
            score = max(score, 85)
        elif issuer_type == "SDL":
            score = max(score, 65)
        elif issuer_type == "PSU":
            score = max(score, 55)

        # Exchange listing boost
        if exchange in ("BOTH", "NSE_BSE"):
            score = min(100, score + 10)
        elif exchange in ("NSE", "BSE"):
            score = min(100, score + 5)

        # Volume boost
        if volume > 100:
            score = min(100, score + 10)
        elif volume > 10:
            score = min(100, score + 5)

        # Bid-ask tightness
        bid = bond.get("bid_yield") or 0
        ask = bond.get("ask_yield") or 0
        if bid > 0 and ask > 0:
            spread_bps = abs(ask - bid) * 100
            if spread_bps < 5:
                score = min(100, score + 10)
            elif spread_bps < 15:
                score = min(100, score + 5)
            elif spread_bps > 50:
                score = max(0, score - 10)

        return min(100, max(0, score))

    def _calc_credit_score(self, bond: dict) -> float:
        """Credit: rating quality, outlook, momentum, issuer type."""
        rating = _normalize_rating(bond.get("credit_rating"), bond.get("issuer_type"))
        issuer_type = (bond.get("issuer_type") or "").upper()
        outlook = (bond.get("rating_outlook") or "STABLE").upper()

        # Base from rating
        rank = RATING_RANK.get(rating, 8)
        if rank == 0:
            score = 98.0  # Sovereign
        elif rank <= 1:
            score = 92.0  # AAA
        elif rank <= 2:
            score = 85.0  # AA+
        elif rank <= 3:
            score = 78.0  # AA
        elif rank <= 4:
            score = 72.0  # AA-
        elif rank <= 5:
            score = 62.0  # A+
        elif rank <= 6:
            score = 55.0  # A
        else:
            score = 40.0  # Below A

        # Outlook adjustment
        if outlook == "POSITIVE":
            score = min(100, score + 8)
        elif outlook == "WATCH_POSITIVE":
            score = min(100, score + 5)
        elif outlook == "NEGATIVE":
            score = max(0, score - 12)
        elif outlook == "WATCH_NEGATIVE":
            score = max(0, score - 20)

        # Issuer type bonus
        if issuer_type == "PSU":
            score = min(100, score + 5)

        # PF eligibility bonus
        if bond.get("is_pf_eligible"):
            score = min(100, score + 3)

        return min(100, max(0, score))

    def _calc_alm_fit_score(self, bond: dict) -> float:
        """ALM Fit: maturity match, duration alignment, coupon frequency, cash flow."""
        score = 50.0

        # Maturity match (0-40)
        try:
            mat = date.fromisoformat(str(bond.get("maturity_date", "")))
            years = max(0, (mat - date.today()).days / 365.25)
        except Exception:
            years = 5.0

        if PF_OPTIMAL_MATURITY_MIN <= years <= PF_OPTIMAL_MATURITY_MAX:
            score += 35
        elif years < PF_OPTIMAL_MATURITY_MIN:
            gap = PF_OPTIMAL_MATURITY_MIN - years
            score += max(0, 25 - gap * 8)
        else:
            gap = years - PF_OPTIMAL_MATURITY_MAX
            score += max(0, 25 - gap * 3)

        # Duration alignment (0-25)
        mod_dur = bond.get("modified_duration") or 0
        if mod_dur > 0:
            dur_diff = abs(mod_dur - PF_TARGET_DURATION)
            if dur_diff < 1:
                score += 25
            elif dur_diff < 2:
                score += 18
            elif dur_diff < 3:
                score += 12
            elif dur_diff < 5:
                score += 6
            # Very long duration penalty
            if mod_dur > 15:
                score -= 10

        # Coupon frequency (0-15)
        freq = bond.get("coupon_frequency") or 2
        coupon = bond.get("coupon_rate") or 0
        if coupon > 0:
            if freq >= 4:
                score += 15  # Quarterly
            elif freq >= 2:
                score += 12  # Semi-annual
            elif freq >= 1:
                score += 8   # Annual
        else:
            score += 3  # Zero coupon — less ALM friendly

        # Coupon income stream (0-10)
        if coupon >= 8.0:
            score += 10
        elif coupon >= 7.0:
            score += 7
        elif coupon >= 6.0:
            score += 4

        return min(100, max(0, score))

    async def _calc_broker_score(self, bond: dict) -> float:
        """Broker opportunity: quote discount vs market, competition, arbitrage."""
        isin = bond["isin"]
        score = 40.0  # Base — no broker quotes means just market order

        # Check for broker quotes
        quotes = await fetch_all(
            """SELECT * FROM unified_broker_quotes
               WHERE isin = :isin
               ORDER BY quote_date DESC LIMIT 10""",
            {"isin": isin},
        )

        if not quotes:
            return score

        # Number of quotes boost
        score += min(15, len(quotes) * 3)

        # Check for discount vs market price
        market_price = bond.get("clean_price") or 0
        if market_price > 0:
            for q in quotes:
                ask = q.get("ask_price") or 0
                if ask > 0 and ask < market_price:
                    discount_pct = (market_price - ask) / market_price * 100
                    if discount_pct > 0.5:
                        score += 25  # Strong broker discount
                    elif discount_pct > 0.2:
                        score += 15
                    elif discount_pct > 0:
                        score += 8
                    break  # Best quote

        # Broker competition
        unique_brokers = set(q.get("broker_name", "") for q in quotes)
        if len(unique_brokers) >= 3:
            score += 10
        elif len(unique_brokers) >= 2:
            score += 5

        return min(100, max(0, score))

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _get_recommendation(self, iss: float) -> str:
        if iss >= 82: return "STRONG_BUY"
        if iss >= 70: return "BUY"
        if iss >= 50: return "HOLD"
        if iss >= 35: return "SELL"
        return "AVOID"

    def _generate_explanation(
        self, bond: dict, iss: float, rec: str,
        vs: float, ls: float, cs: float, alm: float, bs: float
    ) -> str:
        parts = []
        ytm = bond.get("ytm") or 0
        spread = bond.get("spread_over_gsec") or 0
        rating = _normalize_rating(bond.get("credit_rating"), bond.get("issuer_type"))
        # Display label: SOVEREIGN for gov bonds, Unrated for uncovered corporates
        rating_display = "Sovereign" if rating == "SOVEREIGN" else (rating if rating != "UNRATED" else "Unrated")
        issuer_type = (bond.get("issuer_type") or "").upper()

        if ytm > 0:
            parts.append(f"YTM {ytm:.2f}%")
        if spread > 0:
            parts.append(f"spread {spread:.2f}% over G-Sec")

        # Highlight strongest dimension
        scores = {"Value": vs, "Liquidity": ls, "Credit": cs, "ALM_Fit": alm, "Broker": bs}
        best = max(scores, key=scores.get)
        parts.append(f"strongest: {best} ({scores[best]:.0f}/100)")

        if issuer_type in ("G-SEC", "T-BILL"):
            parts.append("sovereign safety with benchmark liquidity")
        elif issuer_type == "SDL":
            parts.append("state government backing")
        elif issuer_type == "PSU":
            parts.append("PSU with implicit govt support")

        if rec == "STRONG_BUY":
            parts.append("attractive across all dimensions")
        elif rec == "AVOID":
            worst = min(scores, key=scores.get)
            parts.append(f"weak {worst} ({scores[worst]:.0f}/100)")

        return ". ".join(parts) + "."
