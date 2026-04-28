"""Forward rebalance planner to ensure year-end PF target convergence."""

from __future__ import annotations

from typing import Any


def generate_rebalance_plan(
    current_distribution: dict[str, float],
    target_distribution: dict[str, float],
    months_remaining: int,
    threshold: float = 2.0,
) -> dict[str, Any]:
    gaps = {
        "AAA_GSEC_SDL": round(float(target_distribution.get("AAA_GSEC_SDL") or 0.0) - float(current_distribution.get("AAA_GSEC_SDL") or 0.0), 4),
        "AA+": round(float(target_distribution.get("AA+") or 0.0) - float(current_distribution.get("AA+") or 0.0), 4),
        "AA": round(float(target_distribution.get("AA") or 0.0) - float(current_distribution.get("AA") or 0.0), 4),
    }

    actions: list[str] = []
    if gaps["AAA_GSEC_SDL"] > threshold:
        actions.append(
            "Increase future allocations to AAA/PSU/G-Sec/SDL and avoid incremental AA until AAA gap narrows."
        )
    if gaps["AA+"] < -threshold:
        actions.append("AA+ is above trajectory. Redirect upcoming inflows to AAA/G-Sec buckets.")
    if gaps["AA"] < -threshold:
        actions.append("AA is above trajectory. Stop new AA allocations and plan natural run-down.")

    if months_remaining <= 3 and gaps["AAA_GSEC_SDL"] > 0:
        actions.append("Final quarter acceleration: allocate majority of new cash to AAA/G-Sec/SDL.")

    if not actions:
        actions.append("Current tactical path is on-track for year-end PF target convergence.")

    return {
        "target_gap": gaps,
        "months_remaining": months_remaining,
        "actions": actions,
    }