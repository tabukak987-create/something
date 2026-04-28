"""
Investment Policy Statement (IPS) Dynamic Rules Engine
========================================================
Enforces all IPS rules dynamically across the portfolio:

  A. Credit Rating Compliance â€” min rating, AAA %, below-AA cap, dual ratings
  B. Issuer Concentration Limits â€” single issuer, group, sector caps
  C. Maturity Constraints â€” max maturity, 3-year bucket, clustering detection
  D. Liquidity Requirements â€” min liquid %, listed-only, volume threshold

All rules are configurable, stored in DB, and produce structured breach results.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso, utcnow
from app.domain.market_data.instrument_classification import classify_instrument
from app.domain.core_trust.issuer_identity import (
    build_issuer_group_lookup,
    is_goi_group,
    normalize_issuer_name,
    resolve_group_name,
)

log = get_logger("service.ips_engine")


# â”€â”€ Enums â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class IPSRuleCategory(str, Enum):
    CREDIT_RATING = "CREDIT_RATING"
    ISSUER_CONCENTRATION = "ISSUER_CONCENTRATION"
    MATURITY = "MATURITY"
    LIQUIDITY = "LIQUIDITY"
    DURATION = "DURATION"
    VALUATION = "VALUATION"
    REGULATORY = "REGULATORY"


class IPSBreachSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    BREACH = "BREACH"
    CRITICAL = "CRITICAL"


class IPSBreachAction(str, Enum):
    ALERT = "ALERT"
    BLOCK_TRADE = "BLOCK_TRADE"
    ESCALATE = "ESCALATE"
    FORCE_SELL = "FORCE_SELL"


# â”€â”€ Rating utilities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

RATING_HIERARCHY = {
    "SOVEREIGN": 0, "SOV": 0,
    "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7,
    "BBB+": 8, "BBB": 9, "BBB-": 10,
    "BB+": 11, "BB": 12, "BB-": 13,
    "B+": 14, "B": 15, "B-": 16,
    "CCC": 17, "CC": 18, "C": 19, "D": 20,
    "NR": 99,
}

INVESTMENT_GRADE_CUTOFF = 10  # BBB- and above


def _rating_rank(rating: str) -> int:
    """Lower rank = better rating."""
    r = (rating or "NR").upper().strip()
    return RATING_HIERARCHY.get(r, 99)


def _is_investment_grade(rating: str) -> bool:
    return _rating_rank(rating) <= INVESTMENT_GRADE_CUTOFF


def _is_below_aa(rating: str) -> bool:
    return _rating_rank(rating) > RATING_HIERARCHY["AA-"]


def _lowest_rating(*ratings: str) -> str:
    """Conservative method: return the worst (highest-ranked) rating."""
    best = "NR"
    worst_rank = -1
    for r in ratings:
        rank = _rating_rank(r)
        if rank != 99 and rank > worst_rank:
            worst_rank = rank
            best = r.upper().strip()
    return best if worst_rank >= 0 else "NR"


# â”€â”€ Schema DDL â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

IPS_ENGINE_DDL = """
CREATE TABLE IF NOT EXISTS ips_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_category TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    rule_value REAL,
    rule_value_text TEXT,
    comparison TEXT NOT NULL DEFAULT 'LTE',
    severity TEXT NOT NULL DEFAULT 'BREACH',
    action_on_breach TEXT NOT NULL DEFAULT 'ALERT',
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    UNIQUE(rule_category, rule_key)
);

CREATE TABLE IF NOT EXISTS ips_scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_type TEXT NOT NULL DEFAULT 'FULL',
    total_rules INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    breaches INTEGER NOT NULL DEFAULT 0,
    critical INTEGER NOT NULL DEFAULT 0,
    results_json TEXT,
    portfolio_snapshot_json TEXT,
    scanned_by TEXT NOT NULL DEFAULT 'system',
    scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ips_breach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    rule_id INTEGER,
    rule_category TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    action TEXT NOT NULL,
    limit_value REAL,
    actual_value REAL,
    breach_pct REAL,
    description TEXT,
    affected_isins TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN',
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    override_by TEXT,
    override_reason TEXT,
    override_at TEXT,
    detected_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ips_breach_category ON ips_breach_log(rule_category);
CREATE INDEX IF NOT EXISTS idx_ips_breach_status ON ips_breach_log(status);
CREATE INDEX IF NOT EXISTS idx_ips_breach_severity ON ips_breach_log(severity);
CREATE INDEX IF NOT EXISTS idx_ips_breach_date ON ips_breach_log(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_ips_scan_date ON ips_scan_results(scanned_at DESC);

CREATE TABLE IF NOT EXISTS rating_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT NOT NULL,
    agency TEXT NOT NULL,
    old_rating TEXT,
    new_rating TEXT,
    change_type TEXT NOT NULL DEFAULT 'UPDATE',
    change_date TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    alert_generated INTEGER NOT NULL DEFAULT 0,
    UNIQUE(isin, agency, change_date)
);

CREATE INDEX IF NOT EXISTS idx_rh_isin ON rating_history(isin);
CREATE INDEX IF NOT EXISTS idx_rh_date ON rating_history(change_date DESC);

CREATE TABLE IF NOT EXISTS dual_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT NOT NULL,
    agency1 TEXT NOT NULL,
    rating1 TEXT NOT NULL,
    agency2 TEXT,
    rating2 TEXT,
    effective_rating TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'LOWEST',
    updated_at TEXT NOT NULL,
    UNIQUE(isin)
);

CREATE INDEX IF NOT EXISTS idx_dr_isin ON dual_ratings(isin);

CREATE TABLE IF NOT EXISTS issuer_group_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_name TEXT NOT NULL,
    group_name TEXT NOT NULL,
    relationship TEXT DEFAULT 'SUBSIDIARY',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(issuer_name, group_name)
);

CREATE INDEX IF NOT EXISTS idx_igm_issuer ON issuer_group_map(issuer_name);
CREATE INDEX IF NOT EXISTS idx_igm_group ON issuer_group_map(group_name);
"""

# â”€â”€ Default IPS Rules â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DEFAULT_IPS_RULES = [
    # A. Credit Rating Compliance
    ("CREDIT_RATING", "min_rating", "Minimum Credit Rating", None, "AA-", "GTE", "CRITICAL", "BLOCK_TRADE",
     "All holdings must be rated AA- or above"),
    ("CREDIT_RATING", "min_aaa_pct", "Minimum AAA Allocation", 60.0, None, "GTE", "BREACH", "ALERT",
     "At least 60% of portfolio must be in AAA-rated bonds"),
    ("CREDIT_RATING", "max_below_aa_pct", "Maximum Below-AA Allocation", 10.0, None, "LTE", "BREACH", "BLOCK_TRADE",
     "Maximum 10% of portfolio can be below AA rating"),
    ("CREDIT_RATING", "require_dual_rating", "Mandatory Dual Rating", 1.0, None, "EQ", "WARNING", "ALERT",
     "All bonds must have ratings from minimum 2 agencies"),
    ("CREDIT_RATING", "use_lowest_rating", "Use Lowest Rating (Conservative)", 1.0, None, "EQ", "INFO", "ALERT",
     "System uses the lowest of multiple agency ratings"),

    # B. Issuer Concentration
    ("ISSUER_CONCENTRATION", "single_issuer_max_pct", "Single Issuer Max Exposure", 10.0, None, "LTE", "BREACH", "BLOCK_TRADE",
     "Single issuer exposure must not exceed 10% of portfolio"),
    ("ISSUER_CONCENTRATION", "single_group_max_pct", "Single Group Max Exposure", 15.0, None, "LTE", "BREACH", "BLOCK_TRADE",
     "Single corporate group exposure must not exceed 15%"),
    ("ISSUER_CONCENTRATION", "sector_max_pct", "Sector Max Exposure", 25.0, None, "LTE", "BREACH", "ALERT",
     "Single sector exposure must not exceed 25%"),
    ("ISSUER_CONCENTRATION", "sovereign_exempt", "Sovereign Exempt from Concentration", 1.0, None, "EQ", "INFO", "ALERT",
     "Government/Sovereign bonds exempt from concentration limits"),

    # C. Maturity Constraints
    ("MATURITY", "max_maturity_years", "Maximum Bond Maturity", 10.0, None, "LTE", "BREACH", "BLOCK_TRADE",
     "No bond shall have maturity exceeding 10 years"),
    ("MATURITY", "min_short_term_pct", "Minimum Short-Term Allocation", 30.0, None, "GTE", "WARNING", "ALERT",
     "At least 30% of holdings must mature within 3 years"),
    ("MATURITY", "max_bullet_cluster_pct", "Maximum Bullet Maturity Clustering", 20.0, None, "LTE", "WARNING", "ALERT",
     "No more than 20% of portfolio should mature in any single year"),
    ("MATURITY", "weighted_avg_maturity_max", "Max Weighted Average Maturity", 7.0, None, "LTE", "BREACH", "ALERT",
     "Portfolio weighted average maturity should not exceed 7 years"),

    # D. Liquidity Requirements
    ("LIQUIDITY", "min_liquid_pct", "Minimum Liquid Assets", 10.0, None, "GTE", "BREACH", "ALERT",
     "At least 10% of portfolio must be in liquid assets"),
    ("LIQUIDITY", "listed_bonds_only", "Listed Bonds Only", 1.0, None, "EQ", "CRITICAL", "BLOCK_TRADE",
     "Only listed bonds are permitted for investment"),
    ("LIQUIDITY", "min_volume_threshold", "Minimum Volume Threshold", 1000000.0, None, "GTE", "WARNING", "ALERT",
     "Bonds must have minimum â‚¹10L daily traded volume"),
    ("LIQUIDITY", "max_illiquid_pct", "Maximum Illiquid Exposure", 15.0, None, "LTE", "BREACH", "ALERT",
     "Illiquid bond exposure must not exceed 15%"),

    # Duration
    ("DURATION", "max_portfolio_duration", "Maximum Portfolio Duration", 6.0, None, "LTE", "BREACH", "ALERT",
     "Portfolio modified duration should not exceed 6 years"),
    ("DURATION", "max_single_bond_duration", "Maximum Single Bond Duration", 10.0, None, "LTE", "WARNING", "ALERT",
     "Single bond modified duration should not exceed 10 years"),
]


# â”€â”€ Issuer group defaults (common Indian corporate groups) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DEFAULT_ISSUER_GROUPS = [
    ("TATA MOTORS FINANCE", "TATA GROUP", "SUBSIDIARY"),
    ("TATA CAPITAL FINANCIAL", "TATA GROUP", "SUBSIDIARY"),
    ("TATA CAPITAL HOUSING", "TATA GROUP", "SUBSIDIARY"),
    ("TATA POWER", "TATA GROUP", "SUBSIDIARY"),
    ("TATA STEEL", "TATA GROUP", "SUBSIDIARY"),
    ("HDB FINANCIAL SERVICES", "HDFC GROUP", "SUBSIDIARY"),
    ("HDFC LIMITED", "HDFC GROUP", "PARENT"),
    ("HDFC BANK", "HDFC GROUP", "SUBSIDIARY"),
    ("L&T FINANCE", "L&T GROUP", "SUBSIDIARY"),
    ("L&T INFRASTRUCTURE FINANCE", "L&T GROUP", "SUBSIDIARY"),
    ("LARSEN AND TOUBRO", "L&T GROUP", "PARENT"),
    ("PNB HOUSING FINANCE", "PNB GROUP", "SUBSIDIARY"),
    ("PUNJAB NATIONAL BANK", "PNB GROUP", "PARENT"),
    ("AXIS BANK", "AXIS GROUP", "PARENT"),
    ("AXIS FINANCE", "AXIS GROUP", "SUBSIDIARY"),
    ("BAJAJ FINANCE", "BAJAJ GROUP", "SUBSIDIARY"),
    ("BAJAJ FINSERV", "BAJAJ GROUP", "PARENT"),
    ("BAJAJ HOUSING FINANCE", "BAJAJ GROUP", "SUBSIDIARY"),
    ("KOTAK MAHINDRA BANK", "KOTAK GROUP", "PARENT"),
    ("KOTAK MAHINDRA PRIME", "KOTAK GROUP", "SUBSIDIARY"),
    ("ICICI BANK", "ICICI GROUP", "PARENT"),
    ("ICICI PRUDENTIAL", "ICICI GROUP", "SUBSIDIARY"),
    ("ICICI HOME FINANCE", "ICICI GROUP", "SUBSIDIARY"),
    ("INDIAN RAILWAY FINANCE", "GOI_GROUP", "PSU"),
    ("IRFC", "GOI_GROUP", "PSU"),
    ("POWER FINANCE CORPORATION", "GOI_GROUP", "PSU"),
    ("REC LIMITED", "GOI_GROUP", "PSU"),
    ("NABARD", "GOI_GROUP", "PSU"),
    ("NHPC", "GOI_GROUP", "PSU"),
    ("NTPC", "GOI_GROUP", "PSU"),
    ("MUTHOOT FINANCE", "MUTHOOT GROUP", "PARENT"),
    ("MUTHOOT HOMEFIN", "MUTHOOT GROUP", "SUBSIDIARY"),
    ("SHRIRAM FINANCE", "SHRIRAM GROUP", "PARENT"),
    ("SHRIRAM TRANSPORT FINANCE", "SHRIRAM GROUP", "SUBSIDIARY"),
    ("SHRIRAM HOUSING FINANCE", "SHRIRAM GROUP", "SUBSIDIARY"),
    ("MANAPPURAM FINANCE", "MANAPPURAM GROUP", "PARENT"),
    ("ADITYA BIRLA FINANCE", "ADITYA BIRLA GROUP", "SUBSIDIARY"),
    ("ADITYA BIRLA HOUSING FINANCE", "ADITYA BIRLA GROUP", "SUBSIDIARY"),
    ("MAHINDRA & MAHINDRA FINANCIAL", "MAHINDRA GROUP", "SUBSIDIARY"),
    ("MAHINDRA RURAL HOUSING", "MAHINDRA GROUP", "SUBSIDIARY"),
]


class IPSEngine:
    """
    Dynamic Investment Policy Statement Rules Engine.
    Enforces credit rating compliance, concentration limits,
    maturity constraints, and liquidity requirements.
    """

    @classmethod
    async def initialize(cls):
        """Create IPS tables and seed default rules."""
        try:
            for stmt in IPS_ENGINE_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await execute_query(stmt + ";")

            # Seed default rules if empty
            count = await fetch_one("SELECT COUNT(*) AS cnt FROM ips_rules")
            if not count or count["cnt"] == 0:
                await cls._seed_default_rules()
                await cls._seed_issuer_groups()

            log.info("ips_engine_initialized")
        except Exception as e:
            log.error("ips_engine_init_failed", error=str(e))

    @classmethod
    async def _seed_default_rules(cls):
        now = utcnow_iso()
        for rule in DEFAULT_IPS_RULES:
            cat, key, name, val, val_text, comp, sev, action, desc = rule
            try:
                await execute_query(
                    """INSERT INTO ips_rules
                       (rule_category, rule_key, rule_name, rule_value, rule_value_text,
                        comparison, severity, action_on_breach, description,
                        effective_from, created_at)
                       VALUES (:cat, :key, :name, :val, :vt, :comp, :sev, :act, :desc, :ef, :ca)""",
                    {"cat": cat, "key": key, "name": name, "val": val, "vt": val_text,
                     "comp": comp, "sev": sev, "act": action, "desc": desc,
                     "ef": now, "ca": now},
                )
            except Exception:
                pass

    @classmethod
    async def _seed_issuer_groups(cls):
        now = utcnow_iso()
        for issuer, group, rel in DEFAULT_ISSUER_GROUPS:
            try:
                await execute_query(
                    """INSERT OR IGNORE INTO issuer_group_map
                       (issuer_name, group_name, relationship, created_at)
                       VALUES (:issuer, :group, :rel, :now)""",
                    {"issuer": issuer, "group": group, "rel": rel, "now": now},
                )
            except Exception:
                pass

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Rule Management
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def get_rules(cls, category: Optional[str] = None) -> List[dict]:
        """Get all active IPS rules, optionally by category."""
        if category:
            return await fetch_all(
                "SELECT * FROM ips_rules WHERE rule_category = :cat AND is_active = 1 ORDER BY rule_key",
                {"cat": category},
            )
        return await fetch_all(
            "SELECT * FROM ips_rules WHERE is_active = 1 ORDER BY rule_category, rule_key"
        )

    # â”€â”€ Mandatory / non-deactivatable rule keys â”€â”€
    # These core safety rules cannot be deactivated or set to unsafe values.
    MANDATORY_RULE_KEYS = {
        "min_rating",           # Credit quality floor
        "single_issuer_max_pct", # Concentration limit
        "max_maturity_years",   # Maturity cap
        "min_aaa_pct",          # AAA allocation floor
        "max_below_aa_pct",     # Below-AA cap
    }

    @classmethod
    async def update_rule(cls, rule_id: int, new_value: float, user: str,
                          new_value_text: Optional[str] = None) -> dict:
        """Update an IPS rule value. Returns old and new values for audit."""
        old = await fetch_one("SELECT * FROM ips_rules WHERE id = :id", {"id": rule_id})
        if not old:
            raise ValueError(f"Rule ID {rule_id} not found")

        # Guard: Prevent deactivation of mandatory rules
        rule_key = old.get("rule_key", "")
        if rule_key in cls.MANDATORY_RULE_KEYS:
            # Cannot set value to 0 (effectively disables the rule)
            if new_value == 0:
                raise ValueError(
                    f"Cannot deactivate mandatory rule '{rule_key}' (ID {rule_id}). "
                    "This is a core safety rule required by PF Trust investment guidelines."
                )
            # Validate safety bounds for specific rules
            if rule_key == "single_issuer_max_pct" and new_value > 25:
                raise ValueError(
                    f"Cannot set single_issuer_max_pct above 25% (requested {new_value}%). "
                    "EPFO guidelines cap single-issuer exposure."
                )
            if rule_key == "max_below_aa_pct" and new_value > 20:
                raise ValueError(
                    f"Cannot set max_below_aa_pct above 20% (requested {new_value}%). "
                    "Regulatory cap on below-AA instruments."
                )

        if new_value_text:
            await execute_query(
                "UPDATE ips_rules SET rule_value = :val, rule_value_text = :vt WHERE id = :id",
                {"val": new_value, "vt": new_value_text, "id": rule_id},
            )
        else:
            await execute_query(
                "UPDATE ips_rules SET rule_value = :val WHERE id = :id",
                {"val": new_value, "id": rule_id},
            )

        return {
            "rule_id": rule_id,
            "rule_name": old["rule_name"],
            "old_value": old["rule_value"],
            "new_value": new_value,
            "changed_by": user,
            "changed_at": utcnow_iso(),
        }

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Full IPS Compliance Scan
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def run_full_scan(cls, user: str = "system") -> dict:
        """
        Run comprehensive IPS compliance scan against the current portfolio.
        Checks all active IPS rules and records breaches.
        """
        holdings = await cls._get_portfolio_holdings()
        if not holdings:
            return {"status": "NO_DATA", "message": "No portfolio holdings found"}

        total_value = sum(h.get("market_value", 0) or 0 for h in holdings)
        if total_value <= 0:
            return {"status": "NO_DATA", "message": "Portfolio has zero market value"}

        rules = await cls.get_rules()
        rule_map = {(r["rule_category"], r["rule_key"]): r for r in rules}

        results = []
        passed = 0
        warnings = 0
        breaches = 0
        critical = 0

        # â”€â”€ A. Credit Rating Compliance â”€â”€
        cr_results = await cls._check_credit_rating_rules(holdings, total_value, rule_map)
        for r in cr_results:
            results.append(r)
            if r["status"] == "PASS":
                passed += 1
            elif r["severity"] == "WARNING":
                warnings += 1
            elif r["severity"] == "CRITICAL":
                critical += 1
            else:
                breaches += 1

        # â”€â”€ B. Issuer Concentration â”€â”€
        ic_results = await cls._check_concentration_rules(holdings, total_value, rule_map)
        for r in ic_results:
            results.append(r)
            if r["status"] == "PASS":
                passed += 1
            elif r["severity"] == "WARNING":
                warnings += 1
            elif r["severity"] == "CRITICAL":
                critical += 1
            else:
                breaches += 1

        # â”€â”€ C. Maturity Constraints â”€â”€
        mat_results = await cls._check_maturity_rules(holdings, total_value, rule_map)
        for r in mat_results:
            results.append(r)
            if r["status"] == "PASS":
                passed += 1
            elif r["severity"] == "WARNING":
                warnings += 1
            else:
                breaches += 1

        # â”€â”€ D. Liquidity Requirements â”€â”€
        liq_results = await cls._check_liquidity_rules(holdings, total_value, rule_map)
        for r in liq_results:
            results.append(r)
            if r["status"] == "PASS":
                passed += 1
            elif r["severity"] == "WARNING":
                warnings += 1
            elif r["severity"] == "CRITICAL":
                critical += 1
            else:
                breaches += 1

        # â”€â”€ E. Duration Rules â”€â”€
        dur_results = await cls._check_duration_rules(holdings, total_value, rule_map)
        for r in dur_results:
            results.append(r)
            if r["status"] == "PASS":
                passed += 1
            elif r["severity"] == "WARNING":
                warnings += 1
            else:
                breaches += 1

        now = utcnow_iso()
        total_rules = len(results)

        # Record scan
        try:
            await execute_query(
                """INSERT INTO ips_scan_results
                   (scan_type, total_rules, passed, warnings, breaches, critical,
                    results_json, scanned_by, scanned_at)
                   VALUES ('FULL', :tr, :p, :w, :b, :c, :rj, :sb, :sa)""",
                {"tr": total_rules, "p": passed, "w": warnings, "b": breaches,
                 "c": critical, "rj": json.dumps(results), "sb": user, "sa": now},
            )
        except Exception as e:
            log.error("ips_scan_record_failed", error=str(e))

        # Record breaches
        scan_row = await fetch_one(
            "SELECT id FROM ips_scan_results ORDER BY id DESC LIMIT 1"
        )
        scan_id = scan_row["id"] if scan_row else None

        for r in results:
            if r["status"] != "PASS":
                try:
                    await execute_query(
                        """INSERT INTO ips_breach_log
                           (scan_id, rule_category, rule_key, rule_name, severity, action,
                            limit_value, actual_value, breach_pct, description,
                            affected_isins, detected_at)
                           VALUES (:sid, :cat, :key, :name, :sev, :act,
                                   :lv, :av, :bp, :desc, :isins, :det)""",
                        {
                            "sid": scan_id, "cat": r["category"], "key": r["rule_key"],
                            "name": r["rule_name"], "sev": r["severity"], "act": r.get("action", "ALERT"),
                            "lv": r.get("limit_value"), "av": r.get("actual_value"),
                            "bp": r.get("breach_pct"), "desc": r.get("description"),
                            "isins": json.dumps(r.get("affected_isins", [])),
                            "det": now,
                        },
                    )
                except Exception as e:
                    log.error("ips_breach_log_failed", error=str(e))

        overall_status = "COMPLIANT"
        if critical > 0:
            overall_status = "CRITICAL_BREACH"
        elif breaches > 0:
            overall_status = "BREACH"
        elif warnings > 0:
            overall_status = "WARNING"

        return {
            "status": overall_status,
            "total_rules_checked": total_rules,
            "passed": passed,
            "warnings": warnings,
            "breaches": breaches,
            "critical": critical,
            "portfolio_value": round(total_value, 2),
            "holdings_count": len(holdings),
            "scan_timestamp": now,
            "scanned_by": user,
            "results": results,
        }

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # A. Credit Rating Rules
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def _check_credit_rating_rules(cls, holdings, total_value, rule_map) -> List[dict]:
        results = []

        # Compute rating distribution
        rating_values = defaultdict(float)
        below_aa_value = 0.0
        aaa_value = 0.0
        violations = []
        dual_rating_missing = []

        for h in holdings:
            rating = (h.get("rating") or "NR").upper().strip()
            mv = h.get("market_value", 0) or 0
            rating_values[rating] += mv

            if rating in ("AAA", "SOVEREIGN", "SOV"):
                aaa_value += mv

            if _is_below_aa(rating) and rating not in ("SOVEREIGN", "SOV"):
                below_aa_value += mv

            # Check dual ratings
            dual = await fetch_one(
                "SELECT * FROM dual_ratings WHERE isin = :isin",
                {"isin": h.get("isin", "")},
            )
            if not dual or not dual.get("agency2"):
                dual_rating_missing.append(h.get("isin", ""))

        # Rule: Minimum rating
        rule = rule_map.get(("CREDIT_RATING", "min_rating"))
        if rule:
            min_rating = rule.get("rule_value_text") or "AA-"
            min_rank = _rating_rank(min_rating)
            violation_bonds = []
            for h in holdings:
                rating = (h.get("rating") or "NR").upper().strip()
                if rating in ("SOVEREIGN", "SOV"):
                    continue
                if _rating_rank(rating) > min_rank and rating != "NR":
                    violation_bonds.append({
                        "isin": h.get("isin"),
                        "issuer": h.get("issuer_name", "")[:40],
                        "rating": rating,
                    })

            if violation_bonds:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "min_rating",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(violation_bonds)} bond(s) below minimum rating {min_rating}",
                    "limit_value": min_rank, "actual_value": len(violation_bonds),
                    "affected_isins": [v["isin"] for v in violation_bonds],
                    "details": violation_bonds,
                })
            else:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "min_rating",
                    "rule_name": rule["rule_name"], "status": "PASS",
                    "severity": "INFO",
                    "description": f"All bonds meet minimum rating {min_rating}",
                })

        # Rule: Minimum AAA %
        rule = rule_map.get(("CREDIT_RATING", "min_aaa_pct"))
        if rule:
            aaa_pct = (aaa_value / total_value * 100) if total_value > 0 else 0
            limit = rule["rule_value"] or 60.0
            if aaa_pct < limit:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "min_aaa_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"AAA allocation at {aaa_pct:.1f}% (min {limit}%)",
                    "limit_value": limit, "actual_value": round(aaa_pct, 2),
                    "breach_pct": round(limit - aaa_pct, 2),
                })
            else:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "min_aaa_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"AAA allocation at {aaa_pct:.1f}% (min {limit}%)",
                    "actual_value": round(aaa_pct, 2),
                })

        # Rule: Max below AA %
        rule = rule_map.get(("CREDIT_RATING", "max_below_aa_pct"))
        if rule:
            below_pct = (below_aa_value / total_value * 100) if total_value > 0 else 0
            limit = rule["rule_value"] or 10.0
            if below_pct > limit:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "max_below_aa_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"Below-AA exposure at {below_pct:.1f}% (max {limit}%)",
                    "limit_value": limit, "actual_value": round(below_pct, 2),
                    "breach_pct": round(below_pct - limit, 2),
                })
            else:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "max_below_aa_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"Below-AA exposure at {below_pct:.1f}% (max {limit}%)",
                    "actual_value": round(below_pct, 2),
                })

        # Rule: Dual rating
        rule = rule_map.get(("CREDIT_RATING", "require_dual_rating"))
        if rule and rule["rule_value"] == 1.0:
            if dual_rating_missing:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "require_dual_rating",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(dual_rating_missing)} bond(s) missing dual agency rating",
                    "actual_value": len(dual_rating_missing),
                    "affected_isins": dual_rating_missing[:20],
                })
            else:
                results.append({
                    "category": "CREDIT_RATING", "rule_key": "require_dual_rating",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": "All bonds have dual agency ratings",
                })

        return results

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # B. Concentration Rules
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def _check_concentration_rules(cls, holdings, total_value, rule_map) -> List[dict]:
        results = []
        is_sovereign_exempt = True
        rule = rule_map.get(("ISSUER_CONCENTRATION", "sovereign_exempt"))
        if rule:
            is_sovereign_exempt = (rule.get("rule_value") == 1.0)

        # Build issuer â†’ group mapping from DB
        groups = await fetch_all(
            "SELECT issuer_name, group_name FROM issuer_group_map WHERE is_active = 1"
        )
        issuer_to_group = build_issuer_group_lookup(groups)

        # Compute exposures
        issuer_exposure = defaultdict(lambda: {"value": 0.0, "isins": [], "is_sov": False, "display_issuers": set()})
        group_exposure = defaultdict(lambda: {"value": 0.0, "isins": [], "issuers": set(), "display_issuers": set()})
        sector_exposure = defaultdict(lambda: {"value": 0.0, "isins": []})

        for h in holdings:
            issuer_raw = str(h.get("issuer_name") or "UNKNOWN").strip() or "UNKNOWN"
            issuer = normalize_issuer_name(issuer_raw) or issuer_raw.upper()
            sector = (h.get("sector") or h.get("category") or "OTHER").upper()
            rating = (h.get("rating") or "NR").upper()
            mv = h.get("market_value", 0) or 0
            isin = h.get("isin", "")
            is_sov = rating in ("SOVEREIGN", "SOV") or sector in ("G-SEC", "SDL", "SOVEREIGN")

            issuer_exposure[issuer]["value"] += mv
            issuer_exposure[issuer]["isins"].append(isin)
            issuer_exposure[issuer]["is_sov"] = is_sov
            issuer_exposure[issuer]["display_issuers"].add(issuer_raw.upper())

            sector_exposure[sector]["value"] += mv
            sector_exposure[sector]["isins"].append(isin)

            # Group mapping
            group = resolve_group_name(issuer_raw, issuer_to_group)
            group_exposure[group]["value"] += mv
            group_exposure[group]["isins"].append(isin)
            group_exposure[group]["issuers"].add(issuer)
            group_exposure[group]["display_issuers"].add(issuer_raw.upper())

        # Rule: Single issuer max
        rule = rule_map.get(("ISSUER_CONCENTRATION", "single_issuer_max_pct"))
        if rule:
            limit = rule["rule_value"] or 10.0
            violated = []
            for issuer, data in issuer_exposure.items():
                if is_sovereign_exempt and data["is_sov"]:
                    continue
                pct = data["value"] / total_value * 100
                if pct > limit:
                    violated.append({
                        "issuer": issuer, "pct": round(pct, 2),
                        "value": round(data["value"], 2),
                        "isins": data["isins"],
                        "display_issuers": sorted(data["display_issuers"]),
                    })

            if violated:
                results.append({
                    "category": "ISSUER_CONCENTRATION", "rule_key": "single_issuer_max_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(violated)} issuer(s) exceed {limit}% limit",
                    "limit_value": limit,
                    "actual_value": max(v["pct"] for v in violated),
                    "affected_isins": [i for v in violated for i in v["isins"]],
                    "details": violated,
                })
            else:
                results.append({
                    "category": "ISSUER_CONCENTRATION", "rule_key": "single_issuer_max_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"All issuers within {limit}% limit",
                })

        # Rule: Single group max
        rule = rule_map.get(("ISSUER_CONCENTRATION", "single_group_max_pct"))
        if rule:
            limit = rule["rule_value"] or 15.0
            violated = []
            for group, data in group_exposure.items():
                pct = data["value"] / total_value * 100
                if pct > limit and not is_goi_group(group):
                    violated.append({
                        "group": group, "pct": round(pct, 2),
                        "issuers": list(data["issuers"]),
                        "display_issuers": sorted(data["display_issuers"]),
                        "isins": data["isins"],
                    })

            if violated:
                results.append({
                    "category": "ISSUER_CONCENTRATION", "rule_key": "single_group_max_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(violated)} group(s) exceed {limit}% limit",
                    "limit_value": limit,
                    "actual_value": max(v["pct"] for v in violated),
                    "details": violated,
                })
            else:
                results.append({
                    "category": "ISSUER_CONCENTRATION", "rule_key": "single_group_max_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"All groups within {limit}% limit",
                })

        # Rule: Sector max
        rule = rule_map.get(("ISSUER_CONCENTRATION", "sector_max_pct"))
        if rule:
            limit = rule["rule_value"] or 25.0
            violated = []
            for sector, data in sector_exposure.items():
                pct = data["value"] / total_value * 100
                if pct > limit and sector not in ("G-SEC", "SDL", "SOVEREIGN"):
                    violated.append({
                        "sector": sector, "pct": round(pct, 2),
                        "isins": data["isins"],
                    })

            if violated:
                results.append({
                    "category": "ISSUER_CONCENTRATION", "rule_key": "sector_max_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(violated)} sector(s) exceed {limit}% limit",
                    "limit_value": limit,
                    "actual_value": max(v["pct"] for v in violated),
                    "details": violated,
                })
            else:
                results.append({
                    "category": "ISSUER_CONCENTRATION", "rule_key": "sector_max_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"All sectors within {limit}% limit",
                })

        return results

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # C. Maturity Rules
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def _check_maturity_rules(cls, holdings, total_value, rule_map) -> List[dict]:
        results = []
        today = date.today()

        # Compute maturity data
        maturity_years = []
        short_term_value = 0.0
        year_buckets = defaultdict(float)
        max_mat = 0.0

        for h in holdings:
            mat_str = h.get("maturity_date", "")
            mv = h.get("market_value", 0) or 0
            if not mat_str:
                continue
            try:
                mat_date = datetime.strptime(mat_str[:10], "%Y-%m-%d").date()
                years = (mat_date - today).days / 365.25
                maturity_years.append({"isin": h.get("isin"), "years": years, "mv": mv})
                max_mat = max(max_mat, years)

                if years <= 3:
                    short_term_value += mv

                mat_year = mat_date.year
                year_buckets[mat_year] += mv
            except (ValueError, TypeError):
                pass

        # WAM
        wam = 0.0
        if maturity_years and total_value > 0:
            wam = sum(m["years"] * m["mv"] / total_value for m in maturity_years)

        # Rule: Max maturity
        rule = rule_map.get(("MATURITY", "max_maturity_years"))
        if rule:
            limit = rule["rule_value"] or 10.0
            violators = [m for m in maturity_years if m["years"] > limit]
            if violators:
                results.append({
                    "category": "MATURITY", "rule_key": "max_maturity_years",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(violators)} bond(s) exceed {limit}Y maturity",
                    "limit_value": limit, "actual_value": round(max_mat, 2),
                    "affected_isins": [v["isin"] for v in violators],
                })
            else:
                results.append({
                    "category": "MATURITY", "rule_key": "max_maturity_years",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"Max maturity {max_mat:.1f}Y within {limit}Y limit",
                })

        # Rule: Min short-term %
        rule = rule_map.get(("MATURITY", "min_short_term_pct"))
        if rule:
            limit = rule["rule_value"] or 30.0
            pct = (short_term_value / total_value * 100) if total_value > 0 else 0
            if pct < limit:
                results.append({
                    "category": "MATURITY", "rule_key": "min_short_term_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"Short-term (<3Y) at {pct:.1f}% (min {limit}%)",
                    "limit_value": limit, "actual_value": round(pct, 2),
                })
            else:
                results.append({
                    "category": "MATURITY", "rule_key": "min_short_term_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"Short-term (<3Y) at {pct:.1f}% (min {limit}%)",
                })

        # Rule: Bullet maturity clustering
        rule = rule_map.get(("MATURITY", "max_bullet_cluster_pct"))
        if rule:
            limit = rule["rule_value"] or 20.0
            clustered = []
            for year, val in year_buckets.items():
                pct = val / total_value * 100 if total_value > 0 else 0
                if pct > limit:
                    clustered.append({"year": year, "pct": round(pct, 2)})

            if clustered:
                results.append({
                    "category": "MATURITY", "rule_key": "max_bullet_cluster_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"Maturity clustering detected in {len(clustered)} year(s)",
                    "limit_value": limit,
                    "actual_value": max(c["pct"] for c in clustered),
                    "details": clustered,
                })
            else:
                results.append({
                    "category": "MATURITY", "rule_key": "max_bullet_cluster_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": "No maturity clustering detected",
                })

        # Rule: WAM max
        rule = rule_map.get(("MATURITY", "weighted_avg_maturity_max"))
        if rule:
            limit = rule["rule_value"] or 7.0
            if wam > limit:
                results.append({
                    "category": "MATURITY", "rule_key": "weighted_avg_maturity_max",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"WAM at {wam:.2f}Y exceeds {limit}Y limit",
                    "limit_value": limit, "actual_value": round(wam, 2),
                })
            else:
                results.append({
                    "category": "MATURITY", "rule_key": "weighted_avg_maturity_max",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"WAM at {wam:.2f}Y within {limit}Y limit",
                })

        return results

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # D. Liquidity Rules
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def _check_liquidity_rules(cls, holdings, total_value, rule_map) -> List[dict]:
        results = []

        liquid_value = 0.0
        illiquid_value = 0.0
        unlisted = []
        low_volume = []

        for h in holdings:
            mv = h.get("market_value", 0) or 0
            isin = h.get("isin", "")
            listing = (h.get("listing_exchange") or "NSE").upper()
            liq_score = h.get("liquidity_score", 50) or 50

            # Check listing
            if listing in ("UNLISTED", "PRIVATE", ""):
                unlisted.append(isin)
                illiquid_value += mv
            elif liq_score >= 40:
                liquid_value += mv
            else:
                illiquid_value += mv

            # Check volume
            volume = h.get("traded_volume", 0) or 0
            rule = rule_map.get(("LIQUIDITY", "min_volume_threshold"))
            if rule and volume < (rule["rule_value"] or 1000000):
                low_volume.append({"isin": isin, "volume": volume})

        # Rule: Listed bonds only
        rule = rule_map.get(("LIQUIDITY", "listed_bonds_only"))
        if rule and rule["rule_value"] == 1.0:
            if unlisted:
                results.append({
                    "category": "LIQUIDITY", "rule_key": "listed_bonds_only",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(unlisted)} unlisted bond(s) in portfolio",
                    "actual_value": len(unlisted),
                    "affected_isins": unlisted,
                })
            else:
                results.append({
                    "category": "LIQUIDITY", "rule_key": "listed_bonds_only",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": "All bonds are listed",
                })

        # Rule: Min liquid %
        rule = rule_map.get(("LIQUIDITY", "min_liquid_pct"))
        if rule:
            limit = rule["rule_value"] or 10.0
            pct = (liquid_value / total_value * 100) if total_value > 0 else 0
            if pct < limit:
                results.append({
                    "category": "LIQUIDITY", "rule_key": "min_liquid_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"Liquid assets at {pct:.1f}% (min {limit}%)",
                    "limit_value": limit, "actual_value": round(pct, 2),
                })
            else:
                results.append({
                    "category": "LIQUIDITY", "rule_key": "min_liquid_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"Liquid assets at {pct:.1f}% (min {limit}%)",
                })

        # Rule: Max illiquid %
        rule = rule_map.get(("LIQUIDITY", "max_illiquid_pct"))
        if rule:
            limit = rule["rule_value"] or 15.0
            pct = (illiquid_value / total_value * 100) if total_value > 0 else 0
            if pct > limit:
                results.append({
                    "category": "LIQUIDITY", "rule_key": "max_illiquid_pct",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"Illiquid exposure at {pct:.1f}% (max {limit}%)",
                    "limit_value": limit, "actual_value": round(pct, 2),
                })
            else:
                results.append({
                    "category": "LIQUIDITY", "rule_key": "max_illiquid_pct",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"Illiquid exposure at {pct:.1f}% (max {limit}%)",
                })

        # Rule: Volume threshold
        rule = rule_map.get(("LIQUIDITY", "min_volume_threshold"))
        if rule and low_volume:
            results.append({
                "category": "LIQUIDITY", "rule_key": "min_volume_threshold",
                "rule_name": rule["rule_name"], "status": "BREACH",
                "severity": rule["severity"], "action": rule["action_on_breach"],
                "description": f"{len(low_volume)} bond(s) below volume threshold",
                "actual_value": len(low_volume),
                "affected_isins": [v["isin"] for v in low_volume],
            })
        elif rule:
            results.append({
                "category": "LIQUIDITY", "rule_key": "min_volume_threshold",
                "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                "description": "All bonds meet volume threshold",
            })

        return results

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # E. Duration Rules
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def _check_duration_rules(cls, holdings, total_value, rule_map) -> List[dict]:
        results = []

        portfolio_duration = 0.0
        high_duration_bonds = []

        for h in holdings:
            mv = h.get("market_value", 0) or 0
            dur = h.get("modified_duration", 0) or h.get("years_to_maturity", 3)
            weight = mv / total_value if total_value > 0 else 0
            portfolio_duration += dur * weight

            rule = rule_map.get(("DURATION", "max_single_bond_duration"))
            if rule and dur > (rule["rule_value"] or 10.0):
                high_duration_bonds.append({
                    "isin": h.get("isin"), "duration": round(dur, 2),
                })

        # Rule: Max portfolio duration
        rule = rule_map.get(("DURATION", "max_portfolio_duration"))
        if rule:
            limit = rule["rule_value"] or 6.0
            if portfolio_duration > limit:
                results.append({
                    "category": "DURATION", "rule_key": "max_portfolio_duration",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"Portfolio duration {portfolio_duration:.2f}Y exceeds {limit}Y",
                    "limit_value": limit, "actual_value": round(portfolio_duration, 2),
                })
            else:
                results.append({
                    "category": "DURATION", "rule_key": "max_portfolio_duration",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": f"Portfolio duration {portfolio_duration:.2f}Y within {limit}Y",
                })

        # Rule: Max single bond duration
        rule = rule_map.get(("DURATION", "max_single_bond_duration"))
        if rule:
            if high_duration_bonds:
                results.append({
                    "category": "DURATION", "rule_key": "max_single_bond_duration",
                    "rule_name": rule["rule_name"], "status": "BREACH",
                    "severity": rule["severity"], "action": rule["action_on_breach"],
                    "description": f"{len(high_duration_bonds)} bond(s) exceed duration limit",
                    "actual_value": max(b["duration"] for b in high_duration_bonds),
                    "limit_value": rule["rule_value"],
                    "affected_isins": [b["isin"] for b in high_duration_bonds],
                })
            else:
                results.append({
                    "category": "DURATION", "rule_key": "max_single_bond_duration",
                    "rule_name": rule["rule_name"], "status": "PASS", "severity": "INFO",
                    "description": "All bonds within duration limit",
                })

        return results

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Helpers
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    @classmethod
    async def _get_portfolio_holdings(cls) -> List[dict]:
        """Fetch portfolio holdings enriched with market data."""
        holdings = await fetch_all(
            """SELECT p.*, b.issuer_type, b.coupon_type, b.coupon_frequency,
                      b.listing_exchange, b.sector as bond_sector,
                      m.clean_price, m.ytm, m.modified_duration, m.liquidity_score,
                      m.traded_volume, m.spread_over_gsec
               FROM portfolio_current_positions p
               LEFT JOIN universal_bond_master b ON p.isin = b.isin
               LEFT JOIN unified_market_data m ON p.isin = m.isin
                   AND m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = p.isin)
               WHERE p.quantity > 0"""
        )

        for h in holdings:
            if not h.get("market_value") or h["market_value"] <= 0:
                clean = h.get("clean_price")
                qty = h.get("quantity", 0)
                fv = h.get("face_value", 1000)
                if clean and clean > 0:
                    h["market_value"] = clean / 100 * fv * qty
                else:
                    h["market_value"] = fv * qty

            if not h.get("sector"):
                h["sector"] = h.get("bond_sector") or h.get("category") or "OTHER"

        return holdings

    @classmethod
    async def get_scan_history(cls, days: int = 90, limit: int = 50) -> List[dict]:
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        return await fetch_all(
            """SELECT id, scan_type, total_rules, passed, warnings, breaches, critical,
                      scanned_by, scanned_at
               FROM ips_scan_results WHERE scanned_at >= :cutoff
               ORDER BY scanned_at DESC LIMIT :lim""",
            {"cutoff": cutoff, "lim": limit},
        )

    @classmethod
    async def get_breach_log(cls, status: Optional[str] = None,
                              category: Optional[str] = None,
                              days: int = 90) -> List[dict]:
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        conditions = ["detected_at >= :cutoff"]
        params: dict = {"cutoff": cutoff}
        if status:
            conditions.append("status = :st")
            params["st"] = status
        if category:
            conditions.append("rule_category = :cat")
            params["cat"] = category
        where = " AND ".join(conditions)
        return await fetch_all(
            f"SELECT * FROM ips_breach_log WHERE {where} ORDER BY detected_at DESC LIMIT 200",
            params,
        )

    @classmethod
    async def store_dual_rating(cls, isin: str, agency1: str, rating1: str,
                                 agency2: Optional[str] = None,
                                 rating2: Optional[str] = None) -> dict:
        """Store dual rating using lowest (conservative) method."""
        effective = _lowest_rating(rating1, rating2 or "") if rating2 else rating1
        now = utcnow_iso()

        await execute_query(
            """INSERT OR REPLACE INTO dual_ratings
               (isin, agency1, rating1, agency2, rating2, effective_rating, method, updated_at)
               VALUES (:isin, :a1, :r1, :a2, :r2, :eff, 'LOWEST', :now)""",
            {"isin": isin, "a1": agency1, "r1": rating1, "a2": agency2,
             "r2": rating2, "eff": effective, "now": now},
        )

        return {
            "isin": isin,
            "agency1": agency1, "rating1": rating1,
            "agency2": agency2, "rating2": rating2,
            "effective_rating": effective,
            "method": "LOWEST",
        }

    @classmethod
    async def record_rating_change(cls, isin: str, agency: str,
                                    old_rating: str, new_rating: str) -> dict:
        """Record a rating change and trigger alert if downgrade."""
        now = utcnow_iso()
        change_type = "UPGRADE" if _rating_rank(new_rating) < _rating_rank(old_rating) else "DOWNGRADE"

        is_downgrade = change_type == "DOWNGRADE"
        try:
            await execute_query(
                """INSERT OR IGNORE INTO rating_history
                   (isin, agency, old_rating, new_rating, change_type, change_date,
                    detected_at, alert_generated)
                   VALUES (:isin, :agency, :old, :new, :ct, :cd, :det, :ag)""",
                {"isin": isin, "agency": agency, "old": old_rating, "new": new_rating,
                 "ct": change_type, "cd": now, "det": now, "ag": 1 if is_downgrade else 0},
            )
        except Exception as e:
            log.error("rating_change_record_failed", error=str(e))

        # Generate alert for downgrade
        if is_downgrade:
            try:
                bond_row = await fetch_one(
                    """SELECT issuer_name, issuer_type, credit_rating, instrument_category
                       FROM universal_bond_master
                       WHERE isin = :isin""",
                    {"isin": isin},
                ) or {}
                derived_category = classify_instrument(
                    {
                        "issuer_name": bond_row.get("issuer_name") or isin,
                        "issuer_type": bond_row.get("issuer_type"),
                        "category": bond_row.get("instrument_category") or bond_row.get("credit_rating"),
                        "bond_name": bond_row.get("issuer_name") or isin,
                    }
                ).get("instrument_category")
                await execute_query(
                    """INSERT INTO unified_alerts
                       (alert_date, isin, issuer_name, issuer_type, credit_rating, instrument_category,
                        alert_type, severity, title, detail, created_at)
                       VALUES (:ad, :isin, :issuer_name, :issuer_type, :credit_rating, :instrument_category,
                               'RATING_DOWNGRADE', 'CRITICAL',
                               :title, :detail, :now)""",
                    {"ad": now[:10], "isin": isin, "now": now,
                     "issuer_name": bond_row.get("issuer_name"),
                     "issuer_type": bond_row.get("issuer_type"),
                     "credit_rating": bond_row.get("credit_rating") or new_rating,
                     "instrument_category": str(bond_row.get("instrument_category") or derived_category or "PRIVATE").upper(),
                     "title": f"Rating Downgrade: {isin} {old_rating}â†’{new_rating} ({agency})",
                     "detail": json.dumps({
                         "agency": agency, "old": old_rating, "new": new_rating,
                         "isin": isin, "type": "DOWNGRADE",
                     })},
                )
            except Exception as e:
                log.error("rating_alert_failed", error=str(e))

        return {
            "isin": isin, "agency": agency,
            "old_rating": old_rating, "new_rating": new_rating,
            "change_type": change_type,
            "alert_generated": is_downgrade,
        }

