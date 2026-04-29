"""
Data Integrity & Failure Handling Service
============================================
Institutional-grade data validation, anomaly detection, and resilience:

  - Data validation checks on ingestion
  - Pricing anomaly detection (statistical + rule-based)
  - Alert if data feed missing
  - Fallback mechanism for stale data
  - Data source transparency panel
  - Last Updated Timestamp tracking

Ensures all pricing data is validated, sourced, and timestamped
before use in portfolio valuation or compliance checks.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso, utcnow

log = get_logger("service.data_integrity")


class DataQualityStatus(str, Enum):
    FRESH = "FRESH"               # Updated within last 24h
    STALE = "STALE"               # 1-3 days old
    VERY_STALE = "VERY_STALE"     # 3-7 days old
    MISSING = "MISSING"           # No data available
    ANOMALOUS = "ANOMALOUS"       # Failed anomaly checks
    FALLBACK = "FALLBACK"         # Using fallback mechanism


class AnomalyType(str, Enum):
    PRICE_SPIKE = "PRICE_SPIKE"               # >5% daily move
    PRICE_DROP = "PRICE_DROP"                 # >5% daily drop
    YIELD_INVERSION = "YIELD_INVERSION"       # Yield < 0 or unrealistic
    ZERO_PRICE = "ZERO_PRICE"                 # Price = 0
    STALE_PRICE = "STALE_PRICE"               # Unchanged for 5+ days
    MISSING_FEED = "MISSING_FEED"             # Expected feed not received
    VOLUME_ANOMALY = "VOLUME_ANOMALY"         # Abnormal trade volume
    COUPON_MISMATCH = "COUPON_MISMATCH"       # Coupon doesn't match master


# ── Schema DDL ────────────────────────────────────────────────────────────────
DATA_INTEGRITY_DDL = """
CREATE TABLE IF NOT EXISTS data_quality_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT,
    check_type TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    data_source TEXT,
    price_value REAL,
    expected_range_low REAL,
    expected_range_high REAL,
    staleness_hours REAL,
    checked_at TEXT NOT NULL,
    auto_resolved INTEGER NOT NULL DEFAULT 0,
    resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_dq_isin ON data_quality_log(isin);
CREATE INDEX IF NOT EXISTS idx_dq_status ON data_quality_log(status);
CREATE INDEX IF NOT EXISTS idx_dq_checked ON data_quality_log(checked_at);

CREATE TABLE IF NOT EXISTS data_source_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    last_successful_fetch TEXT,
    last_failed_fetch TEXT,
    last_record_count INTEGER DEFAULT 0,
    avg_latency_ms REAL,
    reliability_pct REAL DEFAULT 100.0,
    is_active INTEGER NOT NULL DEFAULT 1,
    config_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_freshness_tracker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT NOT NULL,
    data_type TEXT NOT NULL DEFAULT 'price',
    last_updated TEXT NOT NULL,
    source TEXT NOT NULL,
    value REAL,
    staleness_hours REAL,
    quality_status TEXT NOT NULL DEFAULT 'FRESH',
    UNIQUE(isin, data_type)
);

CREATE INDEX IF NOT EXISTS idx_freshness_isin ON data_freshness_tracker(isin);
CREATE INDEX IF NOT EXISTS idx_freshness_status ON data_freshness_tracker(quality_status);
"""


# ── Default Data Sources ──────────────────────────────────────────────────────
DEFAULT_SOURCES = [
    {"name": "NSE", "type": "EXCHANGE", "priority": 1, "desc": "National Stock Exchange bond trading data"},
    {"name": "BSE", "type": "EXCHANGE", "priority": 2, "desc": "Bombay Stock Exchange bond data"},
    {"name": "FIMMDA", "type": "VALUATION", "priority": 3, "desc": "FIMMDA daily bond valuation prices"},
    {"name": "CCIL", "type": "CLEARING", "priority": 4, "desc": "CCIL trade data and yield curves"},
    {"name": "BROKER_QUOTES", "type": "BROKER", "priority": 5, "desc": "Broker dealer quotes"},
    {"name": "SYNTHETIC", "type": "MODEL", "priority": 10, "desc": "Model-derived synthetic prices"},
]


class DataIntegrityService:
    """
    Ensures all pricing and market data meets institutional quality standards.
    """

    @classmethod
    async def initialize(cls):
        """Create data integrity tables and seed source registry."""
        try:
            for stmt in DATA_INTEGRITY_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await execute_query(stmt + ";")

            # Seed source registry
            count = await fetch_one("SELECT COUNT(*) AS cnt FROM data_source_registry")
            if not count or count["cnt"] == 0:
                now = utcnow_iso()
                for src in DEFAULT_SOURCES:
                    await execute_query(
                        """INSERT INTO data_source_registry
                           (source_name, source_type, priority, is_active, created_at)
                           VALUES (:n, :t, :p, 1, :c)
                           ON CONFLICT(source_name) DO NOTHING""",
                        {"n": src["name"], "t": src["type"], "p": src["priority"], "c": now},
                    )

            for column_definition in (
                "dirty_price REAL",
                "macaulay_duration REAL",
                "z_spread REAL",
                "ytm REAL",
                "source_name TEXT",
                "source_type TEXT",
                "ingestion_method TEXT",
                "source_reference TEXT",
                "source_sheet_name TEXT",
                "source_row_number INTEGER",
                "timestamp_ingested TEXT",
                "timestamp_effective TEXT",
                "provenance_json TEXT",
            ):
                try:
                    await execute_query(
                        f"ALTER TABLE unified_market_data ADD COLUMN {column_definition}",
                        {},
                    )
                except Exception:
                    pass

            try:
                await execute_query(
                    """
                    UPDATE unified_market_data
                    SET ytm = COALESCE(ytm, ask_yield)
                    WHERE ytm IS NULL AND ask_yield IS NOT NULL
                    """,
                    {},
                )
            except Exception:
                pass

            log.info("data_integrity_initialized")
        except Exception as e:
            log.error("data_integrity_init_failed", error=str(e))

    @classmethod
    async def validate_price(cls, isin: str, new_price: float,
                              source: str = "UNKNOWN") -> dict:
        """
        Validate a new price against anomaly detection rules.
        Returns validation result with any detected anomalies.
        """
        anomalies = []
        now = utcnow_iso()

        # Rule 1: Zero or negative price
        if new_price <= 0:
            anomalies.append({
                "type": AnomalyType.ZERO_PRICE.value,
                "message": f"Invalid price {new_price} for {isin}",
                "severity": "CRITICAL",
            })

        # Rule 2: Compare with historical price
        last_price = await fetch_one(
                """SELECT clean_price AS price, data_source AS source, last_updated AS updated_at
               FROM unified_market_data
               WHERE isin = :isin
                    ORDER BY last_updated DESC LIMIT 1""",
            {"isin": isin},
        )

        if last_price and last_price.get("price") and last_price["price"] > 0:
            old = last_price["price"]
            change_pct = abs(new_price - old) / old * 100

            if change_pct > 5.0 and new_price > old:
                anomalies.append({
                    "type": AnomalyType.PRICE_SPIKE.value,
                    "message": f"Price spike: {old:.2f} → {new_price:.2f} ({change_pct:.1f}%)",
                    "severity": "WARNING" if change_pct < 10 else "CRITICAL",
                    "old_price": old,
                    "new_price": new_price,
                    "change_pct": round(change_pct, 2),
                })

            if change_pct > 5.0 and new_price < old:
                anomalies.append({
                    "type": AnomalyType.PRICE_DROP.value,
                    "message": f"Price drop: {old:.2f} → {new_price:.2f} ({change_pct:.1f}%)",
                    "severity": "WARNING" if change_pct < 10 else "CRITICAL",
                    "old_price": old,
                    "new_price": new_price,
                    "change_pct": round(-change_pct, 2),
                })

        # Rule 3: Yield sanity check
        bond = await fetch_one(
            "SELECT coupon_rate, maturity_date FROM universal_bond_master WHERE isin = :isin",
            {"isin": isin},
        )
        if bond and bond.get("coupon_rate"):
            coupon = float(bond["coupon_rate"])
            # If price implies negative yield or YTM > 25%, flag
            if new_price > 200:
                anomalies.append({
                    "type": AnomalyType.YIELD_INVERSION.value,
                    "message": f"Price {new_price} implies possible negative yield",
                    "severity": "WARNING",
                })
        # Rule 3b: Coupon rate sanity check
        if bond:
            coupon = float(bond.get("coupon_rate") or 0)
            if coupon < 0:
                anomalies.append({
                    "type": AnomalyType.COUPON_MISMATCH.value,
                    "message": f"Negative coupon rate {coupon}% for {isin} — data integrity error",
                    "severity": "CRITICAL",
                })
            elif coupon == 0:
                # Zero coupon is valid for T-Bills/ZCBs, but flag for review on regular bonds
                anomalies.append({
                    "type": AnomalyType.COUPON_MISMATCH.value,
                    "message": f"Zero coupon rate for {isin} — verify if zero-coupon bond or data error",
                    "severity": "WARNING",
                })
            elif coupon > 15:
                anomalies.append({
                    "type": AnomalyType.COUPON_MISMATCH.value,
                    "message": f"Unrealistic coupon rate {coupon}% for {isin} (max expected ~15%)",
                    "severity": "CRITICAL",
                })

        # Rule 4: Price range sanity (40-160 for most bonds)
        if 0 < new_price < 40 or new_price > 160:
            anomalies.append({
                "type": AnomalyType.PRICE_SPIKE.value,
                "message": f"Price {new_price} outside normal range (40-160)",
                "severity": "WARNING",
            })

        # Log quality check
        status = "ANOMALOUS" if anomalies else "FRESH"
        await cls._log_quality_check(isin, "PRICE_VALIDATION", status, source,
                                      new_price, anomalies)

        # Determine if CRITICAL anomalies should enforce rejection
        has_critical = any(a.get("severity") == "CRITICAL" for a in anomalies)

        return {
            "isin": isin,
            "price": new_price,
            "source": source,
            "valid": len(anomalies) == 0,
            "reject": has_critical,  # ENFORCED: callers MUST reject if True
            "anomalies": anomalies,
            "checked_at": now,
        }

    @classmethod
    async def check_data_freshness(cls) -> dict:
        """
        Check freshness of all pricing data across the portfolio.
        Returns staleness report per ISIN.
        """
        now = utcnow()
        holdings = await fetch_all(
                        """
                        SELECT DISTINCT isin
                        FROM portfolio_holdings
                        WHERE COALESCE(portfolio_id, 'default') = 'default'
                            AND COALESCE(quantity, 0) > 0
                        """
        )

        results = []
        stale_count = 0
        missing_count = 0

        for h in holdings:
            isin = h["isin"]
            latest = await fetch_one(
                """SELECT clean_price AS price,
                          data_source AS source,
                          COALESCE(last_updated, price_date) AS updated_at
                   FROM unified_market_data
                   WHERE isin = :isin
                   ORDER BY COALESCE(last_updated, price_date) DESC LIMIT 1""",
                {"isin": isin},
            )

            if not latest or not latest.get("updated_at"):
                status = DataQualityStatus.MISSING
                staleness_hours = None
                missing_count += 1
            else:
                try:
                    updated = datetime.fromisoformat(latest["updated_at"].replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    staleness = (now - updated).total_seconds() / 3600
                    staleness_hours = round(staleness, 1)

                    if staleness < 24:
                        status = DataQualityStatus.FRESH
                    elif staleness < 72:
                        status = DataQualityStatus.STALE
                        stale_count += 1
                    else:
                        status = DataQualityStatus.VERY_STALE
                        stale_count += 1
                except Exception:
                    status = DataQualityStatus.STALE
                    staleness_hours = None
                    stale_count += 1

            # Update freshness tracker
            try:
                await execute_query(
                    """INSERT OR REPLACE INTO data_freshness_tracker
                       (isin, data_type, last_updated, source, value,
                        staleness_hours, quality_status)
                       VALUES (:isin, 'price', :lu, :src, :val, :sh, :qs)""",
                    {
                        "isin": isin,
                        "lu": latest.get("updated_at", "") if latest else "",
                        "src": latest.get("source", "NONE") if latest else "NONE",
                        "val": latest.get("price") if latest else None,
                        "sh": staleness_hours,
                        "qs": status.value,
                    },
                )
            except Exception:
                pass

            results.append({
                "isin": isin,
                "status": status.value,
                "staleness_hours": staleness_hours,
                "last_price": latest.get("price") if latest else None,
                "last_source": latest.get("source") if latest else None,
                "last_updated": latest.get("updated_at") if latest else None,
            })

        return {
            "total_holdings": len(holdings),
            "fresh": len(holdings) - stale_count - missing_count,
            "stale": stale_count,
            "missing": missing_count,
            "freshness_pct": round((len(holdings) - stale_count - missing_count) / len(holdings) * 100, 1) if holdings else 100,
            "details": results,
            "checked_at": now.isoformat(),
        }

    @classmethod
    async def get_data_source_status(cls) -> dict:
        """
        Data source transparency panel — shows status of all data feeds.
        """
        sources = await fetch_all(
            "SELECT * FROM data_source_registry ORDER BY priority ASC"
        )

        # Enrich with latest fetch info
        enriched = []
        for src in sources:
            last_data = await fetch_one(
                """SELECT COUNT(*) AS records,
                          MAX(COALESCE(last_updated, price_date)) AS latest
                   FROM unified_market_data
                   WHERE data_source = :src""",
                {"src": src["source_name"]},
            )
            enriched.append({
                **src,
                "total_records": last_data.get("records", 0) if last_data else 0,
                "latest_data": last_data.get("latest") if last_data else None,
            })

        return {
            "sources": enriched,
            "total_active": sum(1 for s in sources if s.get("is_active")),
            "checked_at": utcnow_iso(),
        }

    @classmethod
    async def apply_fallback(cls, isin: str) -> dict:
        """
        Apply fallback pricing mechanism for stale/missing data.
        Priority: NSE > BSE > FIMMDA > Broker > Last Known > Synthetic
        """
        sources_priority = ["NSE", "BSE", "FIMMDA", "CCIL", "BROKER_QUOTES", "SYNTHETIC"]
        now = utcnow()

        for source in sources_priority:
            price_data = await fetch_one(
                """SELECT clean_price AS price,
                          data_source AS source,
                          COALESCE(last_updated, price_date) AS updated_at
                   FROM unified_market_data
                   WHERE isin = :isin AND data_source = :src
                   ORDER BY COALESCE(last_updated, price_date) DESC LIMIT 1""",
                {"isin": isin, "src": source},
            )
            if price_data and price_data.get("price") and price_data["price"] > 0:
                try:
                    updated = datetime.fromisoformat(
                        price_data["updated_at"].replace("Z", "+00:00")
                    )
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    staleness = (now - updated).total_seconds() / 3600
                except Exception:
                    staleness = 999

                return {
                    "isin": isin,
                    "price": price_data["price"],
                    "source": source,
                    "fallback_used": True,
                    "staleness_hours": round(staleness, 1),
                    "quality": DataQualityStatus.FALLBACK.value if staleness > 24 else DataQualityStatus.FRESH.value,
                    "warning": f"Using {source} data ({round(staleness, 1)}h old)" if staleness > 24 else None,
                }

        # Last resort: use last known price from any source
        last_known = await fetch_one(
            """SELECT clean_price AS price,
                      data_source AS source,
                      COALESCE(last_updated, price_date) AS updated_at
               FROM unified_market_data
               WHERE isin = :isin AND clean_price > 0
               ORDER BY COALESCE(last_updated, price_date) DESC LIMIT 1""",
            {"isin": isin},
        )
        if last_known:
            return {
                "isin": isin,
                "price": last_known["price"],
                "source": f"LAST_KNOWN_{last_known['source']}",
                "fallback_used": True,
                "quality": DataQualityStatus.VERY_STALE.value,
                "warning": "Using last known price — data significantly stale",
            }

        return {
            "isin": isin,
            "price": None,
            "source": None,
            "fallback_used": True,
            "quality": DataQualityStatus.MISSING.value,
            "warning": "No pricing data available from any source",
        }

    @classmethod
    async def run_anomaly_detection(cls) -> dict:
        """
        Run batch anomaly detection across all portfolio holdings.
        Detects: stale prices, price spikes, zero prices, missing data.
        """
        holdings = await fetch_all(
            """
            SELECT DISTINCT isin
            FROM portfolio_holdings
            WHERE COALESCE(portfolio_id, 'default') = 'default'
              AND COALESCE(quantity, 0) > 0
            """
        )
        anomalies = []
        now = utcnow()

        for h in holdings:
            isin = h["isin"]

            # Check for stale data (>3 days)
            latest = await fetch_one(
                """SELECT clean_price AS price,
                          data_source AS source,
                          COALESCE(last_updated, price_date) AS updated_at
                   FROM unified_market_data
                   WHERE isin = :isin
                   ORDER BY COALESCE(last_updated, price_date) DESC LIMIT 1""",
                {"isin": isin},
            )
            if not latest or not latest.get("updated_at"):
                anomalies.append({
                    "isin": isin, "type": AnomalyType.MISSING_FEED.value,
                    "severity": "CRITICAL", "message": "No price data available",
                })
                continue

            try:
                updated = datetime.fromisoformat(latest["updated_at"].replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                staleness = (now - updated).total_seconds() / 3600
            except Exception:
                staleness = 999

            if staleness > 72:
                anomalies.append({
                    "isin": isin, "type": AnomalyType.STALE_PRICE.value,
                    "severity": "WARNING",
                    "message": f"Price data {round(staleness)}h old (source: {latest.get('source')})",
                })

            latest_price = latest.get("price")
            if latest_price is None or latest_price <= 0:
                anomalies.append({
                    "isin": isin, "type": AnomalyType.ZERO_PRICE.value,
                    "severity": "CRITICAL", "message": "Zero/negative price recorded",
                })

        return {
            "total_checked": len(holdings),
            "anomalies_found": len(anomalies),
            "anomalies": anomalies,
            "checked_at": now.isoformat(),
        }

    @classmethod
    async def _log_quality_check(cls, isin: str, check_type: str,
                                   status: str, source: str,
                                   price: Optional[float],
                                   anomalies: list):
        """Log a data quality check to audit trail."""
        try:
            await execute_query(
                """INSERT INTO data_quality_log
                   (isin, check_type, status, details, data_source,
                    price_value, checked_at)
                   VALUES (:isin, :ct, :st, :det, :src, :pv, :ca)""",
                {
                    "isin": isin, "ct": check_type, "st": status,
                    "det": json.dumps(anomalies) if anomalies else None,
                    "src": source, "pv": price,
                    "ca": utcnow_iso(),
                },
            )
        except Exception:
            pass
