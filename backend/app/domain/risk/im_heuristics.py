"""
IM Hidden Risk Detection Heuristics Library  (Gateway Edition)
==============================================================
24 heuristics across 9 categories for detecting hidden risks in IMs.
Adapted from bond_intelligence engine to use the gateway's shared DB.

Source tables used:
  universal_bond_master  — bond details
  bond_im_data           — structured IM data (entered / extracted)
  issuer_financials      — annual financial data
  unified_market_data    — latest market prices / spreads

H1-H3:  Linguistic Weakness Detection
H4-H7:  Structural Risk
H8-H10: Covenant Weakness
H11-H12: Cashflow Dependency
H13-H15: Legal Ambiguity
H16-H18: Related Party & Governance
H19-H20: Debt Stack Risk
H21-H22: Rating-Related
H23-H24: Market-Based
"""

import re
import json
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from ..shared.database import fetch_all, fetch_one, execute_query

# ── Credit Spread Matrix (bps over G-Sec) ─────────────────────────────
CREDIT_SPREAD_MATRIX = {
    "AAA": {"1Y": 30, "3Y": 45, "5Y": 55, "7Y": 65, "10Y": 75},
    "AA+": {"1Y": 50, "3Y": 65, "5Y": 80, "7Y": 95, "10Y": 110},
    "AA":  {"1Y": 70, "3Y": 90, "5Y": 110, "7Y": 125, "10Y": 140},
    "AA-": {"1Y": 90, "3Y": 115, "5Y": 140, "7Y": 160, "10Y": 180},
    "A+":  {"1Y": 130, "3Y": 160, "5Y": 190, "7Y": 220, "10Y": 250},
    "A":   {"1Y": 170, "3Y": 210, "5Y": 250, "7Y": 285, "10Y": 320},
}

# ═══════════════════════════════════════════════════════════════════════
# HEURISTIC DEFINITION REGISTRY
# ═══════════════════════════════════════════════════════════════════════

HEURISTIC_REGISTRY = {
    "H1":  {"name": "Soft Commitment Language",           "category": "LINGUISTIC",  "sub_category": "NON_BINDING_OBLIGATION",    "impact_weight": 7},
    "H2":  {"name": "Conditional Guarantee",              "category": "LINGUISTIC",  "sub_category": "GUARANTEE_WEAKNESS",        "impact_weight": 9},
    "H3":  {"name": "Ambiguous Ranking Language",         "category": "LINGUISTIC",  "sub_category": "PARI_PASSU_AMBIGUITY",      "impact_weight": 8},
    "H4":  {"name": "Structural Subordination",           "category": "STRUCTURAL",  "sub_category": "SPV_RISK",                  "impact_weight": 9},
    "H5":  {"name": "Bullet Repayment Concentration",     "category": "STRUCTURAL",  "sub_category": "REFINANCING_RISK",          "impact_weight": 8},
    "H6":  {"name": "Weak Security Cover",                "category": "STRUCTURAL",  "sub_category": "SECURITY_COVER",            "impact_weight": 10},
    "H7":  {"name": "Cross-Collateral Confusion",         "category": "STRUCTURAL",  "sub_category": "SHARED_SECURITY",           "impact_weight": 8},
    "H8":  {"name": "Covenant Headroom Risk",             "category": "COVENANT",    "sub_category": "HEADROOM",                  "impact_weight": 8},
    "H9":  {"name": "No Maintenance Covenants",           "category": "COVENANT",    "sub_category": "INCURRENCE_ONLY",           "impact_weight": 9},
    "H10": {"name": "Cure Period Abuse",                  "category": "COVENANT",    "sub_category": "DELAYED_DEFAULT",           "impact_weight": 6},
    "H11": {"name": "Single Revenue Source Dependency",   "category": "CASHFLOW",    "sub_category": "CONCENTRATION",             "impact_weight": 8},
    "H12": {"name": "Regulatory Approval Dependency",     "category": "CASHFLOW",    "sub_category": "REGULATORY_RISK",           "impact_weight": 7},
    "H13": {"name": "Change in Control Clause Weakness",  "category": "LEGAL",       "sub_category": "CHANGE_OF_CONTROL",         "impact_weight": 6},
    "H14": {"name": "Event of Default Narrow Definition", "category": "LEGAL",       "sub_category": "DEFAULT_DEFINITION",        "impact_weight": 8},
    "H15": {"name": "Trustee Limited Powers",             "category": "LEGAL",       "sub_category": "ENFORCEMENT_RISK",          "impact_weight": 5},
    "H16": {"name": "Promoter Support Letter (Non-binding)","category":"GOVERNANCE", "sub_category": "COMFORT_LETTER",            "impact_weight": 8},
    "H17": {"name": "High Related Party Transactions",    "category": "GOVERNANCE",  "sub_category": "RELATED_PARTY",             "impact_weight": 7},
    "H18": {"name": "Pledged Promoter Shares",            "category": "GOVERNANCE",  "sub_category": "PROMOTER_PLEDGE",           "impact_weight": 7},
    "H19": {"name": "Senior Debt Above Bond",             "category": "DEBT_STACK",  "sub_category": "SUBORDINATION",             "impact_weight": 9},
    "H20": {"name": "Intercreditor Agreement Missing",    "category": "DEBT_STACK",  "sub_category": "ENFORCEMENT_WATERFALL",     "impact_weight": 8},
    "H21": {"name": "Rating Watch Negative",              "category": "RATING",      "sub_category": "WATCHLIST",                 "impact_weight": 8},
    "H22": {"name": "Rating Based on External Support",   "category": "RATING",      "sub_category": "SUPPORT_UPLIFT",            "impact_weight": 6},
    "H23": {"name": "Spread Widening",                    "category": "MARKET",      "sub_category": "SPREAD_ANOMALY",            "impact_weight": 6},
    "H24": {"name": "Liquidity Dry-Up",                   "category": "MARKET",      "sub_category": "ILLIQUIDITY",               "impact_weight": 6},
}

# ── Keyword lists ─────────────────────────────────────────────────────
SOFT_COMMITMENT_PHRASES = [
    r"\bmay\b", r"\bendeavour\b", r"\bintend\b", r"\bpropose\b",
    r"\bsubject to\b", r"\bbest efforts?\b", r"\bwhere feasible\b",
    r"\bat (?:its |the )?discretion\b", r"\bas deemed appropriate\b",
    r"\breasonable endeavour\b", r"\bshal[l]? attempt\b",
    r"\bno assurance\b", r"\bnot guarantee\b", r"\bnot binding\b",
]
CONDITIONAL_GUARANTEE_PHRASES = [
    r"corporate guarantee subject to", r"guarantee limited to",
    r"guarantee enforceable only upon", r"guarantor may withdraw",
    r"guarantee shall cease", r"guarantee is conditional",
    r"subject to availability of funds", r"guarantee shall be effective only",
    r"irrevocable.*subject to",
]
AMBIGUOUS_RANKING_PHRASES = [
    r"intended to rank pari passu", r"shall rank equivalent to",
    r"subject to existing encumbrances", r"rank(?:s|ing)? (?:at least )?pari passu",
    r"subordinated (?:only )?to existing", r"subject to prior charges",
    r"priority.*at the discretion",
]
STRUCTURAL_SUB_PHRASES = [
    r"\bspv\b", r"\bspecial purpose (?:vehicle|entity)\b",
    r"upstream.*cash\s*flow", r"downstream subsidiary",
    r"operating entity.*separate", r"holding company bond",
]
CROSS_COLLATERAL_PHRASES = [
    r"(?:security|collateral).*shared", r"pari passu charge.*(?:with|among)",
    r"intercreditor.*not.*disclos", r"shared security",
    r"common pool of assets", r"second charge",
]
WEAK_DEFAULT_DEFINITION_EXCLUSIONS = [
    "cross-default", "cross default", "rating downgrade",
    "auditor resignation", "litigation", "change of auditor",
    "material adverse change",
]
COMFORT_LETTER_PHRASES = [
    r"comfort letter", r"letter of comfort",
    r"support letter.*non-?binding",
    r"promoter.*undertaking.*(?:non-?binding|moral)",
    r"moral obligation", r"letter of awareness",
]


# ═══════════════════════════════════════════════════════════════════════
# ENGINE CLASS
# ═══════════════════════════════════════════════════════════════════════

class IMHeuristicsEngine:
    """
    Runs all 24 heuristics (H1–H24) against a bond's IM data,
    financial data, and market data. Returns audit-grade findings.
    """

    def __init__(self):
        self.registry = HEURISTIC_REGISTRY

    async def run_all_heuristics(self, isin: str) -> Dict[str, Any]:
        """Run all 24 heuristics for a given ISIN."""
        bond = await fetch_one(
            "SELECT * FROM universal_bond_master WHERE isin = :isin", {"isin": isin}
        )
        if not bond:
            return {"error": f"Bond {isin} not found in master registry", "heuristics": []}

        im_data = await fetch_one(
            "SELECT * FROM bond_im_data WHERE isin = :isin", {"isin": isin}
        )
        if im_data:
            for field in ["covenants_financial", "covenants_non_financial", "risk_flags"]:
                val = im_data.get(field)
                if val and isinstance(val, str):
                    try:
                        im_data[field] = json.loads(val)
                    except Exception:
                        pass

        # Fetch financial data (most recent year)
        issuer = bond.get("issuer_name", "")
        financials = await fetch_one(
            """SELECT * FROM issuer_financials
               WHERE issuer_name = :name ORDER BY financial_year DESC LIMIT 1""",
            {"name": issuer}
        )

        # Fetch latest market data
        market_row = await fetch_one(
            """SELECT * FROM unified_market_data
               WHERE isin = :isin ORDER BY price_date DESC LIMIT 1""",
            {"isin": isin}
        )
        # Enrich market row with days_since_last_trade
        market: dict = dict(market_row) if market_row else {}
        if market.get("price_date"):
            try:
                last_date = datetime.strptime(market["price_date"][:10], "%Y-%m-%d").date()
                market["days_since_last_trade"] = (date.today() - last_date).days
            except Exception:
                pass

        ctx = {
            "bond": bond,
            "im_data": im_data or {},
            "financials": financials or {},
            "market": market,
            "rating_history": [],  # not in gateway DB
            "news": [],            # not in gateway DB
            "im_full_text": (im_data or {}).get("im_full_text", "") or "",
        }

        results = []
        for hid, meta in self.registry.items():
            method_name = f"_check_{hid.lower()}"
            method = getattr(self, method_name, None)
            if method:
                try:
                    finding = method(ctx)
                except Exception as e:
                    finding = {"triggered": False, "error": str(e)}
            else:
                finding = {"triggered": False, "note": "Not implemented"}

            results.append({
                "heuristic_id": hid,
                "name": meta["name"],
                "category": meta["category"],
                "sub_category": meta["sub_category"],
                "triggered": finding.get("triggered", False),
                "severity": finding.get("severity", "LOW"),
                "impact_weight": finding.get("impact_weight", meta["impact_weight"]),
                "explanation": finding.get("explanation", ""),
                "page_reference": finding.get("page_reference"),
                "confidence": finding.get("confidence", 0.0),
                "source_text": finding.get("source_text"),
                "remediation": finding.get("remediation"),
            })

        triggered = [r for r in results if r["triggered"]]
        total_impact = sum(r["impact_weight"] for r in triggered)
        max_possible = sum(m["impact_weight"] for m in self.registry.values())
        hidden_risk_score = round(total_impact / max_possible * 100, 1) if max_possible else 0

        category_summary: Dict[str, dict] = {}
        for r in results:
            cat = r["category"]
            if cat not in category_summary:
                category_summary[cat] = {"total": 0, "triggered": 0, "max_severity": "LOW"}
            category_summary[cat]["total"] += 1
            if r["triggered"]:
                category_summary[cat]["triggered"] += 1
                sev_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
                if sev_order.get(r["severity"], 0) > sev_order.get(category_summary[cat]["max_severity"], 0):
                    category_summary[cat]["max_severity"] = r["severity"]

        await self._store_heuristic_results(isin, results, hidden_risk_score)

        return {
            "isin": isin,
            "issuer_name": bond.get("issuer_name"),
            "analysis_date": date.today().isoformat(),
            "total_heuristics": len(results),
            "triggered_count": len(triggered),
            "hidden_risk_score": hidden_risk_score,
            "risk_level": (
                "CRITICAL" if hidden_risk_score > 60 else
                "HIGH" if hidden_risk_score > 40 else
                "MEDIUM" if hidden_risk_score > 20 else "LOW"
            ),
            "category_summary": category_summary,
            "heuristics": results,
            "triggered_heuristics": triggered,
            "im_data_available": bool(im_data),
            "financials_available": bool(financials),
            "market_data_available": bool(market),
        }

    # ═══════════════════════════════════════════════════════════════════
    # H1 — Soft Commitment Language
    # ═══════════════════════════════════════════════════════════════════
    def _check_h1(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        full_text = ctx.get("im_full_text", "")
        critical_sections = [
            im.get("guarantee_details", ""), im.get("security_structure", ""),
            im.get("default_clauses", ""), im.get("payment_mechanism", ""),
            im.get("escrow_mechanism", ""),
            " ".join(im.get("covenants_financial") or []) if isinstance(im.get("covenants_financial"), list) else str(im.get("covenants_financial", "")),
        ]
        combined = " ".join(str(s) for s in critical_sections if s).lower()
        if full_text:
            combined = full_text.lower()
        found_phrases = []
        for pattern in SOFT_COMMITMENT_PHRASES:
            matches = re.findall(pattern, combined, re.IGNORECASE)
            if matches:
                found_phrases.extend(matches[:3])
        if not found_phrases:
            return {"triggered": False}
        guarantee_text = str(im.get("guarantee_details", "")).lower()
        security_text = str(im.get("security_structure", "")).lower()
        in_critical = any(
            re.search(p, guarantee_text + " " + security_text, re.IGNORECASE)
            for p in SOFT_COMMITMENT_PHRASES
        )
        severity = "HIGH" if in_critical else "MEDIUM"
        return {
            "triggered": True, "severity": severity,
            "impact_weight": 9 if in_critical else 7,
            "explanation": (
                f"Found {len(found_phrases)} instances of non-binding language: "
                f"{', '.join(set(found_phrases[:5]))}. "
                f"{'Used in guarantee/security sections — HIGH risk.' if in_critical else 'MEDIUM risk.'}"
            ),
            "confidence": min(0.95, 0.6 + len(found_phrases) * 0.05),
            "source_text": "; ".join(set(found_phrases[:5])),
            "remediation": "Replace with binding language — 'shall' not 'may'",
        }

    # H2 — Conditional Guarantee
    def _check_h2(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        guarantee = str(im.get("guarantee_details", "")).lower()
        full_text = ctx.get("im_full_text", "").lower()
        search_text = guarantee or full_text
        if not search_text:
            return {"triggered": False}
        found = [p for p in CONDITIONAL_GUARANTEE_PHRASES if re.search(p, search_text, re.IGNORECASE)]
        if not found:
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH", "impact_weight": 9,
            "explanation": f"Guarantee contains {len(found)} conditional clause(s) — may be unenforceable under stress.",
            "confidence": min(0.95, 0.7 + len(found) * 0.08),
            "source_text": guarantee[:300] if guarantee else None,
            "remediation": "Request unconditional, irrevocable guarantee",
        }

    # H3 — Ambiguous Ranking Language
    def _check_h3(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        security = str(im.get("security_structure", "")).lower()
        full_text = ctx.get("im_full_text", "").lower()
        found = [p for p in AMBIGUOUS_RANKING_PHRASES if re.search(p, security + " " + full_text, re.IGNORECASE)]
        if not found:
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH" if len(found) > 1 else "MEDIUM", "impact_weight": 8,
            "explanation": f"Debt ranking is ambiguous — {len(found)} vague clause(s). Potential structural subordination.",
            "confidence": 0.75, "source_text": security[:200] if security else None,
            "remediation": "Request clear, unambiguous ranking statement with explicit charge priority",
        }

    # H4 — Structural Subordination (SPV Risk)
    def _check_h4(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        bond = ctx["bond"]
        full_text = ctx.get("im_full_text", "").lower()
        issuer_name = str(bond.get("issuer_name", "")).lower()
        obj = str(im.get("object_of_issue", "")).lower()
        security = str(im.get("security_structure", "")).lower()
        corpus = full_text + " " + obj + " " + security + " " + issuer_name
        spv_indicators = [p for p in STRUCTURAL_SUB_PHRASES if re.search(p, corpus, re.IGNORECASE)]
        if not spv_indicators:
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH", "impact_weight": 9,
            "explanation": "Issuer appears to be an SPV — bondholders may be structurally subordinated to operating creditors.",
            "confidence": 0.70, "source_text": obj[:200] if obj else None,
            "remediation": "Ensure direct security over operating assets or parent guarantee",
        }

    # H5 — Bullet Repayment Concentration
    def _check_h5(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        redemption = str(im.get("redemption_structure", "")).upper()
        full_text = ctx.get("im_full_text", "").lower()
        is_bullet = (redemption == "BULLET" or "bullet" in full_text
                     or ("no amortiz" in full_text and "no sinking" in full_text))
        if not is_bullet:
            return {"triggered": False}
        has_dsra = "dsra" in full_text or "debt service reserve" in full_text
        has_sinking = "sinking fund" in full_text
        fin = ctx["financials"]
        leverage = fin.get("debt_ebitda") or 0
        if has_dsra or has_sinking:
            severity, explanation = "MEDIUM", "Bullet repayment but mitigated by DSRA/sinking fund"
        elif leverage > 4.0:
            severity, explanation = "HIGH", f"Bullet repayment with high leverage ({leverage:.1f}x D/EBITDA)"
        else:
            severity, explanation = "MEDIUM", "Bullet repayment — full principal due at maturity, refinancing risk"
        return {
            "triggered": True, "severity": severity, "impact_weight": 9 if severity == "HIGH" else 7,
            "explanation": explanation, "confidence": 0.85,
            "remediation": "Request structured amortization or DSRA coverage",
        }

    # H6 — Weak Security Cover
    def _check_h6(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        full_text = ctx.get("im_full_text", "").lower()
        security = str(im.get("security_structure", "")).lower()
        charge_type = str(im.get("charge_type", "")).upper()
        if not security or security in ("unsecured", "none", ""):
            if charge_type not in ("FIRST", "SECOND", "PARI_PASSU"):
                return {
                    "triggered": True, "severity": "CRITICAL", "impact_weight": 10,
                    "explanation": "Bond is UNSECURED — no asset backing for investors.",
                    "confidence": 0.95, "remediation": "Seek secured issuance or require collateral",
                }
        cover_match = re.search(r"(?:security|asset)\s*cover(?:age)?\s*(?:ratio)?\s*(?:of|:)?\s*(\d+\.?\d*)", full_text)
        cover_ratio = float(cover_match.group(1)) if cover_match else None
        projection_risk = any(kw in full_text for kw in [
            "projected revenue", "estimated valuation", "expected cash flow",
            "book value basis", "replacement cost", "fair market value"])
        if cover_ratio is not None:
            if cover_ratio < 1.0:
                sev, exp = "CRITICAL", f"Security cover {cover_ratio}x is below 1.0 — insufficient"
            elif cover_ratio < 1.25:
                sev, exp = "HIGH", f"Security cover {cover_ratio}x is below 1.25x minimum"
            elif projection_risk:
                sev, exp = "MEDIUM", f"Security cover {cover_ratio}x — based on projected values"
            else:
                return {"triggered": False}
            return {
                "triggered": True, "severity": sev, "impact_weight": 10 if sev == "CRITICAL" else 8,
                "explanation": exp, "confidence": 0.80,
                "remediation": "Request minimum 1.5x security cover with independent valuation",
            }
        if charge_type == "SECOND":
            return {
                "triggered": True, "severity": "HIGH", "impact_weight": 8,
                "explanation": "Bond has SECOND CHARGE — subordinated claim in enforcement",
                "confidence": 0.90, "remediation": "Request first charge or adequate residual value coverage",
            }
        return {"triggered": False}

    # H7 — Cross-Collateral Confusion
    def _check_h7(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        full_text = ctx.get("im_full_text", "").lower()
        security = str(im.get("security_structure", "")).lower()
        found = [p for p in CROSS_COLLATERAL_PHRASES if re.search(p, full_text + " " + security, re.IGNORECASE)]
        if not found:
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH", "impact_weight": 8,
            "explanation": f"Security appears shared across multiple lenders — {len(found)} shared-security indicator(s).",
            "confidence": 0.70, "source_text": security[:200] if security else None,
            "remediation": "Request exclusive first charge or disclosed ICA with clear waterfall",
        }

    # H8 — Covenant Headroom Risk
    def _check_h8(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        fin = ctx["financials"]
        checks = []
        if im.get("dscr_requirement") and fin.get("dscr") and im["dscr_requirement"] > 0:
            hr = (fin["dscr"] - im["dscr_requirement"]) / im["dscr_requirement"] * 100
            checks.append(("DSCR", im["dscr_requirement"], fin["dscr"], hr))
        if im.get("debt_ebitda_limit") and fin.get("debt_ebitda") and im["debt_ebitda_limit"] > 0:
            hr = (im["debt_ebitda_limit"] - fin["debt_ebitda"]) / im["debt_ebitda_limit"] * 100
            checks.append(("Debt/EBITDA", im["debt_ebitda_limit"], fin["debt_ebitda"], hr))
        if im.get("interest_coverage_req") and fin.get("interest_coverage") and im["interest_coverage_req"] > 0:
            hr = (fin["interest_coverage"] - im["interest_coverage_req"]) / im["interest_coverage_req"] * 100
            checks.append(("ICR", im["interest_coverage_req"], fin["interest_coverage"], hr))
        if not checks:
            return {"triggered": False}
        tight = [(n, r, a, h) for n, r, a, h in checks if h < 10]
        breached = [(n, r, a, h) for n, r, a, h in checks if h < 0]
        if not tight and not breached:
            return {"triggered": False}
        severity = "HIGH" if breached else "MEDIUM"
        details = [f"{n}: actual {a:.2f} vs limit {r:.2f} ({'BREACHED' if h < 0 else f'{h:.0f}% headroom'})"
                   for n, r, a, h in (breached + tight)]
        return {
            "triggered": True, "severity": severity, "impact_weight": 9 if breached else 7,
            "explanation": f"Covenant headroom risk: {'; '.join(details)}",
            "confidence": 0.90, "remediation": "Monitor; covenant waiver may be needed",
        }

    # H9 — No Maintenance Covenants
    def _check_h9(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        full_text = ctx.get("im_full_text", "").lower()
        fin_covs = im.get("covenants_financial") or []
        if isinstance(fin_covs, str):
            try:
                fin_covs = json.loads(fin_covs)
            except Exception:
                fin_covs = [fin_covs] if fin_covs else []
        has_any = (im.get("dscr_requirement") or im.get("interest_coverage_req") or
                   im.get("debt_ebitda_limit") or len(fin_covs) > 0 or
                   any(kw in full_text for kw in ["maintenance covenant", "tested quarterly",
                       "tested semi-annually", "maintain at all times"]))
        if has_any:
            return {"triggered": False}
        is_incurrence_only = any(kw in full_text for kw in ["incurrence", "only at the time of"])
        return {
            "triggered": True, "severity": "HIGH", "impact_weight": 9,
            "explanation": (
                f"No maintenance covenants detected. "
                f"{'Only incurrence-based covenants — ' if is_incurrence_only else ''}"
                f"Credit deterioration may go undetected until default."
            ),
            "confidence": 0.60,
            "remediation": "Require quarterly maintenance covenants for DSCR, ICR, and D/EBITDA",
        }

    # H10 — Cure Period Abuse
    def _check_h10(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        im = ctx["im_data"]
        search = full_text + " " + str(im.get("default_clauses", "")).lower()
        cure_match = re.search(r"cure\s*period\s*(?:of\s*)?(\d+)\s*(?:calendar\s*|business\s*)?days?", search)
        if cure_match and int(cure_match.group(1)) > 90:
            days = int(cure_match.group(1))
            return {
                "triggered": True, "severity": "MEDIUM", "impact_weight": 6,
                "explanation": f"Cure period of {days} days exceeds 90 days — delays default recognition.",
                "confidence": 0.85, "source_text": cure_match.group(0),
                "remediation": "Cap cure period at 30-60 days",
            }
        return {"triggered": False}

    # H11 — Single Revenue Source Dependency
    def _check_h11(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        im = ctx["im_data"]
        obj = str(im.get("object_of_issue", "")).lower()
        patterns = [
            r"(\d+)%?\s*(?:of\s*)?(?:total\s*)?revenue\s*(?:from|attributable to)\s*(?:a\s*)?single",
            r"principal customer\s*(?:contribut|account)", r"(?:sole|single)\s*(?:client|customer|off-?taker)",
            r"revenue concentration", r"dependenc[ye] on\s*(?:a\s*)?single",
        ]
        for pattern in patterns:
            match = re.search(pattern, full_text + " " + obj, re.IGNORECASE)
            if match:
                return {
                    "triggered": True, "severity": "HIGH", "impact_weight": 8,
                    "explanation": "Revenue concentration risk — dependent on single client/contract.",
                    "confidence": 0.70, "source_text": match.group(0)[:100],
                    "remediation": "Verify diversification; add cross-default protection on key contract",
                }
        return {"triggered": False}

    # H12 — Regulatory Approval Dependency
    def _check_h12(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        im = ctx["im_data"]
        patterns = [
            "pending.*(?:government|regulatory|environmental).*(?:approval|clearance)",
            "subject to.*(?:regulatory|government).*approval", "tariff revision.*pending",
            "awaiting.*(?:license|licen[cs]e|permit|clearance)", "environmental clearance.*pending",
        ]
        found = [p for p in patterns if re.search(p, full_text + " " + str(im.get("object_of_issue", "")).lower(), re.IGNORECASE)]
        if not found:
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH", "impact_weight": 7,
            "explanation": "Project viability depends on pending regulatory approvals.",
            "confidence": 0.70,
            "remediation": "Obtain conditional escrow or backstop facility pending approval",
        }

    # H13 — Change in Control Clause Weakness
    def _check_h13(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        has_coc = any(kw in full_text for kw in ["change of control", "change in control"])
        if not has_coc:
            return {
                "triggered": True, "severity": "MEDIUM", "impact_weight": 6,
                "explanation": "No change of control clause — bondholders unprotected if ownership changes",
                "confidence": 0.55, "remediation": "Add mandatory prepayment on change of control",
            }
        has_prepayment = any(kw in full_text for kw in ["mandatory prepayment", "put option", "acceleration upon change"])
        if not has_prepayment:
            return {
                "triggered": True, "severity": "MEDIUM", "impact_weight": 6,
                "explanation": "Change of control clause exists but lacks mandatory prepayment/put option",
                "confidence": 0.70, "remediation": "Add bondholder put option at par on change of control",
            }
        return {"triggered": False}

    # H14 — Event of Default Narrow Definition
    def _check_h14(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        full_text = ctx.get("im_full_text", "").lower()
        search = full_text + " " + str(im.get("default_clauses", "")).lower()
        missing = [t for t in WEAK_DEFAULT_DEFINITION_EXCLUSIONS if t.lower() not in search]
        if len(missing) <= 1:
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH" if len(missing) >= 4 else "MEDIUM",
            "impact_weight": 8 if len(missing) >= 4 else 6,
            "explanation": f"Event of default excludes {len(missing)} standard triggers: {', '.join(missing[:5])}",
            "confidence": 0.65 if not full_text else 0.80,
            "remediation": "Expand EoD to include cross-default, MAC, auditor exit, downgrade triggers",
        }

    # H15 — Trustee Limited Powers
    def _check_h15(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        patterns = [
            "trustee.*(?:shall|may).*act.*(?:only|solely).*(?:upon|with).*(?:majority|consent)",
            "debenture trustee.*limited.*powers",
            "trustee.*(?:cannot|shall not).*(?:enforce|act).*without.*(?:majority|resolution)",
        ]
        if not any(re.search(p, full_text, re.IGNORECASE) for p in patterns):
            return {"triggered": False}
        return {
            "triggered": True, "severity": "MEDIUM", "impact_weight": 5,
            "explanation": "Trustee can act only upon majority bondholder approval — delays enforcement.",
            "confidence": 0.70,
            "remediation": "Request trustee powers to act independently in EoD situations",
        }

    # H16 — Promoter Support Letter (Non-binding)
    def _check_h16(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        full_text = ctx.get("im_full_text", "").lower()
        guarantee = str(im.get("guarantee_details", "")).lower()
        if not any(re.search(p, full_text + " " + guarantee, re.IGNORECASE) for p in COMFORT_LETTER_PHRASES):
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH", "impact_weight": 8,
            "explanation": "Promoter support is a comfort letter — no legal standing in NCLT proceedings.",
            "confidence": 0.80,
            "remediation": "Replace comfort letter with corporate guarantee from promoter group company",
        }

    # H17 — High Related Party Transactions
    def _check_h17(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        rpt_match = re.search(r"related\s*part[yies]+\s*(?:transaction|exposure|revenue).*?(\d+)%", full_text)
        if rpt_match:
            pct = int(rpt_match.group(1))
            if pct >= 25:
                return {
                    "triggered": True, "severity": "HIGH", "impact_weight": 7,
                    "explanation": f"Related party transactions {pct}% — high governance risk",
                    "confidence": 0.80, "source_text": rpt_match.group(0)[:100],
                    "remediation": "Monitor RPTs; add arm's length covenant",
                }
        for pattern in ["significant related party", "material related party", "majority.*revenue.*related"]:
            if re.search(pattern, full_text, re.IGNORECASE):
                return {
                    "triggered": True, "severity": "MEDIUM", "impact_weight": 6,
                    "explanation": "Significant related party transactions mentioned — governance risk",
                    "confidence": 0.55,
                    "remediation": "Request detailed RPT disclosure and arm's length confirmation",
                }
        return {"triggered": False}

    # H18 — Pledged Promoter Shares
    def _check_h18(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        pledge_match = re.search(r"promoter.*?pledge[d]?\s*(?:shares?|holding)*.*?(\d+)%", full_text)
        if pledge_match:
            pct = int(pledge_match.group(1))
            if pct >= 30:
                return {
                    "triggered": True, "severity": "HIGH", "impact_weight": 7,
                    "explanation": f"Promoter share pledge of {pct}% indicates financial stress",
                    "confidence": 0.85, "source_text": pledge_match.group(0)[:100],
                    "remediation": "Add promoter pledge covenant with 50% cap",
                }
            if pct >= 15:
                return {
                    "triggered": True, "severity": "MEDIUM", "impact_weight": 5,
                    "explanation": f"Moderate promoter pledge of {pct}% — monitor for increases",
                    "confidence": 0.80,
                }
        return {"triggered": False}

    # H19 — Senior Debt Above Bond
    def _check_h19(self, ctx: dict) -> dict:
        im = ctx["im_data"]
        full_text = ctx.get("im_full_text", "").lower()
        security = str(im.get("security_structure", "")).lower()
        charge_type = str(im.get("charge_type", "")).upper()
        senior_indicators = [
            "bank.*first charge", "term loan.*first charge",
            "bond.*second charge", "subordinated to.*bank",
            "rank below.*existing", "residual charge",
        ]
        found = any(re.search(p, full_text + " " + security, re.IGNORECASE) for p in senior_indicators)
        if charge_type == "SECOND":
            found = True
        if not found:
            return {"triggered": False}
        return {
            "triggered": True, "severity": "HIGH", "impact_weight": 9,
            "explanation": "Bank loans hold first charge — bond is effectively subordinated. Banks recover first in default.",
            "confidence": 0.80,
            "remediation": "Request pari passu first charge or adequate residual asset coverage",
        }

    # H20 — Intercreditor Agreement Missing
    def _check_h20(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        im = ctx["im_data"]
        security = str(im.get("security_structure", "")).lower()
        has_ica = any(kw in full_text for kw in ["intercreditor agreement", "inter-creditor agreement"])
        has_shared = any(re.search(p, full_text + " " + security, re.IGNORECASE) for p in CROSS_COLLATERAL_PHRASES)
        has_multi = any(kw in full_text for kw in ["other lenders", "bank facility", "consortium", "multiple creditors"])
        if (has_shared or has_multi) and not has_ica:
            return {
                "triggered": True, "severity": "HIGH", "impact_weight": 8,
                "explanation": "Multiple lenders / shared security but no ICA disclosed — enforcement waterfall unclear.",
                "confidence": 0.70, "remediation": "Request disclosed ICA with clear enforcement waterfall",
            }
        return {"triggered": False}

    # H21 — Rating Watch Negative
    def _check_h21(self, ctx: dict) -> dict:
        # No rating_history table in gateway, check bond's credit_rating field
        bond = ctx["bond"]
        rating = str(bond.get("credit_rating") or "").upper()
        if "WATCH" in rating or "NEGATIVE" in rating or "CWD" in rating:
            return {
                "triggered": True, "severity": "HIGH", "impact_weight": 8,
                "explanation": f"Bond credit rating contains watch/negative flag: {rating}",
                "confidence": 0.80,
                "remediation": "Reduce exposure; monitor next rating review",
            }
        full_text = ctx.get("im_full_text", "").lower()
        if any(kw in full_text for kw in ["rating watch", "negative outlook", "credit watch"]):
            return {
                "triggered": True, "severity": "HIGH", "impact_weight": 8,
                "explanation": "Rating watch or negative outlook mentioned in IM text",
                "confidence": 0.65,
            }
        return {"triggered": False}

    # H22 — Rating Based on External Support
    def _check_h22(self, ctx: dict) -> dict:
        full_text = ctx.get("im_full_text", "").lower()
        support_phrases = [
            r"rating.*(?:uplift|enhanced|supported).*(?:parent|government|group)",
            r"standalone.*(?:rating|credit).*(?:lower|weaker)",
            r"(?:parent|government).*support.*(?:factored|considered)",
            r"implicit.*(?:government|parental).*support",
        ]
        for pattern in support_phrases:
            if re.search(pattern, full_text, re.IGNORECASE):
                return {
                    "triggered": True, "severity": "MEDIUM", "impact_weight": 6,
                    "explanation": "Rating incorporates external support uplift — standalone credit may be weaker.",
                    "confidence": 0.65,
                    "remediation": "Assess standalone credit independently; stress-test without support",
                }
        return {"triggered": False}

    # H23 — Spread Widening
    def _check_h23(self, ctx: dict) -> dict:
        market = ctx["market"]
        bond = ctx["bond"]
        spread_raw = market.get("spread_over_gsec")
        if not spread_raw:
            return {"triggered": False}
        spread_bps = spread_raw * 100 if spread_raw < 10 else spread_raw  # normalise decimal → bps
        rating = str(bond.get("credit_rating") or "").replace("CRISIL ", "").replace("ICRA ", "").strip()
        typical = CREDIT_SPREAD_MATRIX.get(rating, {}).get("5Y", 100)
        excess = spread_bps - typical
        if excess > 150:
            return {
                "triggered": True, "severity": "HIGH" if excess > 250 else "MEDIUM",
                "impact_weight": 7 if excess > 250 else 6,
                "explanation": f"Bond spread {spread_bps:.0f}bps vs {rating} peer median {typical}bps (+{excess:.0f}bps). Market pricing elevated risk.",
                "confidence": 0.85,
                "remediation": "Investigate spread driver — potential credit deterioration",
            }
        return {"triggered": False}

    # H24 — Liquidity Dry-Up
    def _check_h24(self, ctx: dict) -> dict:
        market = ctx["market"]
        days_since = market.get("days_since_last_trade")
        volume = market.get("traded_volume") or 0
        if days_since and days_since > 30:
            return {
                "triggered": True, "severity": "MEDIUM", "impact_weight": 6,
                "explanation": f"No trades in {days_since} days — secondary market illiquid, exit risk elevated",
                "confidence": 0.85,
                "remediation": "Ensure exit liquidity via put option or broker commitment",
            }
        if volume == 0 and market.get("price_date"):
            return {
                "triggered": True, "severity": "MEDIUM", "impact_weight": 5,
                "explanation": "Zero trading volume — bond effectively illiquid in secondary market",
                "confidence": 0.70,
                "remediation": "Confirm market-making arrangement or alternative exit",
            }
        return {"triggered": False}

    # ── Utility ──────────────────────────────────────────────────────
    def _find_page_ref(self, full_text: str, keyword: str) -> Optional[str]:
        if not full_text or not keyword:
            return None
        idx = full_text.lower().find(keyword.lower())
        if idx < 0:
            return None
        page_count = full_text[:idx].count("\x0c") + 1
        return f"Page {page_count}" if page_count > 1 else None

    async def _store_heuristic_results(self, isin: str, results: list, score: float) -> None:
        try:
            triggered = [
                {"heuristic_id": r["heuristic_id"], "name": r["name"], "severity": r["severity"],
                 "impact_weight": r["impact_weight"], "explanation": r["explanation"],
                 "category": r["category"], "confidence": r["confidence"],
                 "remediation": r.get("remediation")}
                for r in results if r["triggered"]
            ]
            flags_json = json.dumps(triggered)
            all_json = json.dumps(results)
            existing = await fetch_one("SELECT id FROM bond_im_data WHERE isin = :isin", {"isin": isin})
            if existing:
                await execute_query(
                    "UPDATE bond_im_data SET risk_flags = :flags, heuristic_results = :all, "
                    "hidden_risk_score = :score, updated_at = :now WHERE isin = :isin",
                    {"flags": flags_json, "all": all_json, "score": score,
                     "now": date.today().isoformat(), "isin": isin}
                )
            else:
                await execute_query(
                    "INSERT INTO bond_im_data (isin, risk_flags, heuristic_results, hidden_risk_score, "
                    "im_parsed_at, updated_at) VALUES (:isin, :flags, :all, :score, :now, :now)",
                    {"isin": isin, "flags": flags_json, "all": all_json, "score": score,
                     "now": date.today().isoformat()}
                )
        except Exception as e:
            pass  # Non-fatal — analysis result still returned to caller
