"""
Bond UI Router — replaces bond_agent_server.py (Flask :5000)
============================================================
Exposes the three routes that bond_agent_ui.html depends on:

  GET  /ui/              → serves bond_agent_ui.html
  POST /ui/refresh       → runs BondInvestmentAgent, generates PDF
  GET  /ui/download-report → downloads the generated PDF
  GET  /ui/status        → report-ready flag + last analysis stats

All responses preserve the exact JSON shape produced by the legacy Flask server
so the existing frontend JavaScript works without any changes.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/ui", tags=["Bond Agent UI"])

# ── Paths ─────────────────────────────────────────────────────────────────────
_STATIC = Path(__file__).resolve().parents[1] / "static"
_BOND_UI = _STATIC / "bond_agent_ui.html"
_REPORT_PATH = Path("Daily_PF_Bond_Recommendation_Report.pdf")

# In-memory last-analysis store (reset on gateway restart — acceptable for UI)
_last_analysis: dict = {"recommendations": 0, "low_risk": 0, "medium_risk": 0, "date": None}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=FileResponse, include_in_schema=False)
async def bond_ui_index():
    """Serve the Bond Agent UI HTML."""
    if _BOND_UI.exists():
        return FileResponse(str(_BOND_UI))
    return JSONResponse({"error": "bond_agent_ui.html not found in static/"}, status_code=404)


@router.post("/refresh")
async def refresh_analysis():
    """
    Run BondInvestmentAgent, generate the PDF report, and return a summary.
    Response shape is identical to the legacy Flask /refresh endpoint.
    """
    global _last_analysis
    try:
        # Import lazily so the gateway starts even if optional deps (xhtml2pdf,
        # markdown2) are absent — callers get a clean error instead of a startup crash.
        from bond_im_agent import BondInvestmentAgent  # type: ignore[import]

        agent = BondInvestmentAgent()
        report_file = agent.generate_pdf_report()

        if report_file:
            low  = len([r for r in agent.recommendations if r["risk_category"] == "Low Risk"])
            med  = len([r for r in agent.recommendations if r["risk_category"] == "Medium Risk"])

            _last_analysis = {
                "recommendations": len(agent.recommendations),
                "low_risk": low,
                "medium_risk": med,
                "date": agent.report_date,
            }

            return JSONResponse({
                "success": True,
                "recommendations": len(agent.recommendations),
                "low_risk": low,
                "medium_risk": med,
                "date": agent.report_date,
                "filename": report_file,
            })
        else:
            return JSONResponse(
                {"success": False, "error": "Report generation failed"}, status_code=500
            )

    except ImportError as exc:
        return JSONResponse(
            {"success": False, "error": f"bond_im_agent not available: {exc}"},
            status_code=503,
        )
    except Exception as exc:
        return JSONResponse(
            {"success": False, "error": str(exc)}, status_code=500
        )


@router.get("/download-report")
async def download_report():
    """Download the most recently generated PDF report."""
    report = _REPORT_PATH
    if report.exists():
        filename = f"PF_Bond_Report_{datetime.now().strftime('%Y%m%d')}.pdf"
        return FileResponse(
            str(report),
            media_type="application/pdf",
            filename=filename,
        )
    return JSONResponse(
        {"error": "Report not found. Please run /ui/refresh first."}, status_code=404
    )


@router.get("/status")
async def status():
    """Return report-ready flag and last-analysis metadata."""
    return JSONResponse({
        "report_ready": _REPORT_PATH.exists(),
        "last_analysis": _last_analysis,
        "timestamp": datetime.utcnow().isoformat(),
    })
