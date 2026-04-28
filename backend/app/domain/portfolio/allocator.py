"""Portfolio allocation optimizer maximizing expected yield under PF constraints."""

from __future__ import annotations

from typing import Any

from app.domain.compliance.pf_engine import validate_portfolio


def optimize_allocation(
    bonds: list[dict[str, Any]],
    investment_amount: float,
    current_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current_positions = current_positions or []
    candidates = [b for b in bonds if b.get("score") is not None and float(b.get("score") or 0.0) > 0]
    if not candidates or investment_amount <= 0:
        return {
            "allocation_plan": [],
            "allocated_amount": 0.0,
            "unallocated_amount": float(investment_amount),
            "expected_yield": 0.0,
            "optimization_method": "none",
            "pf_check": validate_portfolio(current_positions),
        }

    # Try LP solver first; if unavailable, fallback to deterministic greedy.
    try:
        return _optimize_with_pulp(candidates, investment_amount, current_positions)
    except Exception:
        return _optimize_greedy(candidates, investment_amount, current_positions)


def _optimize_with_pulp(
    bonds: list[dict[str, Any]],
    investment_amount: float,
    current_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    from pulp import LpMaximize, LpProblem, LpVariable, lpSum, PULP_CBC_CMD

    problem = LpProblem("pf_bond_allocation", LpMaximize)
    x = {
        i: LpVariable(f"alloc_{i}", lowBound=0, upBound=investment_amount)
        for i in range(len(bonds))
    }

    # Objective: maximize weighted yield blended with model score.
    problem += lpSum(
        x[i] * (float(bonds[i].get("ytm") or 0.0) + float(bonds[i].get("score") or 0.0))
        for i in x
    )

    # Budget.
    problem += lpSum(x[i] for i in x) == investment_amount

    # Diversification safety: prevent single-bond concentration.
    max_single = 0.20 * investment_amount
    for i in x:
        problem += x[i] <= max_single

    # Solve silently with CBC.
    problem.solve(PULP_CBC_CMD(msg=False))

    plan = []
    for i in x:
        amt = float(x[i].value() or 0.0)
        if amt <= 0.0:
            continue
        item = dict(bonds[i])
        item["allocation_amount"] = round(amt, 2)
        plan.append(item)

    result = _finalize_plan(plan, investment_amount, current_positions, method="pulp")
    # Hard safety gate: never emit a non-compliant LP plan.
    if not result.get("pf_check", {}).get("pf_compliant", False):
        return _optimize_greedy(bonds, investment_amount, current_positions)
    return result


def _optimize_greedy(
    bonds: list[dict[str, Any]],
    investment_amount: float,
    current_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    remaining = float(investment_amount)
    per_bond_cap = 0.20 * investment_amount
    ranked = sorted(
        bonds,
        key=lambda b: (float(b.get("score") or 0.0), float(b.get("ytm") or 0.0)),
        reverse=True,
    )

    plan: list[dict[str, Any]] = []
    for b in ranked:
        if remaining <= 0:
            break
        alloc = min(per_bond_cap, remaining)
        item = dict(b)
        item["allocation_amount"] = round(alloc, 2)
        trial_positions = current_positions + plan + [item]
        pf = validate_portfolio(trial_positions)
        if pf["pf_compliant"]:
            plan.append(item)
            remaining -= alloc

    # If strict compliance blocked all allocations, create no-allocation response.
    if not plan:
        return _finalize_plan([], investment_amount, current_positions, method="greedy")

    # Fill remaining by topping up existing selected names while preserving compliance.
    i = 0
    while remaining > 0 and plan and i < 10_000:
        idx = i % len(plan)
        topup = min(remaining, max(1.0, investment_amount * 0.01))
        plan[idx]["allocation_amount"] += topup
        pf = validate_portfolio(current_positions + plan)
        if pf["pf_compliant"]:
            remaining -= topup
        else:
            plan[idx]["allocation_amount"] -= topup
        i += 1
        if i > len(plan) * 3 and remaining > 0:
            break

    return _finalize_plan(plan, investment_amount, current_positions, method="greedy")


def _finalize_plan(
    plan: list[dict[str, Any]],
    investment_amount: float,
    current_positions: list[dict[str, Any]],
    method: str,
) -> dict[str, Any]:
    allocated = sum(float(p.get("allocation_amount") or 0.0) for p in plan)
    blended_yield = 0.0
    if allocated > 0:
        blended_yield = sum(float(p.get("ytm") or 0.0) * float(p.get("allocation_amount") or 0.0) for p in plan) / allocated

    pf = validate_portfolio(current_positions + plan)
    return {
        "allocation_plan": sorted(plan, key=lambda x: float(x.get("allocation_amount") or 0.0), reverse=True),
        "allocated_amount": round(allocated, 2),
        "unallocated_amount": round(max(0.0, investment_amount - allocated), 2),
        "expected_yield": round(blended_yield, 6),
        "optimization_method": method,
        "pf_check": pf,
    }
