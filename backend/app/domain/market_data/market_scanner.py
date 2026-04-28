"""Bond market scanner combining database, market feed, and broker inventory."""

from __future__ import annotations

from typing import Any

from ..shared.database import fetch_all


async def scan_tradable_bonds(limit: int = 500) -> list[dict[str, Any]]:
    rows = await fetch_all(
        """
        SELECT
            b.isin,
            b.bond_name,
            b.issuer_name,
            b.issuer_type,
            b.credit_rating,
            b.sector,
            b.maturity_date,
            b.is_pf_eligible,
            b.listing_exchange,
            m.ytm,
            m.modified_duration,
            m.liquidity_score,
            m.traded_volume,
            m.clean_price,
            q.best_bid_yield,
            q.best_ask_yield,
            q.broker_count
        FROM universal_bond_master b
        LEFT JOIN (
            SELECT umd1.*
            FROM unified_market_data umd1
            INNER JOIN (
                SELECT isin, MAX(price_date) AS max_date
                FROM unified_market_data
                GROUP BY isin
            ) umd2 ON umd1.isin = umd2.isin AND umd1.price_date = umd2.max_date
        ) m ON b.isin = m.isin
        LEFT JOIN (
            SELECT
                isin,
                MAX(bid_yield) AS best_bid_yield,
                MIN(ask_yield) AS best_ask_yield,
                COUNT(DISTINCT broker_name) AS broker_count
            FROM broker_daily_quotes
            GROUP BY isin
        ) q ON b.isin = q.isin
        WHERE b.is_active = 1
        ORDER BY COALESCE(m.liquidity_score, 0) DESC, COALESCE(m.ytm, 0) DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )

    tradable = []
    for row in rows:
        has_market = row.get("ytm") is not None
        has_broker = row.get("broker_count") is not None and int(row.get("broker_count") or 0) > 0
        if has_market or has_broker:
            row["tradable"] = True
            tradable.append(row)
    return tradable
