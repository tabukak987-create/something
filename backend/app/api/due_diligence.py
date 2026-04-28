"""
Gateway Router — Due Diligence & IC Reports
=============================================
Exposes new institutional due-diligence endpoints at the Unified Gateway (9005)
under /api/v1/due-diligence/

Endpoints:
  GET  /due-diligence/{isin}/classify
  GET  /due-diligence/{isin}/evaluate
  GET  /due-diligence/{isin}/sovereign
  GET  /due-diligence/{isin}/municipal
  GET  /due-diligence/{isin}/corporate
  POST /due-diligence/ic-note
  POST /due-diligence/comprehensive
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel

from ..shared.auth import require_role, get_current_user, Roles
from ..shared.database import fetch_all, fetch_one
from ..shared.logger import get_logger
from ..services.due_diligence_service import (
    BondClassificationEngine,
    CorporateBondEnhancementEngine,
    DueDiligenceEngine,
    ICReportGenerator,
    MunicipalBondEvaluationEngine,
    SovereignAnalysisEngine,
)

log = get_logger("router.due_diligence")

router = APIRouter(prefix="/due-diligence", tags=["Due Diligence"])


# ─── Models ───────────────────────────────────────────────────────

class BondEvaluationRequest(BaseModel):
    isin: str
    include_classification: bool = True
    include_due_diligence: bool = True
    include_sovereign: bool = True
    include_municipal: bool = True
    include_corporate: bool = True
    include_ic_note: bool = False
    compliance_regime: str = "epfo"


class ICNoteRequest(BaseModel):
    isin: str
    compliance_regime: str = "epfo"
    portfolio_context: Optional[Dict[str, Any]] = None


# ─── Helper: Load bond data from gateway DB ────────────────────────

async def _load_bond_data_gw(isin: str) -> Dict[str, Any]:
    """Load bond data from unified gateway database tables."""
    bond = await fetch_one(
        "SELECT * FROM universal_bond_master WHERE isin = :isin",
        {"isin": isin}
    )

    if not bond:
        raise HTTPException(status_code=404, detail=f"Bond {isin} not found")

    market = await fetch_one(
        """SELECT * FROM unified_market_data
           WHERE isin = :isin ORDER BY last_updated DESC LIMIT 1""",
        {"isin": isin}
    )

    # IM data from gateway tables
    im_data = None
    for table_name in ("bond_im_data", "gw_im_data"):
        try:
            im_data = await fetch_one(
                f"SELECT * FROM {table_name} WHERE isin = :isin",
                {"isin": isin},
            )
        except Exception:
            im_data = None
        if im_data:
            break

    # Parse JSON fields
    if im_data:
        for field in ["risk_flags", "covenants_financial", "key_financials"]:
            if im_data.get(field) and isinstance(im_data.get(field), str):
                try:
                    im_data[field] = json.loads(im_data[field])
                except Exception:
                    pass

    return {
        **dict(bond),
        "market_data": dict(market) if market else {},
        "im_data": dict(im_data) if im_data else {},
    }


def _add_engine_path():
    """Kept for compatibility; engines now live in local backend services."""
    return None


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT 1: Bond Classification
# ═══════════════════════════════════════════════════════════════════

@router.get("/{isin}/classify", summary="Classify bond into 4 institutional classes")
async def classify_bond(
    isin: str,
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Classify a bond as SOVEREIGN, STATE_GUARANTEED, MUNICIPAL, or CORPORATE.
    """
    try:
        bond_data = await _load_bond_data_gw(isin)
        _add_engine_path()
        engine = BondClassificationEngine()
        result = engine.classify(bond_data)
        return {"isin": isin, "classification": result}
    except HTTPException:
        raise
    except Exception as e:
        log.error("classify_bond_failed", isin=isin, error=str(e))
        raise HTTPException(status_code=500, detail=f"Classification failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT 2: Due Diligence Evaluation
# ═══════════════════════════════════════════════════════════════════

@router.get("/{isin}/evaluate", summary="11-factor institutional due diligence")
async def evaluate_bond(
    isin: str,
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Run 11-factor due diligence: nature of instrument, government support,
    financial position, utilisation of proceeds, repayment mechanism,
    business model, financial trend, credit rating, default history,
    legal compliance, risk assessment.
    """
    try:
        bond_data = await _load_bond_data_gw(isin)
        _add_engine_path()
        engine = DueDiligenceEngine()
        result = await engine.evaluate(bond_data)
        return result
    except HTTPException:
        raise
    except Exception as e:
        log.error("dd_evaluate_failed", isin=isin, error=str(e))
        raise HTTPException(status_code=500, detail=f"Due diligence evaluation failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT 3: Sovereign Analysis
# ═══════════════════════════════════════════════════════════════════

@router.get("/{isin}/sovereign", summary="Sovereign/SDL fiscal analysis")
async def analyze_sovereign(
    isin: str,
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Sovereign analysis for G-Sec/SDL: debt/GDP, fiscal deficit, FRBM limits,
    state fiscal metrics, interest rate risk, duration risk.
    """
    try:
        bond_data = await _load_bond_data_gw(isin)
        issuer_type = bond_data.get("issuer_type", "")
        if issuer_type not in ("G-SEC", "SDL"):
            raise HTTPException(
                status_code=400,
                detail=f"Sovereign analysis only for G-SEC/SDL, not {issuer_type}"
            )

        _add_engine_path()
        engine = SovereignAnalysisEngine()
        if issuer_type == "G-SEC":
            result = engine.analyze_gsec(bond_data)
        else:
            result = engine.analyze_sdl(bond_data)
        return result
    except HTTPException:
        raise
    except Exception as e:
        log.error("sovereign_analysis_failed", isin=isin, error=str(e))
        raise HTTPException(status_code=500, detail=f"Sovereign analysis failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT 4: Municipal Bond Evaluation
# ═══════════════════════════════════════════════════════════════════

@router.get("/{isin}/municipal", summary="SEBI municipal bond evaluation")
async def evaluate_municipal(
    isin: str,
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Municipal bond evaluation: revenue analysis, tax collection efficiency,
    escrow, DSCR, SEBI compliance, governance, urban growth potential.
    """
    try:
        bond_data = await _load_bond_data_gw(isin)
        _add_engine_path()
        engine = MunicipalBondEvaluationEngine()
        result = engine.evaluate(bond_data)
        return result
    except HTTPException:
        raise
    except Exception as e:
        log.error("municipal_eval_failed", isin=isin, error=str(e))
        raise HTTPException(status_code=500, detail=f"Municipal evaluation failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT 5: Corporate Bond Enhancement
# ═══════════════════════════════════════════════════════════════════

@router.get("/{isin}/corporate", summary="Corporate management, group support, security")
async def evaluate_corporate(
    isin: str,
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Corporate enhancement: management quality, promoter credibility,
    board governance (SEBI LODR), group support analysis,
    security structure, DRR compliance, trustee monitoring.
    """
    try:
        bond_data = await _load_bond_data_gw(isin)
        _add_engine_path()
        engine = CorporateBondEnhancementEngine()
        result = await engine.evaluate(bond_data)
        return result
    except HTTPException:
        raise
    except Exception as e:
        log.error("corporate_eval_failed", isin=isin, error=str(e))
        raise HTTPException(status_code=500, detail=f"Corporate evaluation failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT 6: IC Note Generation
# ═══════════════════════════════════════════════════════════════════

@router.post("/ic-note", summary="Generate Investment Committee note")
async def generate_ic_note(
    request: ICNoteRequest,
    user=Depends(require_role(Roles.PORTFOLIO)),
):
    """
    Generate structured IC note integrating all available analytics.
    Includes 8 sections: Issuer Overview, Bond Terms, Financial Analysis,
    Credit Rating, Risk Assessment, Security, Compliance Gates,
    Investment Recommendation.
    """
    try:
        bond_data = await _load_bond_data_gw(request.isin)
        _add_engine_path()

        analytics = {}

        # Classification
        try:
            analytics["classification"] = BondClassificationEngine().classify(bond_data)
        except Exception:
            pass

        # Due Diligence
        try:
            analytics["due_diligence"] = await DueDiligenceEngine().evaluate(bond_data)
        except Exception:
            pass

        # Sovereign
        issuer_type = bond_data.get("issuer_type", "")
        if issuer_type in ("G-SEC", "SDL"):
            try:
                sov_engine = SovereignAnalysisEngine()
                analytics["sovereign"] = (
                    sov_engine.analyze_gsec(bond_data)
                    if issuer_type == "G-SEC"
                    else sov_engine.analyze_sdl(bond_data)
                )
            except Exception:
                pass

        # Municipal
        bond_class = analytics.get("classification", {}).get("bond_class", "")
        if bond_class == "MUNICIPAL":
            try:
                analytics["municipal"] = MunicipalBondEvaluationEngine().evaluate(bond_data)
            except Exception:
                pass

        # Corporate
        if issuer_type not in ("G-SEC", "SDL"):
            try:
                analytics["corporate"] = await CorporateBondEnhancementEngine().evaluate(bond_data)
            except Exception:
                pass

        # Market data
        if bond_data.get("market_data"):
            analytics["market_data"] = bond_data["market_data"]

        ic_engine = ICReportGenerator()
        note = await ic_engine.generate_ic_note(
            bond_data=bond_data,
            analytics=analytics,
            portfolio_context=request.portfolio_context or {},
            compliance_regime=request.compliance_regime,
        )

        log.info(
            "ic_note_generated",
            isin=request.isin,
            recommendation=note.get("summary", {}).get("recommendation"),
            user=user.get("username"),
        )

        return note

    except HTTPException:
        raise
    except Exception as e:
        log.error("ic_note_failed", isin=request.isin, error=str(e))
        raise HTTPException(status_code=500, detail=f"IC note generation failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT 7: Comprehensive Evaluation
# ═══════════════════════════════════════════════════════════════════

@router.post("/comprehensive", summary="All due diligence engines in single call")
async def comprehensive_evaluation(
    request: BondEvaluationRequest,
    user=Depends(require_role(Roles.PORTFOLIO)),
):
    """
    Run all applicable due diligence engines in one API call.
    Returns combined output from classification, 11-factor DD,
    sovereign/municipal/corporate, and optionally IC note.
    """
    try:
        bond_data = await _load_bond_data_gw(request.isin)
        _add_engine_path()
        results = {
            "isin": request.isin,
            "issuer_name": bond_data.get("issuer_name", ""),
        }

        if request.include_classification:
            try:
                results["classification"] = BondClassificationEngine().classify(bond_data)
            except Exception as e:
                results["classification_error"] = str(e)

        if request.include_due_diligence:
            try:
                results["due_diligence"] = await DueDiligenceEngine().evaluate(bond_data)
            except Exception as e:
                results["due_diligence_error"] = str(e)

        issuer_type = bond_data.get("issuer_type", "")
        if request.include_sovereign and issuer_type in ("G-SEC", "SDL"):
            try:
                engine = SovereignAnalysisEngine()
                results["sovereign"] = (
                    engine.analyze_gsec(bond_data)
                    if issuer_type == "G-SEC"
                    else engine.analyze_sdl(bond_data)
                )
            except Exception as e:
                results["sovereign_error"] = str(e)

        bond_class = results.get("classification", {}).get("bond_class", "")
        if request.include_municipal and bond_class == "MUNICIPAL":
            try:
                results["municipal"] = MunicipalBondEvaluationEngine().evaluate(bond_data)
            except Exception as e:
                results["municipal_error"] = str(e)

        if request.include_corporate and issuer_type not in ("G-SEC", "SDL"):
            try:
                results["corporate"] = await CorporateBondEnhancementEngine().evaluate(bond_data)
            except Exception as e:
                results["corporate_error"] = str(e)

        if request.include_ic_note:
            try:
                analytics = {
                    k: v for k, v in results.items()
                    if k in ("classification", "due_diligence", "sovereign",
                            "municipal", "corporate")
                }
                if bond_data.get("market_data"):
                    analytics["market_data"] = bond_data["market_data"]
                results["ic_note"] = await ICReportGenerator().generate_ic_note(
                    bond_data=bond_data,
                    analytics=analytics,
                    compliance_regime=request.compliance_regime,
                )
            except Exception as e:
                results["ic_note_error"] = str(e)

        return results

    except HTTPException:
        raise
    except Exception as e:
        log.error("comprehensive_eval_failed", isin=request.isin, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
