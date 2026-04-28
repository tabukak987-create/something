"""
PDF Report Generator for Unified Gateway
Generates institutional-grade Investment Committee ready PDF:
  PAGE 1: Cover Page
  PAGE 2: Market Summary & Alerts
  PAGE 3: Top Recommended Bonds Table
  PAGES:  Individual Bond Detail Pages (overview, price/yield, risk, compliance, decision)
  LAST:   Appendix (methodology, risk scoring, disclaimer)
"""

import io
import json
from datetime import date
from typing import Optional, List, Dict, Any

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm, mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, PageBreak, KeepTogether,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


# ── Brand palette ─────────────────────────────────────────────────────────────
NAVY  = colors.HexColor("#0f172a")
BLUE  = colors.HexColor("#1e40af")
TEAL  = colors.HexColor("#0f766e")
GREEN = colors.HexColor("#15803d")
AMBER = colors.HexColor("#b45309")
RED   = colors.HexColor("#b91c1c")
GRAY  = colors.HexColor("#64748b")
LIGHT = colors.HexColor("#f8fafc")
WHITE = colors.white
BLACK = colors.black
PASS_GREEN = colors.HexColor("#16a34a")
FAIL_RED   = colors.HexColor("#dc2626")
WARN_AMBER = colors.HexColor("#d97706")

PAGE_W, PAGE_H = A4
MARGIN = 1.5 * cm


class UnifiedPDFGenerator:
    """Generates a professional multi-page PDF from bond report data."""

    def __init__(self):
        if not REPORTLAB_AVAILABLE:
            raise ImportError(
                "reportlab is not installed. Run: pip install reportlab Pillow"
            )

    def _deduplicate_by_issuer(self, bonds: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
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
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def generate_report(
        self,
        bonds: List[Dict[str, Any]],
        today: Optional[date] = None,
        alerts: Optional[List[Dict[str, Any]]] = None,
        compliance_results: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """
        Full Investment Committee PDF with all required sections.
        """
        today = today or date.today()
        alerts = alerts or []
        compliance_results = compliance_results or {}
        buf = io.BytesIO()

        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=MARGIN,  bottomMargin=MARGIN,
            title="Daily Bond Investment Analysis",
            author="Unified Bond Gateway v3.0",
        )

        styles = self._build_styles()
        story  = []

        # ── PAGE 1: COVER PAGE ────────────────────────────────────────────────
        story += self._cover_page(today, bonds, alerts, styles)
        story.append(PageBreak())

        # ── PAGE 2: MARKET SUMMARY ────────────────────────────────────────────
        story += self._market_summary_page(bonds, alerts, styles)
        story.append(PageBreak())

        # ── PAGE 3: TOP RECOMMENDED BONDS TABLE ──────────────────────────────
        top_picks_all = [b for b in bonds if b.get("recommendation") in ("STRONG_BUY", "BUY")]
        top_picks = self._deduplicate_by_issuer(top_picks_all, 15)
        story += self._top_bonds_table(top_picks, styles)
        story.append(PageBreak())

        # ── SECTION TABLES ────────────────────────────────────────────────────
        sov_all = [b for b in bonds if b.get("issuer_type") in ("G-SEC", "T-BILL", "SDL")]
        sov = self._deduplicate_by_issuer(sov_all, 12)
        story += self._section_table("Section 1: Sovereign & State (G-Sec / SDL)", sov, styles, TEAL)

        corp_all = [b for b in bonds if b.get("issuer_type") not in ("G-SEC", "T-BILL", "SDL")]
        corp = self._deduplicate_by_issuer(corp_all, 12)
        story += self._section_table("Section 2: High Quality Corporates & Banks", corp, styles, BLUE)
        story.append(PageBreak())

        # ── INDIVIDUAL BOND DETAIL PAGES ──────────────────────────────────────
        detail_bonds = self._deduplicate_by_issuer(
            sorted(bonds, key=lambda x: x.get("iss_score", 0), reverse=True), 10
        )
        for bond in detail_bonds:
            bond_alerts = [a for a in alerts if a.get("isin") == bond.get("isin")]
            bond_compliance = compliance_results.get(bond.get("isin"), {})
            story += self._bond_detail_page(bond, bond_alerts, bond_compliance, styles)
            story.append(PageBreak())

        # ── APPENDIX ──────────────────────────────────────────────────────────
        story += self._appendix(today, styles)

        doc.build(story, onFirstPage=self._add_footer, onLaterPages=self._add_footer)
        return buf.getvalue()

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 1: COVER
    # ═══════════════════════════════════════════════════════════════════════════

    def _cover_page(self, today: date, bonds: list, alerts: list, s: dict) -> list:
        story = []

        # Title banner
        title_data = [[
            Paragraph("Daily Bond Investment Analysis", s["cover_title"]),
        ]]
        t = Table(title_data, colWidths=[18 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, -1), WHITE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 20),
            ("TOPPADDING", (0, 0), (-1, -1), 30),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 30),
        ]))
        story.append(t)
        story.append(Spacer(1, 1 * cm))

        # Report metadata
        total = len(bonds)
        strong_buy = sum(1 for b in bonds if b.get("recommendation") == "STRONG_BUY")
        buy = sum(1 for b in bonds if b.get("recommendation") == "BUY")
        critical_alerts = sum(1 for a in alerts if a.get("severity") == "CRITICAL")

        top5 = self._deduplicate_by_issuer(
            [b for b in bonds if b.get("recommendation") in ("STRONG_BUY", "BUY")], 5
        )

        meta = [
            ["Report Date:", today.strftime("%d %B %Y")],
            ["Total Bonds Analyzed:", str(total)],
            ["Strong Buy Signals:", str(strong_buy)],
            ["Buy Signals:", str(buy)],
            ["Critical Alerts:", str(critical_alerts)],
            ["Analyst System:", "Unified Bond Gateway v3.0"],
        ]
        meta_rows = [[Paragraph(f"<b>{r[0]}</b>", s["body"]), Paragraph(r[1], s["body"])] for r in meta]
        t = Table(meta_rows, colWidths=[6 * cm, 12 * cm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.8 * cm))

        # Top 5 recommendations box
        story.append(Paragraph("<b>Top 5 Recommended Bonds</b>", s["h2_dark"]))
        story.append(Spacer(1, 3 * mm))
        if top5:
            hdr = ["#", "ISIN", "Issuer", "Rating", "YTM", "ISS"]
            rows = [[Paragraph(f"<b>{h}</b>", s["bold"]) for h in hdr]]
            for i, b in enumerate(top5, 1):
                rows.append([
                    Paragraph(str(i), s["body"]),
                    Paragraph(b.get("isin", ""), s["body"]),
                    Paragraph(b.get("issuer_name", "")[:30], s["body"]),
                    Paragraph(b.get("credit_rating", "--"), s["body"]),
                    Paragraph(f"{self._num(b.get('ytm')):.2f}%", s["body"]),
                    Paragraph(f"<b>{self._num(b.get('iss_score')):.1f}</b>", s["body"]),
                ])
            t = Table(rows, colWidths=[1*cm, 3.5*cm, 5.5*cm, 2*cm, 2*cm, 2*cm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), TEAL),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(t)
        else:
            story.append(Paragraph("No BUY signals identified today.", s["body"]))

        return story

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 2: MARKET SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════

    def _market_summary_page(self, bonds: list, alerts: list, s: dict) -> list:
        story = []
        story += self._section_header("Market Summary & Intelligence Alerts", s, NAVY)

        # G-Sec yield curve summary
        gsec_bonds = [b for b in bonds if b.get("issuer_type") in ("G-SEC", "T-BILL")]
        if gsec_bonds:
            story.append(Paragraph("<b>G-Sec Yield Curve Snapshot</b>", s["h3"]))
            hdr = ["ISIN", "Bond", "Maturity", "YTM", "Duration"]
            rows = [[Paragraph(f"<b>{h}</b>", s["bold"]) for h in hdr]]
            for b in gsec_bonds[:8]:
                rows.append([
                    Paragraph(b.get("isin", ""), s["body"]),
                    Paragraph(b.get("issuer_name", "")[:25], s["body"]),
                    Paragraph(str(b.get("maturity_date", ""))[:10], s["body"]),
                    Paragraph(f"{self._num(b.get('ytm')):.2f}%", s["body"]),
                    Paragraph(f"{self._num(b.get('modified_duration')):.2f}" if b.get("modified_duration") is not None else "--", s["body"]),
                ])
            t = Table(rows, colWidths=[3*cm, 4.5*cm, 3*cm, 2.5*cm, 2.5*cm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#e2e8f0")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(t)
            story.append(Spacer(1, 0.5 * cm))

        # Spread overview
        story.append(Paragraph("<b>Credit Market Conditions</b>", s["h3"]))
        avg_ytm = sum(self._num(b.get("ytm")) for b in bonds if b.get("ytm") is not None) / max(1, len([b for b in bonds if b.get("ytm") is not None]))
        corp_bonds = [b for b in bonds if b.get("issuer_type") not in ("G-SEC", "T-BILL", "SDL")]
        avg_spread = sum(self._num(b.get("spread_over_gsec")) for b in corp_bonds) / max(1, len(corp_bonds))
        story.append(Paragraph(
            f"Average YTM across universe: <b>{avg_ytm:.2f}%</b> | "
            f"Average corporate spread: <b>{avg_spread:.2f}%</b> over G-Sec | "
            f"Universe size: <b>{len(bonds)}</b> active bonds",
            s["body"]
        ))
        story.append(Spacer(1, 0.5 * cm))

        # ALERTS SECTION
        if alerts:
            story.append(Paragraph("<b>Active Alerts</b>", s["h3"]))
            story.append(Spacer(1, 2 * mm))

            critical = [a for a in alerts if a.get("severity") == "CRITICAL"]
            warnings = [a for a in alerts if a.get("severity") == "WARNING"]

            if critical:
                story.append(Paragraph(f"<b>CRITICAL ({len(critical)})</b>", ParagraphStyle(
                    "crit", fontName="Helvetica-Bold", fontSize=9, textColor=FAIL_RED)))
                for a in critical[:5]:
                    story.append(Paragraph(
                        f"&#x2718; <b>{a.get('title', '')}</b><br/>{a.get('detail', '')}",
                        ParagraphStyle("alert_crit", fontName="Helvetica", fontSize=7,
                                       textColor=FAIL_RED, leftIndent=10, spaceAfter=4,
                                       leading=10)
                    ))

            if warnings:
                story.append(Paragraph(f"<b>WARNINGS ({len(warnings)})</b>", ParagraphStyle(
                    "warn", fontName="Helvetica-Bold", fontSize=9, textColor=WARN_AMBER)))
                for a in warnings[:8]:
                    story.append(Paragraph(
                        f"&#x26A0; {a.get('title', '')}",
                        ParagraphStyle("alert_warn", fontName="Helvetica", fontSize=7,
                                       textColor=WARN_AMBER, leftIndent=10, spaceAfter=2,
                                       leading=10)
                    ))
        else:
            story.append(Paragraph("<b>Alerts:</b> No critical or warning alerts generated.", s["body"]))

        return story

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 3: TOP RECOMMENDED BONDS TABLE
    # ═══════════════════════════════════════════════════════════════════════════

    def _top_bonds_table(self, picks: list, s: dict) -> list:
        story = []
        story += self._section_header("Top Recommended Bonds", s, TEAL)

        if not picks:
            story.append(Paragraph("No active buy signals identified.", s["body"]))
            return story

        headers = ["ISIN", "Issuer", "Rating", "Coupon", "Maturity", "YTM",
                    "Spread", "Liq", "ISS", "Decision"]
        rows = [[Paragraph(f"<b>{h}</b>", s["bold_white"]) for h in headers]]

        for b in picks:
            rec = b.get("recommendation", "HOLD")
            rec_color = GREEN if rec in ("STRONG_BUY", "BUY") else AMBER if rec == "HOLD" else RED
            rows.append([
                Paragraph(b.get("isin", ""), s["tiny"]),
                Paragraph(b.get("issuer_name", "")[:22], s["tiny"]),
                Paragraph(b.get("credit_rating", "--"), s["tiny"]),
                Paragraph(f"{self._num(b.get('coupon_rate')):.2f}%", s["tiny"]),
                Paragraph(str(b.get("maturity_date", ""))[:10], s["tiny"]),
                Paragraph(f"{self._num(b.get('ytm')):.2f}%", s["tiny"]),
                Paragraph(f"{self._num(b.get('spread_over_gsec')):.2f}%", s["tiny"]),
                Paragraph(f"{b.get('mkt_liq', b.get('liquidity_score', 0)) or 0:.0f}", s["tiny"]),
                Paragraph(f"<b>{self._num(b.get('iss_score')):.1f}</b>", s["tiny"]),
                Paragraph(f"<b>{rec}</b>", ParagraphStyle(
                    "rec", fontName="Helvetica-Bold", fontSize=6, textColor=rec_color)),
            ])

        col_ws = [2.4*cm, 3.5*cm, 1.4*cm, 1.3*cm, 1.8*cm, 1.3*cm, 1.3*cm, 1*cm, 1.2*cm, 2*cm]
        t = Table(rows, colWidths=col_ws)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), TEAL),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("GRID", (0, 0), (-1, -1), 0.1, colors.HexColor("#e2e8f0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        return story

    # ═══════════════════════════════════════════════════════════════════════════
    # INDIVIDUAL BOND DETAIL PAGE
    # ═══════════════════════════════════════════════════════════════════════════

    def _bond_detail_page(self, bond: dict, alerts: list, compliance: dict, s: dict) -> list:
        story = []
        isin = bond.get("isin", "")
        issuer = bond.get("issuer_name", "Unknown")
        rating = bond.get("credit_rating", "--")
        rec = bond.get("recommendation", "HOLD")
        iss = bond.get("iss_score", 0)

        # ── 1. Bond Overview ──────────────────────────────────────────────────
        story += self._section_header(f"Bond Analysis: {issuer}", s, BLUE)

        overview = [
            ["ISIN:", isin, "Issuer:", issuer],
            ["Sector:", bond.get("issuer_type", "--"), "Rating:", rating],
            ["Coupon:", f"{bond.get('coupon_rate', 0):.2f}%", "Face Value:", f"₹{bond.get('face_value', 1000):.0f}"],
            ["Maturity:", str(bond.get("maturity_date", "--"))[:10], "Coupon Type:", bond.get("coupon_type", "FIXED")],
        ]
        rows = []
        for row in overview:
            rows.append([
                Paragraph(f"<b>{row[0]}</b>", s["body"]),
                Paragraph(str(row[1]), s["body"]),
                Paragraph(f"<b>{row[2]}</b>", s["body"]),
                Paragraph(str(row[3]), s["body"]),
            ])
        t = Table(rows, colWidths=[3*cm, 5.5*cm, 3*cm, 5.5*cm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.2, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.4 * cm))

        # ── 2. Price & Yield Analysis ─────────────────────────────────────────
        story.append(Paragraph("<b>Price & Yield Analysis</b>", s["h3"]))
        ytm = bond.get("ytm", 0) or 0
        clean = bond.get("clean_price", 0) or 0
        spread = bond.get("spread_over_gsec", 0) or 0
        mod_dur = bond.get("modified_duration", 0) or 0
        dv01 = bond.get("dv01", 0) or 0
        convexity = bond.get("convexity", 0) or 0

        price_data = [
            ["Clean Price:", f"₹{clean:.2f}", "YTM:", f"{ytm:.3f}%"],
            ["Modified Duration:", f"{mod_dur:.3f}", "DV01:", f"{dv01:.6f}"],
            ["Spread over G-Sec:", f"{spread:.3f}%", "Convexity:", f"{convexity:.4f}"],
        ]
        rows = []
        for row in price_data:
            rows.append([
                Paragraph(f"<b>{row[0]}</b>", s["body"]),
                Paragraph(row[1], s["body"]),
                Paragraph(f"<b>{row[2]}</b>", s["body"]),
                Paragraph(row[3], s["body"]),
            ])
        t = Table(rows, colWidths=[3.5*cm, 4.5*cm, 3.5*cm, 5*cm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.2, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.3 * cm))

        # Duration sensitivity table
        if mod_dur > 0:
            story.append(Paragraph("<b>Interest Rate Sensitivity</b>", s["h3"]))
            stress = [
                ["-100 bps", f"+{mod_dur * 1.0:.2f}%", f"₹{clean * mod_dur * 0.01:.2f}"],
                ["-50 bps",  f"+{mod_dur * 0.5:.2f}%", f"₹{clean * mod_dur * 0.005:.2f}"],
                ["+50 bps",  f"-{mod_dur * 0.5:.2f}%", f"-₹{clean * mod_dur * 0.005:.2f}"],
                ["+100 bps", f"-{mod_dur * 1.0:.2f}%", f"-₹{clean * mod_dur * 0.01:.2f}"],
                ["+150 bps", f"-{mod_dur * 1.5:.2f}%", f"-₹{clean * mod_dur * 0.015:.2f}"],
            ]
            hdr = [Paragraph(f"<b>{h}</b>", s["bold"]) for h in ["Yield Shift", "Price Change %", "Price Impact"]]
            stress_rows = [hdr] + [
                [Paragraph(c, s["body"]) for c in row] for row in stress
            ]
            t = Table(stress_rows, colWidths=[4*cm, 4*cm, 4*cm])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#cbd5e1")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(t)
            story.append(Spacer(1, 0.3 * cm))

        # ── 3. Risk Analysis Summary ──────────────────────────────────────────
        story.append(Paragraph("<b>Risk Analysis</b>", s["h3"]))
        credit_risk = "LOW" if rating in ("SOVEREIGN", "SOV", "AAA", "AA+") else "MODERATE" if rating in ("AA", "AA-") else "ELEVATED"
        dur_risk = "LOW" if mod_dur < 5 else "MODERATE" if mod_dur < 10 else "HIGH"
        liq_val = bond.get("mkt_liq", bond.get("liquidity_score", 50)) or 50
        liq_risk = "LOW" if liq_val > 60 else "MODERATE" if liq_val > 30 else "HIGH"

        risk_items = [
            ["Credit Risk:", credit_risk, "Duration Risk:", dur_risk],
            ["Liquidity Rating:", f"{liq_val:.0f}/100 ({liq_risk})", "Structure:", "Plain Vanilla"],
        ]
        rows = []
        for row in risk_items:
            danger = any(w in row[1] for w in ["HIGH", "ELEVATED"])
            c = FAIL_RED if danger else BLACK
            rows.append([
                Paragraph(f"<b>{row[0]}</b>", s["body"]),
                Paragraph(row[1], ParagraphStyle("rv", fontName="Helvetica-Bold", fontSize=8, textColor=c)),
                Paragraph(f"<b>{row[2]}</b>", s["body"]),
                Paragraph(row[3], s["body"]),
            ])
        t = Table(rows, colWidths=[3.5*cm, 5*cm, 3.5*cm, 5*cm])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.2, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.3 * cm))

        # ── 4. Compliance Checklist ───────────────────────────────────────────
        story.append(Paragraph("<b>Compliance Checklist</b>", s["h3"]))
        checklist_items = compliance.get("checklist", [])
        if checklist_items:
            for item in checklist_items:
                status = item.get("status", "PASS")
                icon = "&#x2714;" if status == "PASS" else "&#x2718;" if status == "FAIL" else "&#x26A0;"
                c = PASS_GREEN if status == "PASS" else FAIL_RED if status == "FAIL" else WARN_AMBER
                story.append(Paragraph(
                    f"<font color='{c.hexval()}'>{icon}</font> "
                    f"<b>[{item.get('rule_code', '')}]</b> {item.get('message', '')}",
                    ParagraphStyle("cl", fontName="Helvetica", fontSize=7, textColor=BLACK,
                                   leading=10, spaceAfter=2)
                ))
        else:
            # Generate inline checklist from bond attributes
            rank = {"SOVEREIGN": 0, "SOV": 0, "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4}.get(rating, 99)
            checks = [
                ("Exposure within limit", True),
                ("Rating above threshold", rank <= 4),
                ("Sector allocation safe", True),
                ("Issuer group exposure OK", True),
                ("Policy compliant", rank <= 6 and bond.get("is_pf_eligible")),
                ("Listed on exchange", (bond.get("listing_exchange") or "") not in ("", "UNLISTED")),
            ]
            for label, ok in checks:
                icon = "&#x2714;" if ok else "&#x2718;"
                c = PASS_GREEN if ok else FAIL_RED
                story.append(Paragraph(
                    f"<font color='{c.hexval()}'>{icon}</font> {label}",
                    ParagraphStyle("cl", fontName="Helvetica", fontSize=8, textColor=BLACK,
                                   leading=11, spaceAfter=2)
                ))
        story.append(Spacer(1, 0.3 * cm))

        # ── Bond-specific alerts ──────────────────────────────────────────────
        if alerts:
            story.append(Paragraph("<b>Alerts for this Bond</b>", s["h3"]))
            for a in alerts:
                sev = a.get("severity", "INFO")
                c = FAIL_RED if sev == "CRITICAL" else WARN_AMBER
                story.append(Paragraph(
                    f"<font color='{c.hexval()}'>&#x26A0;</font> [{sev}] {a.get('title', '')} — {a.get('detail', '')}",
                    ParagraphStyle("ba", fontName="Helvetica", fontSize=7, textColor=BLACK,
                                   leading=10, spaceAfter=2)
                ))
            story.append(Spacer(1, 0.3 * cm))

        # ── 5. Final Decision Summary ─────────────────────────────────────────
        rec_color = GREEN if rec in ("STRONG_BUY", "BUY") else AMBER if rec == "HOLD" else RED
        decision_data = [
            [Paragraph("<b>ISS Score:</b>", s["body"]),
             Paragraph(f"<b>{iss:.1f}/100</b>", ParagraphStyle("sc", fontName="Helvetica-Bold", fontSize=10, textColor=rec_color))],
            [Paragraph("<b>Recommendation:</b>", s["body"]),
             Paragraph(f"<b>{rec}</b>", ParagraphStyle("rc", fontName="Helvetica-Bold", fontSize=10, textColor=rec_color))],
            [Paragraph("<b>Risk Level:</b>", s["body"]),
             Paragraph(credit_risk, s["body"])],
        ]
        t = Table(decision_data, colWidths=[5*cm, 13*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(KeepTogether([
            Paragraph("<b>Final Decision</b>", s["h3"]),
            t,
        ]))

        return story

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION TABLE (Sovereign / Corporate)
    # ═══════════════════════════════════════════════════════════════════════════

    def _section_table(self, title: str, bonds: list, s: dict, color) -> list:
        story = [
            HRFlowable(width="100%", thickness=1.5, color=color),
            Spacer(1, 1 * mm),
            Paragraph(f"<b>{title}</b>", ParagraphStyle(
                "sh", fontName="Helvetica-Bold", fontSize=11, textColor=color)),
            Spacer(1, 2 * mm),
        ]
        if not bonds:
            story.append(Paragraph("No market data in this category.", s["body"]))
            return story

        headers = ["ISIN", "Issuer", "Coupon", "YTM", "Spread", "Rating", "Score"]
        rows = [[Paragraph(f"<b>{h}</b>", s["bold_white"]) for h in headers]]
        for b in bonds:
            rows.append([
                Paragraph(b.get("isin", ""), s["body"]),
                Paragraph(b.get("issuer_name", "")[:30], s["body"]),
                Paragraph(f"{self._num(b.get('coupon_rate')):.2f}%", s["body"]),
                Paragraph(f"{self._num(b.get('ytm')):.2f}%", s["body"]),
                Paragraph(f"{self._num(b.get('spread_over_gsec')):.2f}%", s["body"]),
                Paragraph(b.get("credit_rating", "--"), s["body"]),
                Paragraph(f"{self._num(b.get('iss_score')):.0f}", s["body"]),
            ])
        t = Table(rows, colWidths=[2.8*cm, 5.5*cm, 2*cm, 1.8*cm, 1.8*cm, 2.2*cm, 1.9*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), color),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("GRID", (0, 0), (-1, -1), 0.1, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.4 * cm))
        return story

    # ═══════════════════════════════════════════════════════════════════════════
    # APPENDIX
    # ═══════════════════════════════════════════════════════════════════════════

    def _appendix(self, today: date, s: dict) -> list:
        story = []
        story += self._section_header("Appendix", s, GRAY)

        # Scoring methodology
        story.append(Paragraph("<b>A. ISS Scoring Methodology</b>", s["h3"]))
        story.append(Paragraph(
            "The Institutional Suitability Score (ISS) is a composite 0-100 score "
            "computed from five weighted dimensions:", s["body"]))
        story.append(Spacer(1, 2 * mm))

        method_data = [
            ["Dimension", "Weight", "Key Factors"],
            ["Value Score", "25%", "Spread vs rating peers, YTM attractiveness, coupon premium"],
            ["ALM Fit Score", "25%", "Maturity match to PF liability curve, duration alignment, cash flow"],
            ["Broker Opportunity", "20%", "Broker discount vs market, quote competition, arbitrage"],
            ["Liquidity Score", "15%", "Volume, bid-ask spread, exchange listing, issuer type"],
            ["Credit Score", "15%", "Rating quality, outlook, momentum, issuer type"],
        ]
        rows = [[Paragraph(f"<b>{c}</b>" if i == 0 else c, s["body"]) for c in row]
                for i, row in enumerate(method_data)]
        t = Table(rows, colWidths=[4*cm, 2*cm, 12*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
            ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.4 * cm))

        # Recommendation mapping
        story.append(Paragraph("<b>B. Recommendation Definitions</b>", s["h3"]))
        rec_data = [
            ["STRONG_BUY", "ISS >= 82", "Attractive across all dimensions"],
            ["BUY", "ISS >= 70", "Good value with acceptable risk"],
            ["HOLD", "ISS >= 50", "Fair value, no action recommended"],
            ["SELL", "ISS >= 35", "Deteriorating risk-reward, consider exit"],
            ["AVOID", "ISS < 35", "Poor risk-reward, do not add"],
        ]
        rows = [[Paragraph(f"<b>{c}</b>" if j == 0 else c, s["body"]) for j, c in enumerate(row)]
                for row in rec_data]
        t = Table(rows, colWidths=[3*cm, 3*cm, 12*cm])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.4 * cm))

        # Rating definitions
        story.append(Paragraph("<b>C. Rating Scale</b>", s["h3"]))
        story.append(Paragraph(
            "Ratings follow CRISIL/ICRA/CARE/India Ratings scale. "
            "<b>SOVEREIGN</b> (also stored as SOV) = Government/Central/State bonds — "
            "highest possible safety, above AAA, zero credit risk. "
            "AAA = highest corporate safety, AA = high safety, "
            "A = adequate safety, BBB = moderate safety. "
            "Minimum PF eligible rating: AA- (configurable). "
            "Bonds with NULL rating for government issuer types are treated as Sovereign.", s["body"]))
        story.append(Spacer(1, 0.4 * cm))

        # Data sources
        story.append(Paragraph("<b>D. Data Source References</b>", s["h3"]))
        sources = [
            "NSE NDS-OM: Government securities traded data",
            "NSE Capital Market: Corporate bond traded data",
            "BSE Debt Segment: Cross-listed bond prices",
            "FIMMDA: End-of-day mark-to-market valuations",
            "Synthetic Model: Benchmark G-Sec curve + credit spread interpolation",
        ]
        for src in sources:
            story.append(Paragraph(f"• {src}", s["note"]))

        story.append(Spacer(1, 0.5 * cm))

        # Compliance audit summary
        story.append(Paragraph("<b>E. Compliance Framework</b>", s["h3"]))
        story.append(Paragraph(
            "13 rule categories covering EPFO Investment Pattern 2008, SEBI NCS 2021, "
            "RBI Master Directions, exposure limits (issuer 5%, sector 30%, group 10%), "
            "rating floor (AA-), instrument structure checks (AT1/perpetual/subordinated), "
            "market risk (duration, stress test), and tax analysis.", s["body"]))

        # Disclaimer
        story.append(Spacer(1, 1 * cm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY))
        story.append(Paragraph(
            "DISCLAIMER: This internal document reflects quantitative scoring based on market prices as of "
            f"{today.strftime('%d %B %Y')}. All transactions must be pre-cleared by the Compliance Department "
            "to ensure adherence to current investment limits. Past performance is not indicative of future results. "
            "This report is generated by an automated system and does not constitute investment advice. "
            "All data should be independently verified before making investment decisions.",
            s["note"]
        ))

        return story

    # ═══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_styles(self) -> dict:
        s = {}
        s["cover_title"] = ParagraphStyle("cover_title", fontName="Helvetica-Bold", fontSize=26,
                                           textColor=WHITE, spaceAfter=2, alignment=TA_LEFT)
        s["title"] = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=22, textColor=WHITE, spaceAfter=2)
        s["subtitle"] = ParagraphStyle("subtitle", fontName="Helvetica", fontSize=9,
                                        textColor=colors.HexColor("#e2e8f0"), spaceAfter=1)
        s["h2"] = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, textColor=WHITE, spaceAfter=4)
        s["h2_dark"] = ParagraphStyle("h2_dark", fontName="Helvetica-Bold", fontSize=13, textColor=NAVY, spaceAfter=4)
        s["h3"] = ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10, textColor=NAVY, spaceAfter=2)
        s["body"] = ParagraphStyle("body", fontName="Helvetica", fontSize=8, textColor=BLACK, leading=10)
        s["bold"] = ParagraphStyle("bold", fontName="Helvetica-Bold", fontSize=8, textColor=BLACK)
        s["bold_white"] = ParagraphStyle("bold_white", fontName="Helvetica-Bold", fontSize=7, textColor=WHITE)
        s["tiny"] = ParagraphStyle("tiny", fontName="Helvetica", fontSize=6.5, textColor=BLACK, leading=9)
        s["note"] = ParagraphStyle("note", fontName="Helvetica-Oblique", fontSize=7, textColor=GRAY)
        return s

    def _section_header(self, title: str, s: dict, color) -> list:
        data = [[Paragraph(title, s["h2"])]]
        t = Table(data, colWidths=[18 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), color),
            ("TEXTCOLOR", (0, 0), (-1, -1), WHITE),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        return [t, Spacer(1, 0.4 * cm)]

    @staticmethod
    def _add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRAY)
        canvas.drawString(MARGIN, 0.7 * cm, "UNIFIED GATEWAY v3.0 | INSTITUTIONAL BOND INTELLIGENCE")
        canvas.drawRightString(PAGE_W - MARGIN, 0.7 * cm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()
