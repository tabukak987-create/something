"""
Institutional Reports Router
================================
API endpoints for generating and exporting institutional-grade reports.
All reports support JSON export with PDF generation capability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..shared.auth import require_role, Roles
from ..shared.logger import get_logger
from ..shared.time_utils import utcnow_iso
from ..services.institutional_reports import InstitutionalReportService
from ..utils.pdf_signer import get_pdf_signer

log = get_logger("router.reports")
router = APIRouter(prefix="/reports", tags=["Institutional Reports"])


@router.get("/portfolio-summary")
async def portfolio_summary_report(
    user: dict = Depends(require_role(Roles.TRUSTEE)),
):
    """Generate Portfolio Summary Report (board-ready)."""
    try:
        return await InstitutionalReportService.generate_portfolio_summary(user)
    except Exception as e:
        log.error("portfolio_summary_report_failed", error=str(e))
        raise HTTPException(500, f"Report generation failed: {str(e)}")


@router.get("/duration-ladder")
async def duration_ladder_report(
    user: dict = Depends(require_role(Roles.TRUSTEE)),
):
    """Generate Duration Ladder Report."""
    try:
        return await InstitutionalReportService.generate_duration_ladder(user)
    except Exception as e:
        log.error("duration_ladder_report_failed", error=str(e))
        raise HTTPException(500, f"Report generation failed: {str(e)}")


@router.get("/rating-distribution")
async def rating_distribution_report(
    user: dict = Depends(require_role(Roles.TRUSTEE)),
):
    """Generate Rating Distribution Report."""
    try:
        return await InstitutionalReportService.generate_rating_distribution(user)
    except Exception as e:
        log.error("rating_distribution_report_failed", error=str(e))
        raise HTTPException(500, f"Report generation failed: {str(e)}")


@router.get("/compliance-status")
async def compliance_status_report(
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Generate Compliance Status Report with breach summary."""
    try:
        return await InstitutionalReportService.generate_compliance_status(user)
    except Exception as e:
        log.error("compliance_status_report_failed", error=str(e))
        raise HTTPException(500, f"Report generation failed: {str(e)}")


@router.get("/audit-summary")
async def audit_summary_report(
    days: int = Query(default=30, le=365),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    """Generate Audit Log Summary Report with integrity verification."""
    try:
        return await InstitutionalReportService.generate_audit_summary(user, days)
    except Exception as e:
        log.error("audit_summary_report_failed", error=str(e))
        raise HTTPException(500, f"Report generation failed: {str(e)}")


@router.get("/quarterly-trustee")
async def quarterly_trustee_report(
    quarter: Optional[str] = None,
    user: dict = Depends(require_role(Roles.TRUSTEE)),
):
    """Generate comprehensive Quarterly Trustee Report."""
    try:
        return await InstitutionalReportService.generate_quarterly_trustee_report(user, quarter)
    except Exception as e:
        log.error("quarterly_trustee_report_failed", error=str(e))
        raise HTTPException(500, f"Report generation failed: {str(e)}")


@router.get("/history")
async def get_report_history(
    report_type: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    """Get report generation history with version tracking."""
    return await InstitutionalReportService.get_report_history(report_type, limit)


@router.get("/available")
async def list_available_reports(
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    """List all available institutional report types."""
    return {
        "reports": [
            {
                "id": "portfolio-summary",
                "name": "Portfolio Summary Report",
                "description": "Comprehensive portfolio overview with holdings, allocations, and risk metrics",
                "min_role": "TRUSTEE",
                "format": "JSON (PDF export available)",
            },
            {
                "id": "duration-ladder",
                "name": "Duration Ladder Report",
                "description": "Maturity/duration band breakdown with risk contribution analysis",
                "min_role": "TRUSTEE",
                "format": "JSON (PDF export available)",
            },
            {
                "id": "rating-distribution",
                "name": "Rating Distribution Report",
                "description": "Credit quality analysis with IG/sub-IG split and EPFO compliance",
                "min_role": "TRUSTEE",
                "format": "JSON (PDF export available)",
            },
            {
                "id": "compliance-status",
                "name": "Compliance Status Report",
                "description": "Current limit utilization, breach history, and compliance score",
                "min_role": "COMPLIANCE",
                "format": "JSON (PDF export available)",
            },
            {
                "id": "audit-summary",
                "name": "Audit Log Summary",
                "description": "Governance activity summary with integrity verification",
                "min_role": "AUDITOR",
                "format": "JSON (PDF export available)",
            },
            {
                "id": "quarterly-trustee",
                "name": "Quarterly Trustee Report",
                "description": "Board-ready quarterly review combining all data layers",
                "min_role": "TRUSTEE",
                "format": "JSON (PDF export available)",
            },
        ]
    }


class VerifySignatureRequest(BaseModel):
    signature: str
    reason: str = "Institutional Report"
    timestamp: str
    pdf_base64: Optional[str] = None


@router.post("/verify-signature")
async def verify_pdf_signature(
    req: VerifySignatureRequest,
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Verify a PDF digital signature (M3 — Digital Signing)."""
    signer = get_pdf_signer()
    if req.pdf_base64:
        import base64
        pdf_bytes = base64.b64decode(req.pdf_base64)
        result = signer.verify_signature(pdf_bytes, req.signature, req.reason, req.timestamp)
    else:
        result = {"valid": False, "details": "pdf_base64 is required for verification"}
    return result


@router.get("/signing-metadata")
async def signing_metadata(
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    """Get PDF digital signing configuration metadata."""
    return get_pdf_signer().get_signing_metadata()


@router.get("/audit-export/csv")
async def export_audit_csv(
    start_date: Optional[str] = Query(default=None, description="ISO date start filter"),
    end_date: Optional[str] = Query(default=None, description="ISO date end filter"),
    action_type: Optional[str] = Query(default=None, description="Filter by action type"),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    """
    Export audit log as CSV for regulatory submission.
    Returns downloadable CSV file with full audit trail.
    Immutable records — this export is itself audited.
    """
    from ..domain.core_trust.audit import AuditService
    import io

    try:
        csv_content = await AuditService.export_csv(start_date, end_date, action_type)
        output = io.BytesIO(csv_content.encode("utf-8-sig"))  # BOM for Excel compatibility
        filename = f"audit_export_{utcnow_iso().replace('-', '').replace(':', '').split('.')[0].replace('T', '_')}.csv"
        return StreamingResponse(
            output,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        log.error("audit_csv_export_failed", error=str(e))
        raise HTTPException(500, f"CSV export failed: {str(e)}")


@router.get("/audit-export/trust/csv")
async def export_trust_audit_csv(
    start_date: Optional[str] = Query(default=None, description="ISO date start filter"),
    end_date: Optional[str] = Query(default=None, description="ISO date end filter"),
    action_type: Optional[str] = Query(default=None, description="Filter by action type"),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    """Export canonical trust audit logs (audit_logs schema) as CSV."""
    from ..domain.core_trust.audit import AuditService
    import io

    try:
        csv_content = await AuditService.export_trust_csv(start_date, end_date, action_type)
        output = io.BytesIO(csv_content.encode("utf-8-sig"))
        filename = f"trust_audit_export_{utcnow_iso().replace('-', '').replace(':', '').split('.')[0].replace('T', '_')}.csv"
        return StreamingResponse(
            output,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        log.error("trust_audit_csv_export_failed", error=str(e))
        raise HTTPException(500, f"Trust CSV export failed: {str(e)}")


@router.get("/audit-export/trust/pdf")
async def export_trust_audit_pdf(
    start_date: Optional[str] = Query(default=None, description="ISO date start filter"),
    end_date: Optional[str] = Query(default=None, description="ISO date end filter"),
    action_type: Optional[str] = Query(default=None, description="Filter by action type"),
    limit: int = Query(default=1000, ge=1, le=5000),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    """Export canonical trust audit logs (audit_logs schema) as PDF."""
    from ..domain.core_trust.audit import AuditService
    import io

    try:
        pdf_bytes = await AuditService.export_trust_pdf(start_date, end_date, action_type, limit=limit)
        filename = f"trust_audit_export_{utcnow_iso().replace('-', '').replace(':', '').split('.')[0].replace('T', '_')}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        log.error("trust_audit_pdf_export_failed", error=str(e))
        raise HTTPException(500, f"Trust PDF export failed: {str(e)}")
