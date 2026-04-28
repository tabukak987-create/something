"""
Compliance Rule Versioning & Regulatory Change Management Service
=================================================================
Manages versioned compliance rules with full audit trail, SEBI circular
ingestion pipeline, and conflict resolution framework.

Regulatory coverage:
  - SEBI CSCRF (Cybersecurity & Cyber Resilience Framework)
  - SEBI IA Regulations 2013 (amended 2020)
  - PFRDA Investment Guidelines (Gazette notifications)
  - RBI Sovereign bond exposure norms
  - IRDAI exposure limits
  - NPS Trust / EPFO PF trust investment constraints

Author: Automated Compliance Engine v2.0
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from app.shared.time_utils import utcnow_iso

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent.parent / "data" / "compliance_rules.db"
CIRCULAR_INBOX = Path(__file__).parent.parent / "data" / "regulatory_circulars"
REPORTS_DIR = Path(__file__).parent.parent.parent / "reports" / "compliance"

CIRCULAR_INBOX.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class Regulator(str, Enum):
    SEBI     = "SEBI"
    PFRDA    = "PFRDA"
    RBI      = "RBI"
    IRDAI    = "IRDAI"
    MCA      = "MCA"
    FEMA     = "FEMA"
    AMFI     = "AMFI"


class RuleCategory(str, Enum):
    EXPOSURE_LIMIT    = "EXPOSURE_LIMIT"
    CREDIT_QUALITY    = "CREDIT_QUALITY"
    DURATION_LIMIT    = "DURATION_LIMIT"
    LIQUIDITY         = "LIQUIDITY"
    CONCENTRATION     = "CONCENTRATION"
    COUNTERPARTY      = "COUNTERPARTY"
    REPORTING         = "REPORTING"
    CUSTODY           = "CUSTODY"
    GOVERNANCE        = "GOVERNANCE"
    CYBERSECURITY     = "CYBERSECURITY"


class ChangeType(str, Enum):
    NEW_RULE          = "NEW_RULE"
    AMENDMENT         = "AMENDMENT"
    WITHDRAWAL        = "WITHDRAWAL"
    CLARIFICATION     = "CLARIFICATION"
    EMERGENCY         = "EMERGENCY"


class ConflictResolution(str, Enum):
    LATEST_WINS       = "LATEST_WINS"     # Newer regulation supersedes
    MORE_RESTRICTIVE  = "MORE_RESTRICTIVE" # Always apply tighter constraint
    REGULATOR_PRIORITY = "REGULATOR_PRIORITY"  # SEBI > PFRDA > RBI > IRDAI
    EXPLICIT_OVERRIDE  = "EXPLICIT_OVERRIDE"   # Manually designated


REGULATOR_PRIORITY: dict[str, int] = {
    "SEBI": 1, "PFRDA": 2, "RBI": 3, "IRDAI": 4,
    "MCA": 5, "FEMA": 6, "AMFI": 7,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class ComplianceRule:
    """A single versioned compliance rule."""
    rule_id:        str          # e.g. "CR-SEBI-2024-001"
    version:        int          # 1, 2, 3 …
    regulator:      str          # Regulator enum value
    category:       str          # RuleCategory enum value
    title:          str          # Short descriptive title
    description:    str          # Full regulatory text
    numeric_limit:  Optional[float] = None   # Percentage / bps / years
    limit_unit:     str = ""                 # "%" / "bps" / "years" / "INR_CR"
    effective_date: str = ""                 # ISO 8601
    expiry_date:    Optional[str] = None
    circular_ref:   str = ""                 # e.g. "SEBI/HO/IMD/DF2/CIR/P/2024/0001"
    is_active:      bool = True
    is_superseded:  bool = False
    superseded_by:  Optional[str] = None
    change_type:    str = ChangeType.NEW_RULE
    change_rationale: str = ""
    created_at:     str = field(default_factory=lambda: _now())
    created_by:     str = "system"
    approved_by:    str = ""
    checksum:       str = ""     # SHA-256 of canonical form — tamper detection

    def compute_checksum(self) -> str:
        canonical = json.dumps({
            "rule_id": self.rule_id, "version": self.version,
            "regulator": self.regulator, "category": self.category,
            "title": self.title, "description": self.description,
            "numeric_limit": self.numeric_limit, "limit_unit": self.limit_unit,
            "effective_date": self.effective_date, "circular_ref": self.circular_ref,
        }, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class RegulatoryCircular:
    """Parsed SEBI / PFRDA / RBI circular."""
    circular_id:    str
    regulator:      str
    date_issued:    str
    subject:        str
    url:            str
    raw_text:       str
    extracted_rules: list[dict] = field(default_factory=list)  # Partially parsed rules
    parsed_at:      str = field(default_factory=lambda: _now())
    status:         str = "PENDING"  # PENDING / REVIEWED / INGESTED / REJECTED


@dataclass
class RuleConflict:
    """Detected conflict between two or more rules."""
    conflict_id:     str
    rule_ids:        list[str]
    conflict_type:   str   # "NUMERIC_GAP" / "OVERLAP" / "CONTRADICTION" / "REDUNDANT"
    description:     str
    resolution:      Optional[str] = None
    resolved_rule_id: Optional[str] = None
    detected_at:     str = field(default_factory=lambda: _now())
    resolved_at:     Optional[str] = None
    resolved_by:     str = ""


@dataclass
class ComplianceChangeLog:
    """Immutable audit trail entry for every rule change."""
    log_id:      str
    rule_id:     str
    version:     int
    change_type: str
    summary:     str
    diff_json:   str   # JSON diff between old and new rule
    changed_at:  str = field(default_factory=lambda: _now())
    changed_by:  str = "system"
    ip_address:  str = ""
    checksum:    str = ""  # SHA-256 of the log entry itself


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return utcnow_iso()


def _rule_id(regulator: str, year: int, seq: int) -> str:
    return f"CR-{regulator}-{year}-{seq:03d}"


def _diff(old: dict, new: dict) -> dict:
    """Return fields that changed between two rule dicts."""
    changes = {}
    all_keys = set(old) | set(new)
    for k in all_keys:
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            changes[k] = {"old": ov, "new": nv}
    return changes


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS compliance_rules (
    rule_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    regulator       TEXT NOT NULL,
    category        TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    numeric_limit   REAL,
    limit_unit      TEXT DEFAULT '',
    effective_date  TEXT DEFAULT '',
    expiry_date     TEXT,
    circular_ref    TEXT DEFAULT '',
    is_active       INTEGER DEFAULT 1,
    is_superseded   INTEGER DEFAULT 0,
    superseded_by   TEXT,
    change_type     TEXT DEFAULT 'NEW_RULE',
    change_rationale TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    created_by      TEXT DEFAULT 'system',
    approved_by     TEXT DEFAULT '',
    checksum        TEXT DEFAULT '',
    PRIMARY KEY (rule_id, version)
);

CREATE TABLE IF NOT EXISTS regulatory_circulars (
    circular_id     TEXT PRIMARY KEY,
    regulator       TEXT NOT NULL,
    date_issued     TEXT NOT NULL,
    subject         TEXT NOT NULL,
    url             TEXT,
    raw_text        TEXT,
    extracted_rules TEXT DEFAULT '[]',
    parsed_at       TEXT NOT NULL,
    status          TEXT DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS rule_conflicts (
    conflict_id     TEXT PRIMARY KEY,
    rule_ids        TEXT NOT NULL,
    conflict_type   TEXT NOT NULL,
    description     TEXT NOT NULL,
    resolution      TEXT,
    resolved_rule_id TEXT,
    detected_at     TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS compliance_changelog (
    log_id      TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    summary     TEXT NOT NULL,
    diff_json   TEXT DEFAULT '{}',
    changed_at  TEXT NOT NULL,
    changed_by  TEXT DEFAULT 'system',
    ip_address  TEXT DEFAULT '',
    checksum    TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_rules_regulator   ON compliance_rules(regulator);
CREATE INDEX IF NOT EXISTS idx_rules_category    ON compliance_rules(category);
CREATE INDEX IF NOT EXISTS idx_rules_active      ON compliance_rules(is_active);
CREATE INDEX IF NOT EXISTS idx_changelog_rule_id ON compliance_changelog(rule_id);
"""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------
class ComplianceRuleRepository:
    """SQLite-backed rule store. Upgrade to PostgreSQL for production."""

    def __init__(self, db_path: Path = DB_PATH):
        self._db = str(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        return conn

    # --- Rules ---

    def upsert_rule(self, rule: ComplianceRule, changed_by: str = "system") -> ComplianceRule:
        rule.checksum = rule.compute_checksum()
        existing = self.get_latest(rule.rule_id)
        if existing:
            diff = _diff(asdict(existing), asdict(rule))
            rule.version = existing.version + 1
        else:
            diff = {}
            rule.version = 1

        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO compliance_rules VALUES (
                    :rule_id,:version,:regulator,:category,:title,:description,
                    :numeric_limit,:limit_unit,:effective_date,:expiry_date,
                    :circular_ref,:is_active,:is_superseded,:superseded_by,
                    :change_type,:change_rationale,:created_at,:created_by,
                    :approved_by,:checksum
                )""",
                asdict(rule),
            )
            # Changelog entry
            log_id = hashlib.md5(f"{rule.rule_id}{rule.version}{_now()}".encode()).hexdigest()[:16]
            log_entry_data = {
                "log_id": log_id, "rule_id": rule.rule_id, "version": rule.version,
                "change_type": rule.change_type,
                "summary": f"v{rule.version}: {rule.change_type} — {rule.title}",
                "diff_json": json.dumps(diff), "changed_at": _now(),
                "changed_by": changed_by, "ip_address": "", "checksum": "",
            }
            log_entry_checksum = hashlib.sha256(json.dumps(log_entry_data, sort_keys=True).encode()).hexdigest()
            log_entry_data["checksum"] = log_entry_checksum
            conn.execute(
                """INSERT OR IGNORE INTO compliance_changelog VALUES (
                    :log_id,:rule_id,:version,:change_type,:summary,:diff_json,
                    :changed_at,:changed_by,:ip_address,:checksum
                )""",
                log_entry_data,
            )
        return rule

    def get_latest(self, rule_id: str) -> Optional[ComplianceRule]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM compliance_rules WHERE rule_id=? ORDER BY version DESC LIMIT 1",
                (rule_id,)
            ).fetchone()
        return self._row_to_rule(row) if row else None

    def get_version(self, rule_id: str, version: int) -> Optional[ComplianceRule]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM compliance_rules WHERE rule_id=? AND version=?",
                (rule_id, version)
            ).fetchone()
        return self._row_to_rule(row) if row else None

    def list_active_rules(
        self,
        regulator: Optional[str] = None,
        category: Optional[str] = None,
    ) -> list[ComplianceRule]:
        sql = "SELECT * FROM compliance_rules WHERE is_active=1 AND is_superseded=0"
        params: list = []
        if regulator:
            sql += " AND regulator=?"; params.append(regulator)
        if category:
            sql += " AND category=?"; params.append(category)
        sql += " ORDER BY regulator, category, rule_id"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def get_history(self, rule_id: str) -> list[ComplianceRule]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM compliance_rules WHERE rule_id=? ORDER BY version",
                (rule_id,)
            ).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def supersede_rule(self, old_rule_id: str, new_rule_id: str, changed_by: str = "system") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE compliance_rules SET is_superseded=1, superseded_by=? WHERE rule_id=?",
                (new_rule_id, old_rule_id),
            )

    def save_conflict(self, conflict: RuleConflict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO rule_conflicts VALUES (
                    :conflict_id,:rule_ids,:conflict_type,:description,
                    :resolution,:resolved_rule_id,:detected_at,:resolved_at,:resolved_by
                )""",
                {**asdict(conflict), "rule_ids": json.dumps(conflict.rule_ids)},
            )

    def list_conflicts(self, unresolved_only: bool = True) -> list[RuleConflict]:
        sql = "SELECT * FROM rule_conflicts"
        if unresolved_only:
            sql += " WHERE resolution IS NULL"
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._row_to_conflict(r) for r in rows]

    def save_circular(self, circular: RegulatoryCircular) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO regulatory_circulars VALUES (
                    :circular_id,:regulator,:date_issued,:subject,:url,:raw_text,
                    :extracted_rules,:parsed_at,:status
                )""",
                {**asdict(circular), "extracted_rules": json.dumps(circular.extracted_rules)},
            )

    def get_changelog(self, rule_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM compliance_changelog WHERE rule_id=? ORDER BY version",
                (rule_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Helpers ---

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> ComplianceRule:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_superseded"] = bool(d["is_superseded"])
        return ComplianceRule(**d)

    @staticmethod
    def _row_to_conflict(row: sqlite3.Row) -> RuleConflict:
        d = dict(row)
        d["rule_ids"] = json.loads(d["rule_ids"])
        return RuleConflict(**d)


# ---------------------------------------------------------------------------
# Conflict detector
# ---------------------------------------------------------------------------
class ConflictDetector:
    """Detects and resolves rule conflicts using configurable strategies."""

    def __init__(self, repo: ComplianceRuleRepository):
        self.repo = repo

    def detect_all(self) -> list[RuleConflict]:
        """Scan active rules for conflicts. Returns newly detected conflicts."""
        rules = self.repo.list_active_rules()
        conflicts: list[RuleConflict] = []

        # Group by category for O(n²) comparison within groups
        by_cat: dict[str, list[ComplianceRule]] = {}
        for r in rules:
            by_cat.setdefault(r.category, []).append(r)

        for cat, cat_rules in by_cat.items():
            for i, r1 in enumerate(cat_rules):
                for r2 in cat_rules[i + 1:]:
                    conflict = self._compare(r1, r2)
                    if conflict:
                        conflicts.append(conflict)
                        self.repo.save_conflict(conflict)

        return conflicts

    def _compare(self, r1: ComplianceRule, r2: ComplianceRule) -> Optional[RuleConflict]:
        if r1.rule_id == r2.rule_id:
            return None
        if r1.numeric_limit is None or r2.numeric_limit is None:
            return None

        cid = hashlib.md5(f"{r1.rule_id}{r2.rule_id}".encode()).hexdigest()[:12]

        # Same category, different regulators, different numeric limits
        if r1.category == r2.category and r1.regulator != r2.regulator:
            gap_pct = abs(r1.numeric_limit - r2.numeric_limit) / max(r1.numeric_limit, r2.numeric_limit, 0.001) * 100
            if gap_pct > 10:  # >10% gap is a conflict
                return RuleConflict(
                    conflict_id=f"CONF-{cid}",
                    rule_ids=[r1.rule_id, r2.rule_id],
                    conflict_type="NUMERIC_GAP",
                    description=(
                        f"{r1.regulator} sets {r1.numeric_limit}{r1.limit_unit} "
                        f"vs {r2.regulator} sets {r2.numeric_limit}{r2.limit_unit} "
                        f"for {r1.category} — {gap_pct:.0f}% gap"
                    ),
                )

        # Contradictory limits (one says max X, other says min Y where Y > X)
        if (r1.title.lower().startswith("max") and r2.title.lower().startswith("min")
                and r1.numeric_limit < r2.numeric_limit):
            return RuleConflict(
                conflict_id=f"CONF-{cid}",
                rule_ids=[r1.rule_id, r2.rule_id],
                conflict_type="CONTRADICTION",
                description=(
                    f"Impossible: {r1.rule_id} (max={r1.numeric_limit}) "
                    f"< {r2.rule_id} (min={r2.numeric_limit})"
                ),
            )
        return None

    def auto_resolve(
        self,
        conflict: RuleConflict,
        strategy: ConflictResolution = ConflictResolution.MORE_RESTRICTIVE,
    ) -> Optional[str]:
        """
        Apply resolution strategy. Returns the winning rule_id.
        """
        rules = [self.repo.get_latest(rid) for rid in conflict.rule_ids]
        rules = [r for r in rules if r is not None]
        if not rules:
            return None

        winner: Optional[ComplianceRule] = None

        if strategy == ConflictResolution.MORE_RESTRICTIVE:
            # Lower numeric limit = more restrictive
            winner = min(rules, key=lambda r: r.numeric_limit or float("inf"))

        elif strategy == ConflictResolution.LATEST_WINS:
            winner = max(rules, key=lambda r: r.effective_date or "")

        elif strategy == ConflictResolution.REGULATOR_PRIORITY:
            winner = min(rules, key=lambda r: REGULATOR_PRIORITY.get(r.regulator, 99))

        elif strategy == ConflictResolution.EXPLICIT_OVERRIDE:
            return None  # Requires manual intervention

        if winner:
            with self.repo._conn() as conn:
                conn.execute(
                    "UPDATE rule_conflicts SET resolution=?, resolved_rule_id=?, resolved_at=? WHERE conflict_id=?",
                    (strategy.value, winner.rule_id, _now(), conflict.conflict_id),
                )
        return winner.rule_id if winner else None


# ---------------------------------------------------------------------------
# SEBI circular ingestion pipeline
# ---------------------------------------------------------------------------
class SEBICircularParser:
    """
    Parses SEBI / PFRDA circulars (text or structured JSON) and extracts
    compliance rule candidates for human review.

    Production integration points:
      - SEBI SCORES API: https://scores.sebi.gov.in/scores/Welcome.html
      - SEBI circular RSS: https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes&sid=2
      - PFRDA circular page: https://www.pfrda.org.in/index.cshtml?pageid=49
    """

    # Patterns for extracting numeric limits from circular text
    LIMIT_PATTERNS = [
        r"(?:not exceeding|shall not exceed|maximum of|up to)\s+(\d+(?:\.\d+)?)\s*(?:per cent|%)",
        r"(\d+(?:\.\d+)?)\s*(?:per cent|%)\s*of (?:the )?(?:total|net|corpus)",
        r"minimum (?:of\s+)?(\d+(?:\.\d+)?)\s*(?:per cent|%)",
        r"(?:credit rating of|rated)\s+(?:at least\s+)?([A-Z]+(?:\+|-)?)",
    ]

    def parse_circular_text(self, text: str, circular_id: str, regulator: str) -> list[dict]:
        """Extract candidate rules from circular text (NLP-lite pattern matching)."""
        candidates = []
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 50]

        for i, para in enumerate(paragraphs):
            for pattern in self.LIMIT_PATTERNS:
                m = re.search(pattern, para, re.IGNORECASE)
                if m:
                    candidates.append({
                        "candidate_id": f"{circular_id}-RULE-{i+1:02d}",
                        "circular_ref": circular_id,
                        "regulator": regulator,
                        "paragraph": para[:300],
                        "extracted_value": m.group(1),
                        "pattern_matched": pattern,
                        "review_status": "PENDING",
                        "suggested_category": self._infer_category(para),
                    })
                    break

        return candidates

    @staticmethod
    def _infer_category(text: str) -> str:
        """Heuristic category inference from paragraph text."""
        text_lower = text.lower()
        if any(w in text_lower for w in ["exposure", "invested", "investment"]):
            return RuleCategory.EXPOSURE_LIMIT
        if any(w in text_lower for w in ["credit rating", "rated", "aaa", "aa+"]):
            return RuleCategory.CREDIT_QUALITY
        if any(w in text_lower for w in ["duration", "maturity", "years"]):
            return RuleCategory.DURATION_LIMIT
        if any(w in text_lower for w in ["liquidity", "liquid"]):
            return RuleCategory.LIQUIDITY
        if any(w in text_lower for w in ["single issuer", "concentration", "sector"]):
            return RuleCategory.CONCENTRATION
        if any(w in text_lower for w in ["report", "disclosure", "submit"]):
            return RuleCategory.REPORTING
        return RuleCategory.GOVERNANCE


# ---------------------------------------------------------------------------
# Pre-loaded seed rules (PFRDA/SEBI investment guidelines)
# ---------------------------------------------------------------------------
SEED_RULES: list[dict] = [
    # ── SEBI IA Regulations ──
    dict(
        rule_id="CR-SEBI-2024-001", regulator="SEBI", category="EXPOSURE_LIMIT",
        title="Max corporate bond exposure per issuer (IA)",
        description=(
            "An Investment Adviser's model portfolio shall not have more than 10% of "
            "the total portfolio value exposed to bonds issued by a single corporate entity. "
            "Ref: SEBI/HO/IMD/DF2/CIR/P/2024/0001"
        ),
        numeric_limit=10.0, limit_unit="%", effective_date="2024-01-01",
        circular_ref="SEBI/HO/IMD/DF2/CIR/P/2024/0001",
        change_type="NEW_RULE",
        change_rationale="SEBI IA Regulation amendment — enhanced concentration limits",
    ),
    dict(
        rule_id="CR-SEBI-2024-002", regulator="SEBI", category="CREDIT_QUALITY",
        title="Minimum investment grade for bond recommendations",
        description=(
            "Bonds recommended to retail investors must carry a minimum long-term credit "
            "rating of BBB+ (Indian scale) from at least one SEBI-registered CRA. "
            "Below-investment-grade bonds require explicit risk disclosure."
        ),
        numeric_limit=None, limit_unit="", effective_date="2024-01-01",
        circular_ref="SEBI/HO/IMD/DF2/CIR/P/2024/0002",
        change_type="NEW_RULE",
        change_rationale="Investor protection — minimum credit quality gate",
    ),
    dict(
        rule_id="CR-SEBI-2024-003", regulator="SEBI", category="DURATION_LIMIT",
        title="Max modified duration for retail bond portfolio",
        description=(
            "Bonds recommended to retail / conservative investors within an IA platform "
            "shall have a weighted average modified duration not exceeding 7 years, unless "
            "an explicit risk waiver is obtained."
        ),
        numeric_limit=7.0, limit_unit="years", effective_date="2024-01-01",
        circular_ref="SEBI/HO/IMD/DF2/CIR/P/2024/0003",
        change_type="NEW_RULE",
        change_rationale="Duration risk management for retail investors",
    ),
    # ── PFRDA (NPS / PF Trust) ──
    dict(
        rule_id="CR-PFRDA-2024-001", regulator="PFRDA", category="EXPOSURE_LIMIT",
        title="Max equity exposure for PF Trust (Tier-2)",
        description=(
            "Provident fund trusts under PFRDA purview may not allocate more than 15% of "
            "total corpus to equity-oriented assets. The remaining corpus must be in "
            "government securities, AAA-rated corporate bonds, or liquid funds."
        ),
        numeric_limit=15.0, limit_unit="%", effective_date="2024-07-01",
        circular_ref="PFRDA/2024/03/15/equity-limits",
        change_type="NEW_RULE",
        change_rationale="PFRDA investment guidelines update — July 2024",
    ),
    dict(
        rule_id="CR-PFRDA-2024-002", regulator="PFRDA", category="CONCENTRATION",
        title="Single-group entity exposure cap for PF Trust",
        description=(
            "Total exposure to a single business group (including all subsidiaries and "
            "associate companies) shall not exceed 15% of the fund corpus."
        ),
        numeric_limit=15.0, limit_unit="%", effective_date="2024-07-01",
        circular_ref="PFRDA/2024/03/15/concentration-limits",
        change_type="NEW_RULE",
        change_rationale="Group-level concentration risk management",
    ),
    dict(
        rule_id="CR-PFRDA-2024-003", regulator="PFRDA", category="CREDIT_QUALITY",
        title="Minimum credit rating for PF Trust bond investments",
        description=(
            "PF Trust bond portfolio must maintain a weighted average credit rating "
            "equivalent to AA or above on the Indian domestic scale. "
            "Minimum individual bond rating: A."
        ),
        numeric_limit=None, limit_unit="", effective_date="2024-01-01",
        circular_ref="PFRDA/2024/01/10/credit-quality",
        change_type="NEW_RULE",
        change_rationale="Capital preservation mandate for retirement corpus",
    ),
    # ── RBI ──
    dict(
        rule_id="CR-RBI-2024-001", regulator="RBI", category="COUNTERPARTY",
        title="Max interbank / counterparty exposure",
        description=(
            "Regulated financial entities acting as trustees shall limit exposure to any "
            "single bank counterparty to 10% of portfolio NAV for settlement or custody "
            "purposes. Exemption: RBI-designated systemically important banks (up to 20%)."
        ),
        numeric_limit=10.0, limit_unit="%", effective_date="2024-03-01",
        circular_ref="RBI/2024-25/18",
        change_type="NEW_RULE",
        change_rationale="Counterparty risk mitigation post-SVB stress events",
    ),
    # ── SEBI CSCRF ──
    dict(
        rule_id="CR-SEBI-2024-004", regulator="SEBI", category="CYBERSECURITY",
        title="SEBI CSCRF — RTO for critical securities infrastructure",
        description=(
            "SEBI Circular SEBI/HO/ITD/ITDCS/CIR/P/2023/204 mandates Market Infrastructure "
            "Institutions (MII) and Qualified REs to achieve Recovery Time Objective (RTO) "
            "of ≤4 hours and Recovery Point Objective (RPO) of ≤1 hour for Tier-1 systems."
        ),
        numeric_limit=4.0, limit_unit="hours (RTO)", effective_date="2024-01-01",
        circular_ref="SEBI/HO/ITD/ITDCS/CIR/P/2023/204",
        change_type="NEW_RULE",
        change_rationale="SEBI Cybersecurity and Cyber Resilience Framework (CSCRF)",
    ),
    dict(
        rule_id="CR-SEBI-2024-005", regulator="SEBI", category="CYBERSECURITY",
        title="SEBI CSCRF — Vulnerability assessment frequency",
        description=(
            "Qualified Regulated Entities under SEBI CSCRF must conduct: "
            "(a) automated vulnerability scanning at least weekly, "
            "(b) quarterly VAPT by CERT-In empanelled auditor, "
            "(c) annual red-team exercise for Tier-1 systems."
        ),
        numeric_limit=None, limit_unit="", effective_date="2024-01-01",
        circular_ref="SEBI/HO/ITD/ITDCS/CIR/P/2023/204",
        change_type="NEW_RULE",
        change_rationale="SEBI CSCRF — security assessment requirements",
    ),
]


# ---------------------------------------------------------------------------
# Service facade
# ---------------------------------------------------------------------------
class ComplianceRuleVersioningService:
    """
    High-level service for all compliance rule operations.
    Integrates repository, conflict detector, and circular parser.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.repo = ComplianceRuleRepository(db_path)
        self.detector = ConflictDetector(self.repo)
        self.parser = SEBICircularParser()

    # --- Rule management ---

    def add_rule(self, rule_data: dict, changed_by: str = "system") -> ComplianceRule:
        rule = ComplianceRule(**rule_data)
        return self.repo.upsert_rule(rule, changed_by)

    def amend_rule(self, rule_id: str, changes: dict, rationale: str, changed_by: str) -> ComplianceRule:
        existing = self.repo.get_latest(rule_id)
        if not existing:
            raise ValueError(f"Rule {rule_id} not found")
        updated = asdict(existing)
        updated.update(changes)
        updated["change_type"] = ChangeType.AMENDMENT
        updated["change_rationale"] = rationale
        new_rule = ComplianceRule(**{k: v for k, v in updated.items() if k != "version"})
        return self.repo.upsert_rule(new_rule, changed_by)

    def withdraw_rule(self, rule_id: str, reason: str, changed_by: str) -> None:
        existing = self.repo.get_latest(rule_id)
        if not existing:
            raise ValueError(f"Rule {rule_id} not found")
        updated = asdict(existing)
        updated["is_active"] = False
        updated["change_type"] = ChangeType.WITHDRAWAL
        updated["change_rationale"] = reason
        new_rule = ComplianceRule(**{k: v for k, v in updated.items() if k != "version"})
        self.repo.upsert_rule(new_rule, changed_by)

    def get_rule_history(self, rule_id: str) -> list[dict]:
        history = self.repo.get_history(rule_id)
        changelog = self.repo.get_changelog(rule_id)
        return [
            {**asdict(h), "changelog": [c for c in changelog if c["version"] == h.version]}
            for h in history
        ]

    # --- Conflict management ---

    def run_conflict_scan(self) -> dict:
        conflicts = self.detector.detect_all()
        return {
            "scanned_at": _now(),
            "conflicts_found": len(conflicts),
            "conflicts": [asdict(c) for c in conflicts],
        }

    def resolve_conflict(
        self,
        conflict_id: str,
        strategy: ConflictResolution = ConflictResolution.MORE_RESTRICTIVE,
    ) -> dict:
        unresolved = {c.conflict_id: c for c in self.repo.list_conflicts()}
        if conflict_id not in unresolved:
            raise ValueError(f"Conflict {conflict_id} not found or already resolved")
        winner = self.detector.auto_resolve(unresolved[conflict_id], strategy)
        return {"conflict_id": conflict_id, "winner_rule_id": winner, "strategy": strategy.value}

    # --- Circular ingestion ---

    def ingest_circular(
        self,
        circular_id: str,
        regulator: str,
        date_issued: str,
        subject: str,
        text: str,
        url: str = "",
    ) -> RegulatoryCircular:
        candidates = self.parser.parse_circular_text(text, circular_id, regulator)
        circular = RegulatoryCircular(
            circular_id=circular_id, regulator=regulator, date_issued=date_issued,
            subject=subject, url=url, raw_text=text, extracted_rules=candidates,
        )
        self.repo.save_circular(circular)
        # Save circular text to inbox for audit
        inbox_path = CIRCULAR_INBOX / f"{circular_id.replace('/', '_')}.txt"
        inbox_path.write_text(f"Subject: {subject}\nDate: {date_issued}\n\n{text}", encoding="utf-8")
        log.info("Ingested circular %s — %d rule candidates extracted", circular_id, len(candidates))
        return circular

    # --- Reporting ---

    def generate_rule_matrix(self) -> dict:
        """Produce compliance rule matrix for board pack / audit."""
        rules = self.repo.list_active_rules()
        by_reg: dict[str, list[dict]] = {}
        for r in rules:
            entry = {
                "rule_id": r.rule_id, "version": r.version,
                "category": r.category, "title": r.title,
                "numeric_limit": f"{r.numeric_limit}{r.limit_unit}" if r.numeric_limit else "See desc.",
                "effective_date": r.effective_date, "circular_ref": r.circular_ref,
            }
            by_reg.setdefault(r.regulator, []).append(entry)
        return {
            "generated_at": _now(),
            "total_active_rules": len(rules),
            "by_regulator": by_reg,
            "pending_conflicts": len(self.repo.list_conflicts()),
        }

    def seed_default_rules(self, changed_by: str = "system") -> int:
        """Seed the database with PFRDA/SEBI/RBI default investment constraints."""
        seeded = 0
        for rule_data in SEED_RULES:
            existing = self.repo.get_latest(rule_data["rule_id"])
            if not existing:
                self.add_rule(rule_data, changed_by=changed_by)
                seeded += 1
        log.info("Seeded %d compliance rules", seeded)
        return seeded

    def export_to_json(self, output_path: Optional[Path] = None) -> Path:
        out = output_path or (REPORTS_DIR / f"compliance_rules_{date.today().isoformat()}.json")
        matrix = self.generate_rule_matrix()
        out.write_text(json.dumps(matrix, indent=2, default=str), encoding="utf-8")
        return out


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    ap = argparse.ArgumentParser(description="Compliance Rule Versioning CLI")
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("seed", help="Seed default SEBI/PFRDA/RBI rules")
    sub.add_parser("scan", help="Scan for conflicts")
    sub.add_parser("matrix", help="Print rule matrix")

    args = ap.parse_args()
    svc = ComplianceRuleVersioningService()

    if args.command == "seed":
        n = svc.seed_default_rules(changed_by="cli-admin")
        print(f"Seeded {n} new rules")
        matrix = svc.generate_rule_matrix()
        print(f"Total active rules: {matrix['total_active_rules']}")

    elif args.command == "scan":
        result = svc.run_conflict_scan()
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "matrix":
        matrix = svc.generate_rule_matrix()
        print(json.dumps(matrix, indent=2, default=str))
    else:
        ap.print_help()
