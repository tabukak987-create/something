"""
Unified Report Generator
Creates institutional-grade Markdown reports with alerts and data-driven analysis.
"""

from datetime import date
from typing import List, Dict, Any
from ..shared.database import fetch_all

class UnifiedReportGenerator:
    """Generates market analysis reports for the Unified Gateway."""

    def _deduplicate_by_issuer(self, bonds: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """Keep only the highest-scoring bond for each unique issuer."""
        seen_issuers = set()
        unique_bonds = []
        for b in bonds:
            issuer = b.get("issuer_name", "UNKNOWN")
            if issuer not in seen_issuers:
                unique_bonds.append(b)
                seen_issuers.add(issuer)
                if len(unique_bonds) >= limit:
                    break
        return unique_bonds

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        """Return a float-safe numeric value for report formatting."""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def generate_market_report(self) -> str:
        """Fetch the latest scores and format a full report with alerts."""

        # Get the latest scores joined with bond master and market data
        bonds = await fetch_all(
            """
            SELECT s.isin, s.iss_score, s.recommendation, s.overall_explanation,
                   s.value_score, s.liquidity_score, s.credit_score,
                   s.alm_fit_score, s.broker_score,
                   b.issuer_name, b.issuer_type, b.credit_rating, b.maturity_date,
                   b.coupon_rate, b.face_value, b.bond_name,
                   m.ytm, m.clean_price, m.spread_over_gsec, m.modified_duration,
                   m.dv01, m.liquidity_score as mkt_liq, m.data_source
            FROM   unified_bond_scores s
            JOIN   universal_bond_master b ON s.isin = b.isin
            JOIN   unified_market_data   m ON s.isin = m.isin
            WHERE  s.score_date = (SELECT MAX(score_date) FROM unified_bond_scores)
              AND  m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = s.isin)
            ORDER  BY s.iss_score DESC
            """
        )

        if not bonds:
            return "# Market Analysis Report\n\nNo scoring data found. Please run the scoring engine first."

        # Get alerts
        alerts = await fetch_all(
            """SELECT * FROM unified_alerts
               WHERE alert_date = :dt
               ORDER BY severity DESC, created_at DESC""",
            {"dt": date.today().isoformat()},
        ) or []

        today = date.today().strftime("%d %b %Y")

        # Categorize with Issuer Deduplication
        top_buys_all = [b for b in bonds if b["recommendation"] in ("STRONG_BUY", "BUY")]
        top_buys = self._deduplicate_by_issuer(top_buys_all, 10)

        gsec_all = [b for b in bonds if b["issuer_type"] in ("G-SEC", "T-BILL")]
        gsec = self._deduplicate_by_issuer(gsec_all, 5)

        sdl_all  = [b for b in bonds if b["issuer_type"] == "SDL"]
        sdl = self._deduplicate_by_issuer(sdl_all, 5)

        financials_all = [b for b in bonds if b["issuer_type"] in ("BANK", "NBFC")]
        financials = self._deduplicate_by_issuer(financials_all, 5)

        psu_infra_all = [b for b in bonds if b["issuer_type"] in ("PSU", "INFRA", "MUNICIPAL")]
        psu_infra = self._deduplicate_by_issuer(psu_infra_all, 5)

        corp_all = [b for b in bonds if b["issuer_type"] == "CORPORATE"]
        corp = self._deduplicate_by_issuer(corp_all, 5)

        # Statistics
        total = len(bonds)
        strong_buy_cnt = sum(1 for b in bonds if b["recommendation"] == "STRONG_BUY")
        buy_cnt = sum(1 for b in bonds if b["recommendation"] == "BUY")
        hold_cnt = sum(1 for b in bonds if b["recommendation"] == "HOLD")
        avg_ytm = sum(b.get("ytm", 0) for b in bonds if b.get("ytm")) / max(1, len([b for b in bonds if b.get("ytm")]))
        avg_iss = sum(b.get("iss_score", 0) for b in bonds) / max(1, total)

        report = [
            "# Indian Bond Market — Daily Investment Analysis Report",
            f"**Date:** {today}  |  **Bonds Analyzed:** {total}  |  **Engine:** Unified Gateway v3.0",
            "",
            f"**Signal Summary:** {strong_buy_cnt} STRONG_BUY | {buy_cnt} BUY | {hold_cnt} HOLD",
            f"**Avg YTM:** {avg_ytm:.2f}%  |  **Avg ISS:** {avg_iss:.1f}/100",
            "",
        ]

        # ─── ALERTS SECTION ──────────────────────────────────────────────────
        critical_alerts = [a for a in alerts if a.get("severity") == "CRITICAL"]
        warning_alerts = [a for a in alerts if a.get("severity") == "WARNING"]

        if critical_alerts or warning_alerts:
            report.append("## ⚠ Active Alerts")
            report.append("")
            if critical_alerts:
                report.append(f"### 🔴 Critical ({len(critical_alerts)})")
                for a in critical_alerts[:10]:
                    report.append(f"- **{a.get('title', '')}** — {a.get('detail', '')}")
                report.append("")
            if warning_alerts:
                report.append(f"### 🟡 Warnings ({len(warning_alerts)})")
                for a in warning_alerts[:10]:
                    report.append(f"- {a.get('title', '')} — {a.get('detail', '')}")
                report.append("")
            report.append("---\n")

        # ─── TOP PICKS ───────────────────────────────────────────────────────
        report.append("## 🎯 Executive Summary & Top Picks")
        report.append("The scoring engine has identified the following high-value opportunities.")
        report.append("")
        report.append("| ISIN | Issuer | Rating | YTM | Spread | ISS Score | Decision |")
        report.append("|:---|:---|:---|:---|:---|:---|:---|")

        if not top_buys:
            report.append("| -- | No BUY/STRONG_BUY signals found today | -- | -- | -- | -- | -- |")
        else:
            for b in top_buys:
                rec_label = f"`{b['recommendation']}`"
                if b['recommendation'] == 'STRONG_BUY': rec_label = f"**{rec_label}**"
                ytm = self._num(b.get('ytm'))
                spread = self._num(b.get('spread_over_gsec'))
                iss_score = self._num(b.get('iss_score'))
                report.append(
                    f"| {b['isin']} | {b['issuer_name'][:30]} | {b['credit_rating']} | "
                    f"{ytm:.2f}% | {spread:.2f}% | "
                    f"**{iss_score:.1f}** | {rec_label} |"
                )

        report.append("\n---\n")

        # ─── SOVEREIGN SECTION ───────────────────────────────────────────────
        report.append("## 🏛 Section 1: Sovereign & State (G-Sec / SDL)")
        report.append("Core safety assets for regulatory compliance.")
        report.append("\n| ISIN | Bond Name | YTM | Duration | G-Spread | Rating |")
        report.append("|:---|:---|:---|:---|:---|:---|")

        sov_bonds = gsec + sdl
        if not sov_bonds:
            report.append("| -- | No sovereign market data currently available | -- | -- | -- | -- |")
        else:
            for b in sov_bonds:
                ytm = self._num(b.get("ytm"))
                dur = self._num(b.get("modified_duration"))
                spread = self._num(b.get("spread_over_gsec"))
                report.append(
                    f"| {b['isin']} | {b['issuer_name'][:30]} | {ytm:.2f}% | "
                    f"{dur:.2f} | {spread:.2f}% | {b['credit_rating']} |"
                )

        report.append("\n---\n")

        # ─── CORPORATE SECTION ───────────────────────────────────────────────
        report.append("## 🏢 Section 2: Financials & Corporate Credit")
        report.append("Spread capture opportunities in high-quality credit segments.")
        report.append("\n| ISIN | Issuer | YTM | Rating | Spread | ISS | Rationale |")
        report.append("|:---|:---|:---|:---|:---|:---|:---|")

        corp_bonds = financials + corp + psu_infra
        if not corp_bonds:
            report.append("| -- | No corporate pricing data found | -- | -- | -- | -- | -- |")
        else:
            for b in corp_bonds:
                ytm = self._num(b.get('ytm'))
                spread = self._num(b.get('spread_over_gsec'))
                iss_score = self._num(b.get('iss_score'))
                report.append(
                    f"| {b['isin']} | {b['issuer_name'][:25]} | {ytm:.2f}% | "
                    f"{b['credit_rating']} | {spread:.2f}% | "
                    f"{iss_score:.1f} | {(b['overall_explanation'] or '')[:60]} |"
                )

        report.append("\n---\n")

        # ─── DETAILED ANALYSIS (Data-Driven) ─────────────────────────────────
        report.append("## 🔍 Detailed Analysis")
        report.append(f"The {total:,} bond universe was scanned for anomalies where yield persists above rating-peer medians.")
        report.append("")

        # G-Sec curve analysis
        if gsec:
            ytms = [b.get("ytm", 0) for b in gsec if b.get("ytm")]
            if ytms:
                min_ytm = min(ytms)
                max_ytm = max(ytms)
                report.append(f"- **Sovereign Curve:** G-Sec yields range {min_ytm:.2f}% to {max_ytm:.2f}% "
                              f"(spread: {(max_ytm - min_ytm)*100:.0f}bps across tenors)")

        # Corporate spread analysis
        if corp_bonds:
            corp_spreads = [b.get("spread_over_gsec", 0) for b in corp_bonds if b.get("spread_over_gsec")]
            if corp_spreads:
                avg_cs = sum(corp_spreads) / len(corp_spreads)
                report.append(f"- **Corporate Spreads:** Average spread {avg_cs:.2f}% over G-Sec across "
                              f"{len(corp_bonds)} high-quality issuers")

        # Score distribution
        report.append(f"- **Score Distribution:** {strong_buy_cnt} STRONG_BUY, {buy_cnt} BUY, "
                      f"{hold_cnt} HOLD — avg ISS {avg_iss:.1f}/100")

        # Top scoring dimensions
        if top_buys:
            top = top_buys[0]
            top_iss = self._num(top.get('iss_score'))
            top_ytm = self._num(top.get('ytm'))
            report.append(f"- **Top Pick:** {top['issuer_name']} ({top['isin']}) — "
                          f"ISS {top_iss:.1f}, YTM {top_ytm:.2f}%, "
                          f"Rating {top['credit_rating']}")

        report.append("\n---\n")

        # ─── SCORING METHODOLOGY ─────────────────────────────────────────────
        report.append("## 📊 Scoring Methodology")
        report.append("ISS (Institutional Suitability Score) — 5-dimension composite:")
        report.append("")
        report.append("| Dimension | Weight | Description |")
        report.append("|:---|:---|:---|")
        report.append("| Value | 25% | Spread vs rating peers, YTM attractiveness |")
        report.append("| ALM Fit | 25% | Maturity alignment to PF liability curve |")
        report.append("| Broker Opportunity | 20% | Broker quote discount, competition |")
        report.append("| Liquidity | 15% | Volume, bid-ask, exchange listing |")
        report.append("| Credit | 15% | Rating quality, outlook, momentum |")
        report.append("")
        report.append("*Report generated by Unified Bond Gateway v3.0*")

        return "\n".join(report)
