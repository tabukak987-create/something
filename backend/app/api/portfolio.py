from fastapi import APIRouter, HTTPException

from ..shared.logger import get_logger

log = get_logger("router.portfolio.legacy")
router = APIRouter(prefix="/portfolio", tags=["Portfolio Management (Legacy Disabled)"])


@router.api_route("/{legacy_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@router.api_route("", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def legacy_portfolio_endpoint_blocked(legacy_path: str = ""):
    """Hard block all legacy portfolio endpoints.

    Single source of truth is the institutional pipeline in routers/portfolio_v2.py.
    """
    attempted = f"/portfolio/{legacy_path}" if legacy_path else "/portfolio"
    log.warning("legacy_portfolio_endpoint_blocked", path=attempted)
    raise HTTPException(
        status_code=410,
        detail=(
            "Legacy portfolio endpoint removed. Use institutional endpoints under "
            "/api/v1/portfolio implemented by portfolio_v2."
        ),
    )
