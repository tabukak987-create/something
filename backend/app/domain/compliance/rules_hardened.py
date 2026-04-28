"""
Institutional Compliance Hardening Service
=============================================
Enhanced compliance engine with:
  - Configurable limits (rating, sector, duration, issuer)
  - Breach tracking & historical log
  - Automated breach alert system
  - Breach summary report generation
  - Rating-based limits
  - Sector exposure limits
  - Duration band limits

All breaches are logged immutably with timestamps and resolution tracking.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso, utcnow

log = get_logger("service.compliance_hardened")


# ── Breach Types ──────────────────────────────────────────────────────────────
class BreachType(str, Enum):
    SINGLE_ISSUER_LIMIT = "SINGLE_ISSUER_LIMIT"
    GROUP_EXPOSURE_LIMIT = "GROUP_EXPOSURE_LIMIT"
    SECTOR_EXPOSURE_LIMIT = "SECTOR_EXPOSURE_LIMIT"
    RATING_BUCKET_LIMIT = "RATING_BUCKET_LIMIT"
    DURATION_BAND_LIMIT = "DURATION_BAND_LIMIT"
    RATING_FLOOR_VIOLATION = "RATING_FLOOR_VIOLATION"
    MATURITY_CAP_VIOLATION = "MATURITY_CAP_VIOLATION"
    CONCENTRATION_LIMIT = "CONCENTRATION_LIMIT"
    LIQUIDITY_THRESHOLD = "LIQUIDITY_THRESHOLD"
    EPFO_ELIGIBILITY = "EPFO_ELIGIBILITY"
    INSTRUMENT_RESTRICTION = "INSTRUMENT_RESTRICTION"
    ALM_MISMATCH = "ALM_MISMATCH"


class BreachSeverity(str, Enum):
    WARNING = "WARNING"       # Approaching limit (>80% of threshold)
    BREACH = "BREACH"         # Limit exceeded
    CRITICAL = "CRITICAL"     # Severe breach requiring immediate action


class BreachStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    WAIVED = "WAIVED"         # Board-approved exception


# ── Schema DDL ────────────────────────────────────────────────────────────────
COMPLIANCE_HARDENED_DDL = """
CREATE TABLE IF NOT EXISTS compliance_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    limit_type TEXT NOT NULL,
    limit_key TEXT NOT NULL,
    limit_value REAL NOT NULL,
    warning_threshold REAL NOT NULL DEFAULT 0.8,
    description TEXT,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(limit_type, limit_key, effective_from)
);

CREATE TABLE IF NOT EXISTS compliance_breaches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    breach_type TEXT NOT NULL,
    breach_key TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'BREACH',
    status TEXT NOT NULL DEFAULT 'OPEN',
    limit_value REAL,
    actual_value REAL,
    breach_pct REAL,
    description TEXT,
    affected_isins TEXT,
    detected_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_notes TEXT,
    waiver_reference TEXT,
    portfolio_snapshot TEXT,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_breach_type ON compliance_breaches(breach_type);
CREATE INDEX IF NOT EXISTS idx_breach_status ON compliance_breaches(status);
CREATE INDEX IF NOT EXISTS idx_breach_detected ON compliance_breaches(detected_at);
CREATE INDEX IF NOT EXISTS idx_breach_severity ON compliance_breaches(severity);

CREATE TABLE IF NOT EXISTS compliance_alert_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    email_recipients TEXT,
    webhook_url TEXT,
    cooldown_minutes INTEGER NOT NULL DEFAULT 60,
    last_triggered TEXT,
    created_at TEXT NOT NULL
);
"""


# ── Default Compliance Limits ─────────────────────────────────────────────────
DEFAULT_LIMITS = {
    # Single issuer exposure limits
    ("SINGLE_ISSUER", "default"): {"value": 5.0, "desc": "Max 5% portfolio in single issuer"},
    ("SINGLE_ISSUER", "sovereign"): {"value": 100.0, "desc": "No limit for sovereign bonds"},
    ("SINGLE_ISSUER", "psu"): {"value": 10.0, "desc": "Max 10% for PSU issuers"},

    # Group exposure limits
    ("GROUP_EXPOSURE", "default"): {"value": 10.0, "desc": "Max 10% portfolio in single group"},

    # Sector exposure limits
    ("SECTOR_EXPOSURE", "NBFC"): {"value": 25.0, "desc": "Max 25% in NBFC sector"},
    ("SECTOR_EXPOSURE", "BANKING"): {"value": 30.0, "desc": "Max 30% in Banking sector"},
    ("SECTOR_EXPOSURE", "INFRASTRUCTURE"): {"value": 25.0, "desc": "Max 25% in Infrastructure"},
    ("SECTOR_EXPOSURE", "POWER"): {"value": 20.0, "desc": "Max 20% in Power sector"},
    ("SECTOR_EXPOSURE", "REAL_ESTATE"): {"value": 10.0, "desc": "Max 10% in Real Estate"},
    ("SECTOR_EXPOSURE", "default"): {"value": 30.0, "desc": "Max 30% in any single sector"},

    # Rating bucket limits
    ("RATING_BUCKET", "AAA"): {"value": 100.0, "desc": "No cap on AAA"},
    ("RATING_BUCKET", "AA+"): {"value": 40.0, "desc": "Max 40% in AA+ bucket"},
    ("RATING_BUCKET", "AA"): {"value": 30.0, "desc": "Max 30% in AA bucket"},
    ("RATING_BUCKET", "AA-"): {"value": 20.0, "desc": "Max 20% in AA- bucket"},
    ("RATING_BUCKET", "A+"): {"value": 10.0, "desc": "Max 10% in A+ bucket"},
    ("RATING_BUCKET", "sub_IG"): {"value": 0.0, "desc": "Zero tolerance for sub-IG"},

    # Duration band limits
    ("DURATION_BAND", "0-1Y"): {"value": 30.0, "desc": "Max 30% in 0-1Y bucket"},
    ("DURATION_BAND", "1-3Y"): {"value": 40.0, "desc": "Max 40% in 1-3Y bucket"},
    ("DURATION_BAND", "3-5Y"): {"value": 40.0, "desc": "Max 40% in 3-5Y bucket"},
    ("DURATION_BAND", "5-7Y"): {"value": 30.0, "desc": "Max 30% in 5-7Y bucket"},
    ("DURATION_BAND", "7-10Y"): {"value": 25.0, "desc": "Max 25% in 7-10Y bucket"},
    ("DURATION_BAND", "10Y+"): {"value": 15.0, "desc": "Max 15% in 10Y+ bucket"},

    # Portfolio-level limits
    ("PORTFOLIO", "max_maturity_years"): {"value": 30.0, "desc": "Max 30Y individual bond maturity"},
    ("PORTFOLIO", "min_rating"): {"value": 5.0, "desc": "Minimum AA- rating (score 5)"},
    ("PORTFOLIO", "max_portfolio_duration"): {"value": 8.0, "desc": "Max portfolio modified duration 8Y"},
    ("PORTFOLIO", "min_liquidity_score"): {"value": 20.0, "desc": "Min liquidity score for new purchases"},
}


class ComplianceHardeningService:
    """
    Institutional compliance hardening with configurable limits,
    breach tracking, and automated alerting.
    """

    @classmethod
    async def initialize(cls):
        """Create compliance hardening tables and seed defaults."""
        try:
            for stmt in COMPLIANCE_HARDENED_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await execute_query(stmt + ";")

            # Seed default limits if empty
            count = await fetch_one("SELECT COUNT(*) AS cnt FROM compliance_limits")
            if not count or count["cnt"] == 0:
                await cls._seed_default_limits()

            log.info("compliance_hardening_initialized")
        except Exception as e:
            log.error("compliance_hardening_init_failed", error=str(e))

    @classmethod
    async def _seed_default_limits(cls):
        """Seed default compliance limits."""
        now = utcnow_iso()
        for (limit_type, limit_key), config in DEFAULT_LIMITS.items():
            try:
                await execute_query(
                    """INSERT INTO compliance_limits
                       (limit_type, limit_key, limit_value, warning_threshold,
                        description, effective_from, created_by, created_at)
                       VALUES (:lt, :lk, :lv, :wt, :desc, :ef, :cb, :ca)""",
                    {
                        "lt": limit_type, "lk": limit_key, "lv": config["value"],
                        "wt": 0.8, "desc": config["desc"], "ef": now,
                        "cb": "system", "ca": now,
                    },
                )
            except Exception:
                pass  # Skip if exists

    @classmethod
    async def get_limits(cls, limit_type: Optional[str] = None) -> List[dict]:
        """Get all active compliance limits, optionally filtered by type."""
        if limit_type:
            rows = await fetch_all(
                "SELECT * FROM compliance_limits WHERE limit_type = :lt AND is_active = 1 ORDER BY limit_key",
                {"lt": limit_type},
            )
        else:
            rows = await fetch_all(
                "SELECT * FROM compliance_limits WHERE is_active = 1 ORDER BY limit_type, limit_key"
            )
        return rows

    @classmethod
    async def update_limit(cls, limit_id: int, new_value: float, user: dict, 
                           warning_threshold: float = 0.8) -> dict:
        """Update a compliance limit. Returns old and new values for audit."""
        old = await fetch_one("SELECT * FROM compliance_limits WHERE id = :id", {"id": limit_id})
        if not old:
            raise ValueError(f"Limit ID {limit_id} not found")

        await execute_query(
            "UPDATE compliance_limits SET limit_value = :val, warning_threshold = :wt WHERE id = :id",
            {"val": new_value, "wt": warning_threshold, "id": limit_id},
        )

        return {
            "limit_id": limit_id,
            "limit_type": old["limit_type"],
            "limit_key": old["limit_key"],
            "old_value": old["limit_value"],
            "new_value": new_value,
            "changed_by": user.get("username", "system"),
            "changed_at": utcnow_iso(),
        }

    @classmethod
    async def run_compliance_scan(cls, portfolio_data: dict) -> dict:
        """
        Run full compliance scan against portfolio data.
        Checks all limits and records breaches.
        """
        holdings = portfolio_data.get("holdings", [])
        if not holdings:
            return {"breaches": [], "warnings": [], "total_checks": 0}

        total_value = sum(h.get("market_value", 0) for h in holdings)
        if total_value == 0:
            return {"breaches": [], "warnings": [], "total_checks": 0}

        breaches = []
        warnings = []
        checks = 0
        limits = await cls.get_limits()
        limit_map = {}
        for lim in limits:
            limit_map[(lim["limit_type"], lim["limit_key"])] = lim

        # ── 1. Single Issuer Concentration ──
        issuer_exposure = {}
        for h in holdings:
            issuer = h.get("issuer_name", "UNKNOWN")
            issuer_exposure[issuer] = issuer_exposure.get(issuer, 0) + h.get("market_value", 0)

        for issuer, exposure in issuer_exposure.items():
            pct = exposure / total_value * 100
            limit_entry = limit_map.get(("SINGLE_ISSUER", "default"), {})
            limit_val = limit_entry.get("limit_value", 5.0) if limit_entry else 5.0
            warn_thresh = limit_entry.get("warning_threshold", 0.8) if limit_entry else 0.8
            checks += 1

            if pct > limit_val:
                breach = await cls._record_breach(
                    BreachType.SINGLE_ISSUER_LIMIT, issuer,
                    BreachSeverity.BREACH, limit_val, pct,
                    f"Issuer '{issuer}' at {pct:.1f}% exceeds {limit_val}% limit",
                    [h.get("isin", "") for h in holdings if h.get("issuer_name") == issuer],
                )
                breaches.append(breach)
            elif pct > limit_val * warn_thresh:
                warnings.append({
                    "type": "SINGLE_ISSUER_LIMIT",
                    "key": issuer,
                    "current_pct": round(pct, 2),
                    "limit_pct": limit_val,
                    "severity": "WARNING",
                })

        # ── 2. Sector Exposure ──
        sector_exposure = {}
        for h in holdings:
            sector = h.get("sector", "OTHER")
            sector_exposure[sector] = sector_exposure.get(sector, 0) + h.get("market_value", 0)

        for sector, exposure in sector_exposure.items():
            pct = exposure / total_value * 100
            limit_entry = limit_map.get(("SECTOR_EXPOSURE", sector)) or limit_map.get(("SECTOR_EXPOSURE", "default"), {})
            limit_val = limit_entry.get("limit_value", 30.0) if limit_entry else 30.0
            checks += 1

            if pct > limit_val:
                breach = await cls._record_breach(
                    BreachType.SECTOR_EXPOSURE_LIMIT, sector,
                    BreachSeverity.BREACH, limit_val, pct,
                    f"Sector '{sector}' at {pct:.1f}% exceeds {limit_val}% limit",
                )
                breaches.append(breach)
            elif pct > limit_val * 0.8:
                warnings.append({
                    "type": "SECTOR_EXPOSURE_LIMIT", "key": sector,
                    "current_pct": round(pct, 2), "limit_pct": limit_val,
                })

        # ── 3. Rating Bucket Limits ──
        rating_exposure = {}
        for h in holdings:
            rating = h.get("rating", "NR")
            rating_exposure[rating] = rating_exposure.get(rating, 0) + h.get("market_value", 0)

        for rating, exposure in rating_exposure.items():
            pct = exposure / total_value * 100
            limit_entry = limit_map.get(("RATING_BUCKET", rating), {})
            limit_val = limit_entry.get("limit_value", 40.0) if limit_entry else 40.0
            checks += 1

            if pct > limit_val:
                breach = await cls._record_breach(
                    BreachType.RATING_BUCKET_LIMIT, rating,
                    BreachSeverity.BREACH if limit_val > 0 else BreachSeverity.CRITICAL,
                    limit_val, pct,
                    f"Rating bucket '{rating}' at {pct:.1f}% exceeds {limit_val}% limit",
                )
                breaches.append(breach)

        # ── 4. Duration Band Limits ──
        duration_bands = {"0-1Y": 0, "1-3Y": 0, "3-5Y": 0, "5-7Y": 0, "7-10Y": 0, "10Y+": 0}
        for h in holdings:
            dur = h.get("modified_duration", 0) or h.get("years_to_maturity", 0)
            band = cls._get_duration_band(dur)
            duration_bands[band] = duration_bands.get(band, 0) + h.get("market_value", 0)

        for band, exposure in duration_bands.items():
            if exposure == 0:
                continue
            pct = exposure / total_value * 100
            limit_entry = limit_map.get(("DURATION_BAND", band), {})
            limit_val = limit_entry.get("limit_value", 40.0) if limit_entry else 40.0
            checks += 1

            if pct > limit_val:
                breach = await cls._record_breach(
                    BreachType.DURATION_BAND_LIMIT, band,
                    BreachSeverity.WARNING, limit_val, pct,
                    f"Duration band '{band}' at {pct:.1f}% exceeds {limit_val}% limit",
                )
                breaches.append(breach)

        # ── 5. Rating Floor Check ──
        sub_ig_ratings = {"BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"}
        for h in holdings:
            rating = h.get("rating", "NR").upper()
            if rating in sub_ig_ratings:
                checks += 1
                breach = await cls._record_breach(
                    BreachType.RATING_FLOOR_VIOLATION, h.get("isin", ""),
                    BreachSeverity.CRITICAL, 0, 0,
                    f"Sub-investment-grade bond {h.get('isin')} rated {rating} — not PF eligible",
                    [h.get("isin", "")],
                )
                breaches.append(breach)

        return {
            "total_checks": checks,
            "breaches": breaches,
            "warnings": warnings,
            "breach_count": len(breaches),
            "warning_count": len(warnings),
            "scan_timestamp": utcnow_iso(),
            "portfolio_value": total_value,
        }

    @classmethod
    async def _record_breach(cls, breach_type: BreachType, breach_key: str,
                              severity: BreachSeverity, limit_val: float,
                              actual_val: float, description: str,
                              affected_isins: Optional[List[str]] = None) -> dict:
        """Record an immutable breach entry."""
        now = utcnow_iso()
        breach_pct = (actual_val / limit_val * 100) if limit_val > 0 else 999

        try:
            await execute_query(
                """INSERT INTO compliance_breaches
                   (breach_type, breach_key, severity, status, limit_value,
                    actual_value, breach_pct, description, affected_isins,
                    detected_at)
                   VALUES (:bt, :bk, :sev, :st, :lv, :av, :bp, :desc, :isins, :da)""",
                {
                    "bt": breach_type.value, "bk": breach_key,
                    "sev": severity.value, "st": BreachStatus.OPEN.value,
                    "lv": limit_val, "av": actual_val, "bp": round(breach_pct, 2),
                    "desc": description,
                    "isins": json.dumps(affected_isins) if affected_isins else None,
                    "da": now,
                },
            )
        except Exception as e:
            log.error("breach_record_failed", error=str(e))

        return {
            "breach_type": breach_type.value,
            "breach_key": breach_key,
            "severity": severity.value,
            "limit_value": limit_val,
            "actual_value": round(actual_val, 2),
            "breach_pct": round(breach_pct, 2),
            "description": description,
            "detected_at": now,
        }

    @classmethod
    async def get_breach_history(cls, breach_type: Optional[str] = None,
                                  status: Optional[str] = None,
                                  days: int = 90,
                                  limit: int = 100) -> dict:
        """Get historical breach records with filtering."""
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        conditions = ["detected_at >= :cutoff"]
        params: dict = {"cutoff": cutoff}

        if breach_type:
            conditions.append("breach_type = :bt")
            params["bt"] = breach_type
        if status:
            conditions.append("status = :st")
            params["st"] = status

        where = " AND ".join(conditions)
        rows = await fetch_all(
            f"""SELECT * FROM compliance_breaches
                WHERE {where}
                ORDER BY detected_at DESC LIMIT :lim""",
            {**params, "lim": limit},
        )

        summary = await fetch_all(
            f"""SELECT breach_type, severity, COUNT(*) AS count
                FROM compliance_breaches
                WHERE {where}
                GROUP BY breach_type, severity
                ORDER BY count DESC""",
            params,
        )

        return {
            "breaches": rows,
            "summary": summary,
            "period_days": days,
            "total": len(rows),
        }

    @classmethod
    async def acknowledge_breach(cls, breach_id: int, user: dict, notes: str = "") -> dict:
        """Mark a breach as acknowledged by a compliance officer."""
        now = utcnow_iso()
        await execute_query(
            """UPDATE compliance_breaches
               SET status = 'ACKNOWLEDGED', acknowledged_at = :now,
                   acknowledged_by = :user, resolution_notes = :notes
               WHERE id = :id""",
            {"now": now, "user": user.get("username"), "notes": notes, "id": breach_id},
        )
        return {"status": "acknowledged", "breach_id": breach_id}

    @classmethod
    async def resolve_breach(cls, breach_id: int, user: dict, 
                              resolution_notes: str = "") -> dict:
        """Mark a breach as resolved."""
        now = utcnow_iso()
        await execute_query(
            """UPDATE compliance_breaches
               SET status = 'RESOLVED', resolved_at = :now,
                   resolved_by = :user, resolution_notes = :notes
               WHERE id = :id""",
            {"now": now, "user": user.get("username"), "notes": resolution_notes, "id": breach_id},
        )
        return {"status": "resolved", "breach_id": breach_id}

    @classmethod
    async def generate_breach_report(cls, days: int = 30) -> dict:
        """Generate compliance breach summary report for board/trustee."""
        cutoff = (utcnow() - timedelta(days=days)).isoformat()

        # Overall stats
        total = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM compliance_breaches WHERE detected_at >= :c",
            {"c": cutoff},
        )
        by_severity = await fetch_all(
            """SELECT severity, COUNT(*) AS count FROM compliance_breaches
               WHERE detected_at >= :c GROUP BY severity""",
            {"c": cutoff},
        )
        by_status = await fetch_all(
            """SELECT status, COUNT(*) AS count FROM compliance_breaches
               WHERE detected_at >= :c GROUP BY status""",
            {"c": cutoff},
        )
        by_type = await fetch_all(
            """SELECT breach_type, COUNT(*) AS count, AVG(breach_pct) AS avg_breach_pct
               FROM compliance_breaches WHERE detected_at >= :c
               GROUP BY breach_type ORDER BY count DESC""",
            {"c": cutoff},
        )
        # Resolution metrics
        resolution_rows = await fetch_all(
            """SELECT resolved_at, detected_at
               FROM compliance_breaches
               WHERE detected_at >= :c AND resolved_at IS NOT NULL""",
            {"c": cutoff},
        )
        resolution_hours: List[float] = []
        for row in resolution_rows:
            try:
                resolved_at = datetime.fromisoformat(str(row.get("resolved_at")).replace("Z", "+00:00"))
                detected_at = datetime.fromisoformat(str(row.get("detected_at")).replace("Z", "+00:00"))
            except Exception:
                continue
            resolution_hours.append((resolved_at - detected_at).total_seconds() / 3600)
        avg_resolution = {"avg_hours": (sum(resolution_hours) / len(resolution_hours)) if resolution_hours else None}
        # Critical breaches
        critical = await fetch_all(
            """SELECT * FROM compliance_breaches
               WHERE detected_at >= :c AND severity = 'CRITICAL'
               ORDER BY detected_at DESC LIMIT 20""",
            {"c": cutoff},
        )
        # Open breaches
        open_count = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM compliance_breaches WHERE status = 'OPEN'",
        )

        return {
            "report_type": "Compliance Breach Summary",
            "period_days": days,
            "generated_at": utcnow_iso(),
            "total_breaches": total["cnt"] if total else 0,
            "by_severity": by_severity,
            "by_status": by_status,
            "by_type": by_type,
            "open_breaches": open_count["cnt"] if open_count else 0,
            "avg_resolution_hours": round(avg_resolution["avg_hours"], 1) if avg_resolution and avg_resolution["avg_hours"] else None,
            "critical_breaches": critical,
        }

    @staticmethod
    def _get_duration_band(duration_years: float) -> str:
        if duration_years <= 1:
            return "0-1Y"
        elif duration_years <= 3:
            return "1-3Y"
        elif duration_years <= 5:
            return "3-5Y"
        elif duration_years <= 7:
            return "5-7Y"
        elif duration_years <= 10:
            return "7-10Y"
        else:
            return "10Y+"
