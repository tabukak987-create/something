"""Central router registration for the unified gateway.

Keeping router wiring in one place makes the app entrypoint smaller and keeps
security boundaries easier to audit.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from .auth import Roles, require_role
from .auth import router as auth_router
from ..api.alerts import router as alerts_router
from ..api.api_versioning import api_v2_router
from ..api.bond_agent import router as agent_router
from ..api.bond_ui import router as bond_ui_router
from ..api.compliance import router as compliance_router
from ..api.compliance_dashboard import router as compliance_dashboard_router
from ..api.decision_registry import router as decision_registry_router
from ..api.explainability import router as explainability_router
from ..api.datasources_ext import router as datasources_ext_router
from ..api.due_diligence import router as due_diligence_router
from ..api.governance import router as governance_router
from ..api.im_checker import router as im_checker_router
from ..api.institutional_ops import router as institutional_ops_router
from ..api.intelligence import router as intelligence_router
from ..api.ips_compliance import router as ips_compliance_router
from ..api.market_data import router as market_data_router
from ..api.news import router as news_router
from ..api.portfolio_v2 import router as portfolio_router
from ..api.quotes import router as quotes_router
from ..api.reports import router as reports_router
from ..api.risk import router as risk_router


def _viewer_gate() -> list[Depends]:
    return [Depends(require_role(Roles.VIEWER))]


def register_gateway_routes(app: FastAPI) -> None:
    """Mount all gateway routers with explicit baseline access control."""
    viewer_gate = _viewer_gate()

    app.include_router(intelligence_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(compliance_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(compliance_dashboard_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(decision_registry_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(explainability_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(quotes_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(agent_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(alerts_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(portfolio_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(risk_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(market_data_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(im_checker_router, dependencies=viewer_gate)
    app.include_router(news_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(governance_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(institutional_ops_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(reports_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(ips_compliance_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(datasources_ext_router, prefix="/api/v1", dependencies=viewer_gate)
    app.include_router(due_diligence_router, prefix="/api/v1", dependencies=viewer_gate)

    # Bond Agent UI — no auth gate (serves static HTML + PDF; matches legacy Flask :5000 behaviour)
    app.include_router(bond_ui_router)

    app.include_router(api_v2_router, prefix="/api/v2")
    app.include_router(auth_router, prefix="/api/v2")
    app.include_router(intelligence_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(compliance_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(compliance_dashboard_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(decision_registry_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(explainability_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(quotes_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(portfolio_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(reports_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(governance_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(ips_compliance_router, prefix="/api/v2", dependencies=viewer_gate)
    app.include_router(due_diligence_router, prefix="/api/v2", dependencies=viewer_gate)