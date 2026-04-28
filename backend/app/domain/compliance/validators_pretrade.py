"""
Pre-Trade Compliance Validation Engine
========================================
Before confirming any investment, the system simulates:
  - New issuer exposure impact
  - Sector exposure impact
  - Rating distribution impact
  - Duration impact
  - Liquidity impact
  - Maturity constraint impact

Produces a structured pass/fail/warning result.
Blocks trades that would cause CRITICAL IPS violations.
Logs overrides when allowed by authorized users.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso, utcnow
from app.domain.compliance.engine import IPSEngine, _rating_rank
from app.domain.core_trust.issuer_identity import (
    build_issuer_group_lookup,
    is_goi_group,
    normalize_issuer_name,
    resolve_group_name,
)

log = get_logger("service.pretrade")


# ── Schema ────────────────────────────────────────────────────────────────────

PRETRADE_DDL = """
CREATE TABLE IF NOT EXISTS pretrade_simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'BUY',
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    trade_value REAL NOT NULL,
    simulation_result TEXT NOT NULL,
    overall_status TEXT NOT NULL,
    violations_json TEXT,
    warnings_json TEXT,
    trade_blocked INTEGER NOT NULL DEFAULT 0,
    override_allowed INTEGER NOT NULL DEFAULT 0,
    override_by TEXT,
    override_reason TEXT,
    override_at TEXT,
    simulated_by TEXT NOT NULL DEFAULT 'system',
    simulated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pts_isin ON pretrade_simulations(isin);
CREATE INDEX IF NOT EXISTS idx_pts_date ON pretrade_simulations(simulated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pts_status ON pretrade_simulations(overall_status);

CREATE TABLE IF NOT EXISTS trade_override_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id INTEGER NOT NULL,
    isin TEXT NOT NULL,
    override_by TEXT NOT NULL,
    override_reason TEXT NOT NULL,
    violations_overridden TEXT,
    approved_by TEXT,
    approval_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tol_sim ON trade_override_log(simulation_id);
CREATE INDEX IF NOT EXISTS idx_tol_date ON trade_override_log(created_at DESC);
"""


class PreTradeValidator:
    """
    Simulates the impact of a proposed trade on portfolio compliance
    before execution. Blocks or warns on IPS violations.
    """

    @classmethod
    async def initialize(cls):
        """Create pre-trade simulation tables."""
        try:
            for stmt in PRETRADE_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await execute_query(stmt + ";")
            log.info("pretrade_validator_initialized")
        except Exception as e:
            log.error("pretrade_init_failed", error=str(e))

    @classmethod
    async def simulate_trade(cls, isin: str, action: str, quantity: int,
                              price: float, user: str = "system") -> dict:
        """
        Simulate a proposed trade and return compliance impact analysis.

        Returns post-trade compliance status for:
          - Issuer concentration
          - Group concentration
          - Sector exposure
          - Rating distribution
          - Duration impact
          - Maturity constraints
          - Liquidity impact
        """
        action = action.upper()
        if action not in ("BUY", "SELL"):
            return {"error": "Action must be BUY or SELL"}

        # Get bond details
        bond = await fetch_one(
            """SELECT b.*, m.clean_price, m.ytm, m.modified_duration,
                      m.liquidity_score, m.traded_volume
               FROM universal_bond_master b
               LEFT JOIN unified_market_data m ON b.isin = m.isin
                   AND m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = b.isin)
               WHERE b.isin = :isin""",
            {"isin": isin},
        )
        if not bond:
            return {"error": f"Bond {isin} not found in master registry"}

        face_value = bond.get("face_value")
        if not face_value or face_value <= 0:
            return {
                "error": f"Bond {isin} has no face_value in master registry. "
                         "Cannot compute trade value — aborting pre-trade check.",
                "overall_status": "BLOCKED",
                "trade_blocked": True,
                "block_reason": "MISSING_FACE_VALUE",
            }
        trade_value = price * quantity * (face_value / 100)

        # Get current portfolio
        holdings = await IPSEngine._get_portfolio_holdings()
        rules = await IPSEngine.get_rules()
        rule_map = {(r["rule_category"], r["rule_key"]): r for r in rules}

        current_total = sum(h.get("market_value", 0) or 0 for h in holdings)

        # Build post-trade portfolio
        if action == "BUY":
            post_total = current_total + trade_value
        else:
            post_total = max(current_total - trade_value, 0)

        violations = []
        warnings = []
        checks = []

        # ── 1. Issuer Concentration Impact ──
        issuer_name = normalize_issuer_name(bond.get("issuer_name") or "UNKNOWN") or "UNKNOWN"
        issuer_display_name = (str(bond.get("issuer_name") or "").strip() or issuer_name)
        current_issuer_value = sum(
            h.get("market_value", 0) or 0
            for h in holdings
            if normalize_issuer_name(h.get("issuer_name") or "") == issuer_name
        )
        post_issuer_value = current_issuer_value + (trade_value if action == "BUY" else -trade_value)
        post_issuer_pct = (post_issuer_value / post_total * 100) if post_total > 0 else 0

        rule = rule_map.get(("ISSUER_CONCENTRATION", "single_issuer_max_pct"))
        limit = rule["rule_value"] if rule else 10.0
        rating = (bond.get("credit_rating") or "NR").upper()
        is_sov = rating in ("SOVEREIGN", "SOV") or (bond.get("issuer_type") or "").upper() in ("G-SEC", "SDL")

        if not is_sov:
            check = {
                "check": "Issuer Concentration",
                "issuer": issuer_display_name,
                "issuer_normalized": issuer_name,
                "current_pct": round(current_issuer_value / current_total * 100, 2) if current_total > 0 else 0,
                "post_trade_pct": round(post_issuer_pct, 2),
                "limit_pct": limit,
            }
            if post_issuer_pct > limit:
                check["status"] = "VIOLATION"
                violations.append({
                    "rule": "single_issuer_max_pct",
                    "description": f"Post-trade issuer '{issuer_display_name}' at {post_issuer_pct:.1f}% (limit {limit}%)",
                    "severity": "BREACH",
                })
            elif post_issuer_pct > limit * 0.8:
                check["status"] = "WARNING"
                warnings.append({
                    "rule": "single_issuer_max_pct",
                    "description": f"Post-trade issuer '{issuer_display_name}' approaching limit at {post_issuer_pct:.1f}%",
                })
            else:
                check["status"] = "PASS"
            checks.append(check)

        # ── 2. Group Concentration ──
        groups = await fetch_all(
            "SELECT issuer_name, group_name FROM issuer_group_map WHERE is_active = 1"
        )
        issuer_to_group = build_issuer_group_lookup(groups)
        group_name = resolve_group_name(issuer_display_name, issuer_to_group)

        current_group_value = 0.0
        for h in holdings:
            h_group = resolve_group_name(h.get("issuer_name") or "", issuer_to_group)
            if h_group == group_name:
                current_group_value += h.get("market_value", 0) or 0

        post_group_value = current_group_value + (trade_value if action == "BUY" else -trade_value)
        post_group_pct = (post_group_value / post_total * 100) if post_total > 0 else 0

        rule = rule_map.get(("ISSUER_CONCENTRATION", "single_group_max_pct"))
        group_limit = rule["rule_value"] if rule else 15.0

        if not is_sov and not is_goi_group(group_name):
            check = {
                "check": "Group Concentration",
                "group": group_name,
                "current_pct": round(current_group_value / current_total * 100, 2) if current_total > 0 else 0,
                "post_trade_pct": round(post_group_pct, 2),
                "limit_pct": group_limit,
            }
            if post_group_pct > group_limit:
                check["status"] = "VIOLATION"
                violations.append({
                    "rule": "single_group_max_pct",
                    "description": f"Post-trade group '{group_name}' at {post_group_pct:.1f}% (limit {group_limit}%)",
                    "severity": "BREACH",
                })
            elif post_group_pct > group_limit * 0.8:
                check["status"] = "WARNING"
                warnings.append({
                    "rule": "single_group_max_pct",
                    "description": f"Post-trade group '{group_name}' approaching limit at {post_group_pct:.1f}%",
                })
            else:
                check["status"] = "PASS"
            checks.append(check)

        # ── 3. Sector Exposure ──
        sector = (bond.get("sector") or bond.get("issuer_type") or "OTHER").upper()
        current_sector_value = sum(
            h.get("market_value", 0) or 0
            for h in holdings
            if (h.get("sector") or h.get("category") or "OTHER").upper() == sector
        )
        post_sector_value = current_sector_value + (trade_value if action == "BUY" else -trade_value)
        post_sector_pct = (post_sector_value / post_total * 100) if post_total > 0 else 0

        rule = rule_map.get(("ISSUER_CONCENTRATION", "sector_max_pct"))
        sector_limit = rule["rule_value"] if rule else 25.0

        if sector not in ("G-SEC", "SDL", "SOVEREIGN"):
            check = {
                "check": "Sector Exposure",
                "sector": sector,
                "current_pct": round(current_sector_value / current_total * 100, 2) if current_total > 0 else 0,
                "post_trade_pct": round(post_sector_pct, 2),
                "limit_pct": sector_limit,
            }
            if post_sector_pct > sector_limit:
                check["status"] = "VIOLATION"
                violations.append({
                    "rule": "sector_max_pct",
                    "description": f"Post-trade sector '{sector}' at {post_sector_pct:.1f}% (limit {sector_limit}%)",
                    "severity": "BREACH",
                })
            else:
                check["status"] = "PASS"
            checks.append(check)

        # ── 4. Rating Distribution ──
        rule = rule_map.get(("CREDIT_RATING", "min_rating"))
        if rule:
            min_rating = rule.get("rule_value_text") or "AA-"
            if _rating_rank(rating) > _rating_rank(min_rating) and rating not in ("SOVEREIGN", "SOV", "NR"):
                check = {
                    "check": "Minimum Rating",
                    "bond_rating": rating,
                    "min_required": min_rating,
                    "status": "VIOLATION",
                }
                violations.append({
                    "rule": "min_rating",
                    "description": f"Bond rated {rating} is below minimum {min_rating}",
                    "severity": "CRITICAL",
                })
                checks.append(check)

        # Check AAA % post-trade
        rule = rule_map.get(("CREDIT_RATING", "min_aaa_pct"))
        if rule and action == "BUY":
            current_aaa = sum(
                h.get("market_value", 0) or 0
                for h in holdings
                if (h.get("rating") or "").upper() in ("AAA", "SOVEREIGN", "SOV")
            )
            bond_is_aaa = rating in ("AAA", "SOVEREIGN", "SOV")
            post_aaa = current_aaa + (trade_value if bond_is_aaa else 0)
            post_aaa_pct = (post_aaa / post_total * 100) if post_total > 0 else 0
            aaa_limit = rule["rule_value"] or 60.0

            check = {
                "check": "AAA Allocation",
                "current_pct": round(current_aaa / current_total * 100, 2) if current_total > 0 else 0,
                "post_trade_pct": round(post_aaa_pct, 2),
                "limit_pct": aaa_limit,
            }
            if post_aaa_pct < aaa_limit:
                check["status"] = "WARNING"
                warnings.append({
                    "rule": "min_aaa_pct",
                    "description": f"Post-trade AAA at {post_aaa_pct:.1f}% (min {aaa_limit}%)",
                })
            else:
                check["status"] = "PASS"
            checks.append(check)

        # ── 5. Duration Impact ──
        bond_duration = bond.get("modified_duration") or 0
        if not bond_duration and not is_sov:
            # Missing duration for non-sovereign bonds — cannot assess interest rate risk
            violations.append({
                "rule": "missing_duration",
                "description": f"Bond {isin} has no modified_duration — cannot assess interest rate risk",
                "severity": "BREACH",
            })
            checks.append({
                "check": "Duration Data Present",
                "status": "VIOLATION",
                "detail": "modified_duration is NULL/0 for non-sovereign bond",
            })
        current_portfolio_dur = 0.0
        for h in holdings:
            dur = h.get("modified_duration", 0) or 0
            weight = (h.get("market_value", 0) or 0) / current_total if current_total > 0 else 0
            current_portfolio_dur += dur * weight

        if action == "BUY" and post_total > 0:
            post_dur = (current_portfolio_dur * current_total + bond_duration * trade_value) / post_total
        elif action == "SELL" and post_total > 0:
            post_dur = (current_portfolio_dur * current_total - bond_duration * trade_value) / post_total
        else:
            post_dur = current_portfolio_dur

        rule = rule_map.get(("DURATION", "max_portfolio_duration"))
        dur_limit = rule["rule_value"] if rule else 6.0

        check = {
            "check": "Portfolio Duration",
            "current_duration": round(current_portfolio_dur, 2),
            "post_trade_duration": round(post_dur, 2),
            "limit": dur_limit,
        }
        if post_dur > dur_limit:
            check["status"] = "VIOLATION"
            violations.append({
                "rule": "max_portfolio_duration",
                "description": f"Post-trade duration {post_dur:.2f}Y exceeds {dur_limit}Y",
                "severity": "BREACH",
            })
        else:
            check["status"] = "PASS"
        checks.append(check)

        # ── 6. Maturity Constraint ──
        mat_str = bond.get("maturity_date", "")
        if mat_str:
            try:
                mat_date = datetime.strptime(mat_str[:10], "%Y-%m-%d").date()

                # Temporal consistency: maturity must be in the future
                if mat_date <= date.today():
                    violations.append({
                        "rule": "maturity_past_date",
                        "description": f"Bond maturity {mat_date} is in the past — cannot trade matured bond",
                        "severity": "CRITICAL",
                    })
                    checks.append({
                        "check": "Maturity Temporal Validity",
                        "maturity_date": str(mat_date),
                        "status": "VIOLATION",
                    })

                # Temporal consistency: maturity must be after issue date
                issue_str = bond.get("issue_date", "")
                if issue_str:
                    try:
                        issue_date = datetime.strptime(issue_str[:10], "%Y-%m-%d").date()
                        if mat_date <= issue_date:
                            violations.append({
                                "rule": "maturity_before_issue",
                                "description": f"Maturity {mat_date} <= issue date {issue_date} — data integrity error",
                                "severity": "CRITICAL",
                            })
                            checks.append({
                                "check": "Maturity vs Issue Date",
                                "maturity_date": str(mat_date),
                                "issue_date": str(issue_date),
                                "status": "VIOLATION",
                            })
                    except (ValueError, TypeError):
                        pass

                years_to_mat = (mat_date - date.today()).days / 365.25
                rule = rule_map.get(("MATURITY", "max_maturity_years"))
                mat_limit = rule["rule_value"] if rule else 10.0

                check = {
                    "check": "Maturity Constraint",
                    "bond_maturity_years": round(years_to_mat, 2),
                    "limit_years": mat_limit,
                }
                if years_to_mat > mat_limit:
                    check["status"] = "VIOLATION"
                    violations.append({
                        "rule": "max_maturity_years",
                        "description": f"Bond maturity {years_to_mat:.1f}Y exceeds {mat_limit}Y limit",
                        "severity": "BREACH",
                    })
                else:
                    check["status"] = "PASS"
                checks.append(check)
            except (ValueError, TypeError):
                violations.append({
                    "rule": "maturity_date_invalid",
                    "description": f"Maturity date '{mat_str}' is not a valid date — cannot assess maturity risk",
                    "severity": "CRITICAL",
                })
                checks.append({
                    "check": "Maturity Date Validity",
                    "raw_value": mat_str,
                    "status": "VIOLATION",
                })
        else:
            # Missing maturity date — BLOCK trade (cannot assess maturity risk)
            violations.append({
                "rule": "missing_maturity_date",
                "description": "Bond has no maturity_date in master registry — cannot assess maturity risk",
                "severity": "CRITICAL",
            })
            checks.append({
                "check": "Maturity Date Present",
                "status": "VIOLATION",
                "detail": "maturity_date is NULL/empty in bond master",
            })

        # ── 7. Liquidity Impact ──
        liq_score = bond.get("liquidity_score", 50) or 50
        volume = bond.get("traded_volume", 0) or 0
        rule = rule_map.get(("LIQUIDITY", "min_volume_threshold"))
        if rule:
            vol_threshold = rule["rule_value"] or 1000000
            check = {
                "check": "Liquidity",
                "liquidity_score": liq_score,
                "traded_volume": volume,
                "volume_threshold": vol_threshold,
            }
            if volume < vol_threshold and action == "BUY":
                check["status"] = "WARNING"
                warnings.append({
                    "rule": "min_volume_threshold",
                    "description": f"Bond volume ₹{volume:,.0f} below threshold ₹{vol_threshold:,.0f}",
                })
            else:
                check["status"] = "PASS"
            checks.append(check)

        # ── Determine overall status ──
        trade_blocked = False
        overall_status = "APPROVED"

        has_critical = any(v.get("severity") == "CRITICAL" for v in violations)
        has_breach = len(violations) > 0

        if has_critical:
            overall_status = "BLOCKED"
            trade_blocked = True
        elif has_breach and action == "BUY":
            overall_status = "BLOCKED"
            trade_blocked = True
        elif warnings:
            overall_status = "APPROVED_WITH_WARNINGS"

        now = utcnow_iso()

        # Record simulation
        try:
            await execute_query(
                """INSERT INTO pretrade_simulations
                   (isin, action, quantity, price, trade_value, simulation_result,
                    overall_status, violations_json, warnings_json, trade_blocked,
                    simulated_by, simulated_at)
                   VALUES (:isin, :action, :qty, :price, :tv, :sr, :os,
                           :vj, :wj, :tb, :sb, :sa)""",
                {
                    "isin": isin, "action": action, "qty": quantity,
                    "price": price, "tv": trade_value,
                    "sr": json.dumps(checks), "os": overall_status,
                    "vj": json.dumps(violations), "wj": json.dumps(warnings),
                    "tb": 1 if trade_blocked else 0,
                    "sb": user, "sa": now,
                },
            )
        except Exception as e:
            log.error("pretrade_sim_record_failed", error=str(e))

        return {
            "isin": isin,
            "bond_name": bond.get("bond_name", ""),
            "issuer": bond.get("issuer_name", ""),
            "action": action,
            "quantity": quantity,
            "price": price,
            "trade_value": round(trade_value, 2),
            "overall_status": overall_status,
            "trade_blocked": trade_blocked,
            "can_override": has_breach and not has_critical,
            "checks": checks,
            "violations": violations,
            "warnings": warnings,
            "post_trade_summary": {
                "portfolio_value": round(post_total, 2),
                "issuer_exposure_pct": round(post_issuer_pct, 2) if not is_sov else "N/A (Sovereign)",
                "group_exposure_pct": round(post_group_pct, 2) if not is_sov else "N/A",
                "sector_exposure_pct": round(post_sector_pct, 2),
                "portfolio_duration": round(post_dur, 2),
            },
            "simulated_at": now,
        }

    @classmethod
    async def log_override(cls, simulation_id: int, user: str,
                            reason: str, approved_by: Optional[str] = None) -> dict:
        """Log a compliance override for a blocked trade."""
        sim = await fetch_one(
            "SELECT * FROM pretrade_simulations WHERE id = :id",
            {"id": simulation_id},
        )
        if not sim:
            return {"error": f"Simulation {simulation_id} not found"}

        if not sim["trade_blocked"]:
            return {"error": "Trade was not blocked, no override needed"}

        now = utcnow_iso()

        await execute_query(
            """UPDATE pretrade_simulations
               SET override_allowed = 1, override_by = :user,
                   override_reason = :reason, override_at = :now
               WHERE id = :id""",
            {"user": user, "reason": reason, "now": now, "id": simulation_id},
        )

        await execute_query(
            """INSERT INTO trade_override_log
               (simulation_id, isin, override_by, override_reason,
                violations_overridden, approved_by, approval_at, created_at)
               VALUES (:sid, :isin, :user, :reason, :violations, :approver, :appat, :now)""",
            {
                "sid": simulation_id, "isin": sim["isin"],
                "user": user, "reason": reason,
                "violations": sim.get("violations_json"),
                "approver": approved_by, "appat": now if approved_by else None,
                "now": now,
            },
        )

        # Log to audit trail
        try:
            await execute_query(
                """INSERT INTO unified_decision_audit
                   (isin, decision_date, action, decision, reason, performed_by, metadata_json, created_at)
                   VALUES (:isin, :dd, 'COMPLIANCE_OVERRIDE', 'OVERRIDE', :reason, :user, :meta, :now)""",
                {
                    "isin": sim["isin"], "dd": now[:10], "reason": reason,
                    "user": user, "now": now,
                    "meta": json.dumps({
                        "simulation_id": simulation_id,
                        "original_status": sim["overall_status"],
                        "violations": json.loads(sim.get("violations_json", "[]")),
                        "approved_by": approved_by,
                    }),
                },
            )
        except Exception as e:
            log.error("audit_override_failed", error=str(e))

        return {
            "override_id": simulation_id,
            "isin": sim["isin"],
            "overridden_by": user,
            "reason": reason,
            "approved_by": approved_by,
            "timestamp": now,
        }

    @classmethod
    async def get_simulation_history(cls, isin: Optional[str] = None,
                                      days: int = 30) -> List[dict]:
        """Get pre-trade simulation history."""
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        if isin:
            return await fetch_all(
                """SELECT * FROM pretrade_simulations
                   WHERE isin = :isin AND simulated_at >= :cutoff
                   ORDER BY simulated_at DESC LIMIT 100""",
                {"isin": isin, "cutoff": cutoff},
            )
        return await fetch_all(
            """SELECT * FROM pretrade_simulations
               WHERE simulated_at >= :cutoff
               ORDER BY simulated_at DESC LIMIT 100""",
            {"cutoff": cutoff},
        )
