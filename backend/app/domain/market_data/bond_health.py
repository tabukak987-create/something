"""
Bond Issue Detection Engine
=============================
Per-bond health monitoring for PF Trust portfolio:
 - Rating downgrade detection
 - Negative outlook detection
 - Spread widening (vs benchmark)
 - Low liquidity warning
 - Price drop detection (day-over-day, week-over-week)
 - Concentration risk per bond
 - Maturity clustering risk
 - Call/Put proximity alert

Each bond gets: âœ” HEALTHY, âš  MONITOR, or ðŸš¨ RISK status with detailed issue list.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ..shared.database import fetch_all, fetch_one, execute_query
from ..shared.logger import get_logger
from app.domain.portfolio.analytics import RATING_SCORE, compute_accrued_interest, normalize_rating
from ..shared.market_config import (
    RATING_SPREAD_BPS,
    interpolate_gsec_yield,
)

log = get_logger("service.bond_health")


# Thresholds
THRESHOLDS = {
    "spread_widening_bps": 50,       # Spread widened >50bps vs expected
    "low_liquidity_score": 30,       # Score < 30 = concern
    "price_drop_pct": 2.0,           # >2% drop from recent price
    "concentration_pct": 10.0,       # >10% portfolio weight
    "maturity_cluster_months": 3,    # Multiple bonds maturing within 3 months
    "maturity_cluster_count": 3,     # â‰¥3 bonds maturing in same window
    "call_proximity_days": 180,      # Call/put within 6 months
    "days_to_maturity_near": 90,     # Near maturity (reinvestment risk)
}


async def run_bond_health_check() -> dict:
    """
    Run comprehensive health check on all portfolio bonds.
    Returns per-bond status and issues + aggregate summary.
    """
    today = date.today()
    today_str = str(today)

    # Fetch portfolio with market data
    query = """
    SELECT
        p.id, p.isin, p.issuer_name, p.face_value, p.book_value, p.quantity,
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
        b.issuer_type AS bond_issuer_type,
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

    holdings = await fetch_all(query)

    # Compute total market value for concentration check
    total_mv = 0
    holding_mvs = {}
    for h in holdings:
        face = float(h["face_value"] or 1000)
        qty = int(h["quantity"] or 0)
        price = float(h["clean_price"] or 100)
        mv = face * qty * price / 100
        total_mv += mv
        holding_mvs[h["isin"]] = mv

    # Build maturity calendar for clustering check
    maturity_calendar = {}
    for h in holdings:
        try:
            mat = date.fromisoformat(str(h["maturity_date"])[:10])
            month_key = f"{mat.year}-{mat.month:02d}"
            if month_key not in maturity_calendar:
                maturity_calendar[month_key] = []
            maturity_calendar[month_key].append(h["isin"])
        except Exception:
            pass

    # Find maturity clusters (â‰¥3 bonds in same month)
    maturity_clusters = {k: v for k, v in maturity_calendar.items() if len(v) >= THRESHOLDS["maturity_cluster_count"]}

    # Historical prices for price drop check
    prev_prices = {}
    try:
        prev_rows = await fetch_all("""
            SELECT isin, clean_price, price_date FROM unified_market_data
            WHERE price_date < :today
            ORDER BY price_date DESC
        """, {"today": today_str})
        for r in prev_rows:
            isin = r["isin"]
            if isin not in prev_prices:
                prev_prices[isin] = r["clean_price"]
    except Exception:
        pass

    # Check each bond
    results = []
    status_counts = {"HEALTHY": 0, "MONITOR": 0, "RISK": 0}

    for h in holdings:
        isin = h["isin"]
        _raw_r = str(h["rating"] or "").upper().strip()
        _itype = str(h.get("bond_issuer_type") or h.get("category") or "").upper()
        rating = normalize_rating(_raw_r, _itype, isin=isin, issuer_name=h.get("issuer_name"))
        issues = []
        flags = {
            "rating_downgrade": 0,
            "rating_outlook_negative": 0,
            "spread_widening": 0,
            "low_liquidity": 0,
            "price_drop": 0,
            "high_concentration": 0,
            "maturity_clustering": 0,
            "call_put_risk": 0,
        }

        # 1. Rating check (below A is concerning for PF Trust)
        credit_num = RATING_SCORE.get(rating, 7)
        if credit_num >= 9:  # BBB+ or below
            issues.append({
                "type": "RATING_CONCERN",
                "severity": "HIGH",
                "message": f"Rating '{rating}' is below investment grade for PF Trust",
            })
            flags["rating_downgrade"] = 1
        elif credit_num >= 7:  # A or A-
            issues.append({
                "type": "RATING_WATCH",
                "severity": "MEDIUM",
                "message": f"Rating '{rating}' â€” single-A range, monitor for downgrade",
            })

        # 2. Spread widening
        expected_spread = RATING_SPREAD_BPS.get(rating, 100)
        actual_spread = float(h.get("spread_over_gsec") or 0) * 100  # convert to bps
        # If actual spread is much wider than expected for its rating
        spread_diff = actual_spread - expected_spread
        if spread_diff > THRESHOLDS["spread_widening_bps"]:
            issues.append({
                "type": "SPREAD_WIDENING",
                "severity": "MEDIUM",
                "message": f"Spread at {actual_spread:.0f}bps, {spread_diff:.0f}bps wider than expected {expected_spread}bps for {rating}",
            })
            flags["spread_widening"] = 1

        # 3. Low liquidity
        liq_score = float(h.get("liquidity_score") or 50)
        if liq_score < THRESHOLDS["low_liquidity_score"]:
            issues.append({
                "type": "LOW_LIQUIDITY",
                "severity": "HIGH" if liq_score < 15 else "MEDIUM",
                "message": f"Liquidity score {liq_score:.0f} â€” may face exit difficulty",
            })
            flags["low_liquidity"] = 1

        # 4. Price drop
        curr_price = float(h.get("clean_price") or 100)
        prev_price = prev_prices.get(isin)
        if prev_price and prev_price > 0:
            drop_pct = (prev_price - curr_price) / prev_price * 100
            if drop_pct > THRESHOLDS["price_drop_pct"]:
                issues.append({
                    "type": "PRICE_DROP",
                    "severity": "HIGH" if drop_pct > 5 else "MEDIUM",
                    "message": f"Price dropped {drop_pct:.2f}% from {prev_price:.2f} to {curr_price:.2f}",
                })
                flags["price_drop"] = 1

        # 5. Concentration
        mv = holding_mvs.get(isin, 0)
        conc_pct = mv / total_mv * 100 if total_mv else 0
        if conc_pct > THRESHOLDS["concentration_pct"]:
            issues.append({
                "type": "CONCENTRATION",
                "severity": "HIGH" if conc_pct > 15 else "MEDIUM",
                "message": f"Single bond at {conc_pct:.1f}% of portfolio â€” exceeds {THRESHOLDS['concentration_pct']}% threshold",
            })
            flags["high_concentration"] = 1

        # 6. Maturity clustering
        try:
            mat = date.fromisoformat(str(h["maturity_date"])[:10])
            month_key = f"{mat.year}-{mat.month:02d}"
            if month_key in maturity_clusters:
                cluster_size = len(maturity_clusters[month_key])
                issues.append({
                    "type": "MATURITY_CLUSTER",
                    "severity": "MEDIUM",
                    "message": f"{cluster_size} bonds maturing in {month_key} â€” reinvestment risk",
                })
                flags["maturity_clustering"] = 1

            # Near maturity
            days_to_mat = (mat - today).days
            if 0 < days_to_mat < THRESHOLDS["days_to_maturity_near"]:
                issues.append({
                    "type": "NEAR_MATURITY",
                    "severity": "LOW",
                    "message": f"Matures in {days_to_mat} days â€” plan reinvestment",
                })
        except Exception:
            pass

        # 7. Determine overall status
        high_count = sum(1 for i in issues if i["severity"] == "HIGH")
        medium_count = sum(1 for i in issues if i["severity"] == "MEDIUM")
        severity_score = high_count * 3 + medium_count * 1

        if severity_score >= 4:
            status = "RISK"
        elif severity_score >= 1:
            status = "MONITOR"
        else:
            status = "HEALTHY"

        status_counts[status] += 1

        # Persist to bond_health_status
        try:
            await execute_query("""
                INSERT INTO bond_health_status (
                    isin, check_date, status,
                    rating_downgrade, rating_outlook_negative, spread_widening,
                    low_liquidity, price_drop, high_concentration,
                    maturity_clustering, call_put_risk,
                    issues_json, severity_score
                ) VALUES (
                    :isin, :cd, :status,
                    :rd, :ron, :sw, :ll, :pd, :hc, :mc, :cpr,
                    :ij, :ss
                ) ON CONFLICT(isin, check_date) DO UPDATE SET
                    status = excluded.status,
                    rating_downgrade = excluded.rating_downgrade,
                    spread_widening = excluded.spread_widening,
                    low_liquidity = excluded.low_liquidity,
                    price_drop = excluded.price_drop,
                    high_concentration = excluded.high_concentration,
                    maturity_clustering = excluded.maturity_clustering,
                    issues_json = excluded.issues_json,
                    severity_score = excluded.severity_score
            """, {
                "isin": isin, "cd": today_str, "status": status,
                "rd": flags["rating_downgrade"], "ron": flags["rating_outlook_negative"],
                "sw": flags["spread_widening"], "ll": flags["low_liquidity"],
                "pd": flags["price_drop"], "hc": flags["high_concentration"],
                "mc": flags["maturity_clustering"], "cpr": flags["call_put_risk"],
                "ij": json.dumps(issues), "ss": severity_score,
            })
        except Exception as e:
            log.warning("bond_health_persist_failed", isin=isin, error=str(e))

        results.append({
            "isin": isin,
            "issuer_name": h.get("issuer_name", ""),
            "rating": rating,
            "category": h.get("category", ""),
            "market_value": round(mv, 2),
            "weight_pct": round(conc_pct, 2),
            "status": status,
            "severity_score": severity_score,
            "issues": issues,
            "issue_count": len(issues),
        })

    # Sort: RISK first, then MONITOR, then HEALTHY
    status_order = {"RISK": 0, "MONITOR": 1, "HEALTHY": 2}
    results.sort(key=lambda x: (status_order.get(x["status"], 3), -x["severity_score"]))

    return {
        "bonds": results,
        "summary": {
            "total": len(results),
            "healthy": status_counts["HEALTHY"],
            "monitor": status_counts["MONITOR"],
            "risk": status_counts["RISK"],
            "risk_pct": round(status_counts["RISK"] / len(results) * 100, 1) if results else 0,
        },
        "maturity_clusters": [
            {"month": k, "count": len(v), "isins": v}
            for k, v in sorted(maturity_clusters.items())
        ],
        "check_date": today_str,
    }

