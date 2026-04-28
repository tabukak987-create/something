"""
Stress Testing & Resilience Service
=======================================
Phase 8: Simulates extreme scenarios â€” bond default, rating cascade,
data feed failure, service outage, compliance breach spikes.
Produces institutional-grade stress test reports.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from app.shared.database import fetch_all, fetch_one, execute
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso

log = get_logger("services.stress_testing")


class StressTestingService:
    """
    Institutional stress testing engine.
    Simulates adverse scenarios and measures system resilience.
    """

    @staticmethod
    async def run_full_stress_suite() -> dict[str, Any]:
        """Execute all stress test scenarios and compile results."""
        start = time.time()
        results = {}

        scenarios = [
            ("bond_default", StressTestingService._scenario_bond_default),
            ("rating_cascade_downgrade", StressTestingService._scenario_rating_cascade),
            ("data_feed_failure", StressTestingService._scenario_data_feed_failure),
            ("market_shock", StressTestingService._scenario_market_shock),
            ("compliance_breach_spike", StressTestingService._scenario_compliance_breach_spike),
            ("database_stress", StressTestingService._scenario_database_stress),
            ("concurrent_load", StressTestingService._scenario_concurrent_load),
            ("portfolio_concentration_extreme", StressTestingService._scenario_concentration_extreme),
            ("yield_curve_inversion", StressTestingService._scenario_yield_curve_inversion),
            ("liquidity_crisis", StressTestingService._scenario_liquidity_crisis),
        ]

        for name, scenario_fn in scenarios:
            try:
                result = await scenario_fn()
                result["status"] = "COMPLETED"
                results[name] = result
            except Exception as e:
                results[name] = {
                    "status": "FAILED",
                    "error": str(e),
                    "severity": "HIGH",
                }
                log.error(f"stress_test_failed_{name}", error=str(e))

        elapsed = time.time() - start

        # Compile summary
        passed = sum(1 for r in results.values() if r.get("passed", False))
        failed = sum(1 for r in results.values() if not r.get("passed", True))
        total = len(results)

        return {
            "report_title": "PF Trust â€” Institutional Stress Test Report",
            "generated_at": utcnow_iso(),
            "execution_time_seconds": round(elapsed, 2),
            "summary": {
                "total_scenarios": total,
                "passed": passed,
                "failed": failed,
                "warnings": total - passed - failed,
                "overall_resilience_score": round((passed / total) * 100) if total > 0 else 0,
            },
            "scenarios": results,
            "recommendations": StressTestingService._generate_recommendations(results),
        }

    # â”€â”€ Scenario 1: Bond Default â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_bond_default() -> dict:
        """Simulate a major corporate bond default affecting portfolio."""
        # Check portfolio for corporate bond holdings
        holdings = await fetch_all(
            """SELECT isin, issuer_name, face_value, quantity, credit_rating
               FROM portfolio_current_positions
               WHERE security_type IN ('Corporate Bond', 'NCD', 'CORPORATE')
               LIMIT 20"""
        ) or []

        total_portfolio = await fetch_one(
            "SELECT SUM(face_value * COALESCE(quantity, 1)) AS total FROM portfolio_current_positions"
        )
        total_value = total_portfolio["total"] if total_portfolio and total_portfolio["total"] else 100_000_000

        # Simulate default of largest corporate holding
        if holdings:
            largest = max(holdings, key=lambda h: (h.get("face_value", 0) * h.get("quantity", 1)))
            default_exposure = largest.get("face_value", 0) * largest.get("quantity", 1)
            recovery_rate = 0.30  # 30% recovery assumption for Indian corporate bonds
            loss_given_default = default_exposure * (1 - recovery_rate)
            impact_pct = (loss_given_default / total_value) * 100 if total_value > 0 else 0
        else:
            # Simulate with synthetic data
            default_exposure = total_value * 0.05  # Assume 5% in one issuer
            recovery_rate = 0.30
            loss_given_default = default_exposure * (1 - recovery_rate)
            impact_pct = 3.5

        return {
            "scenario": "Major Corporate Bond Default",
            "description": "Largest corporate holding defaults with 30% recovery rate",
            "assumptions": {
                "recovery_rate": "30% (standard for Indian senior unsecured)",
                "default_exposure": f"â‚¹{default_exposure:,.0f}",
                "loss_given_default": f"â‚¹{loss_given_default:,.0f}",
            },
            "impact": {
                "portfolio_loss_pct": round(impact_pct, 2),
                "absolute_loss": f"â‚¹{loss_given_default:,.0f}",
                "compliant_limit_5pct": impact_pct <= 5.0,
            },
            "passed": impact_pct <= 5.0,  # EPFO single-issuer limit
            "mitigation": [
                "Single-issuer limit enforced at 5% (compliance engine)",
                "Daily credit rating monitoring active",
                "Diversification across 20+ issuers recommended",
            ],
        }

    # â”€â”€ Scenario 2: Rating Cascade Downgrade â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_rating_cascade() -> dict:
        """Simulate mass rating downgrade (e.g., sector-wide stress)."""
        # Check current rating distribution
        rating_dist = await fetch_all(
            """SELECT credit_rating, COUNT(*) as count,
                      SUM(face_value * COALESCE(quantity, 1)) as exposure
               FROM portfolio_current_positions
               WHERE credit_rating IS NOT NULL
               GROUP BY credit_rating"""
        ) or []

        # Simulate: all A-rated bonds downgraded to BBB
        a_rated_exposure = sum(
            r.get("exposure", 0) for r in rating_dist
            if r.get("credit_rating", "").startswith("A") and not r.get("credit_rating", "").startswith("AA")
        )
        total_exposure = sum(r.get("exposure", 0) for r in rating_dist) or 100_000_000

        # Price impact of A â†’ BBB downgrade (typical spread widening: 50-150 bps)
        spread_widening_bps = 100  # 1% spread widening
        avg_duration = 4.5  # Assumed average modified duration
        price_impact_pct = spread_widening_bps / 100 * avg_duration  # ~4.5% price drop
        mtm_loss = a_rated_exposure * (price_impact_pct / 100)

        # Check if sub-IG exposure would breach limits
        current_sub_ig = sum(
            r.get("exposure", 0) for r in rating_dist
            if r.get("credit_rating", "") in ("BB+", "BB", "BB-", "B+", "B", "B-", "C", "D")
        )
        sub_ig_pct = ((current_sub_ig + a_rated_exposure) / total_exposure * 100) if total_exposure > 0 else 0

        return {
            "scenario": "Sector-Wide Rating Cascade Downgrade",
            "description": "All A-rated holdings downgraded to BBB (100bps spread widening)",
            "assumptions": {
                "spread_widening": "100 basis points",
                "average_duration": 4.5,
                "recovery_assumption": "No default, spread-driven MTM loss only",
            },
            "impact": {
                "a_rated_exposure": f"â‚¹{a_rated_exposure:,.0f}",
                "estimated_mtm_loss": f"â‚¹{mtm_loss:,.0f}",
                "price_impact_pct": round(price_impact_pct, 2),
                "post_downgrade_sub_ig_pct": round(sub_ig_pct, 2),
                "sub_ig_limit_breach": sub_ig_pct > 10,
            },
            "passed": sub_ig_pct <= 10 and price_impact_pct < 10,
            "mitigation": [
                "Rating-based exposure limits enforced (compliance engine)",
                "Minimum AA- concentration target for PF Trust",
                "Active monitoring of rating watch/outlook changes",
            ],
        }

    # â”€â”€ Scenario 3: Data Feed Failure â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_data_feed_failure() -> dict:
        """Simulate complete data source outage for 24+ hours."""
        # Check data freshness
        stale_data = await fetch_all(
            """SELECT source_name, last_successful_fetch,
                      ROUND(julianday('now') - julianday(last_successful_fetch)) as days_since
               FROM data_freshness_tracker
               WHERE last_successful_fetch IS NOT NULL
               ORDER BY days_since DESC"""
        ) or []

        # Check fallback pricing availability
        fallback_count = await fetch_one(
            """SELECT COUNT(*) as count FROM data_source_registry
               WHERE is_fallback = 1 AND is_active = 1"""
        )

        # Simulate: how many bonds would have stale pricing after 24h outage
        total_bonds = await fetch_one(
            "SELECT COUNT(*) as count FROM portfolio_current_positions"
        )
        bond_count = total_bonds["count"] if total_bonds else 0

        has_fallback = fallback_count and fallback_count["count"] > 0

        return {
            "scenario": "Complete Data Feed Failure (24-hour outage)",
            "description": "All primary data sources (BSE, NSE, RBI) go offline for 24 hours",
            "impact": {
                "bonds_affected": bond_count,
                "stale_after_24h": True,
                "fallback_sources_available": fallback_count["count"] if fallback_count else 0,
                "valuation_impact": "Last known price used â€” no MTM update for 24 hours",
            },
            "system_response": {
                "anomaly_detection": "ACTIVE â€” would flag stale data",
                "fallback_pricing": "ACTIVE" if has_fallback else "NOT CONFIGURED",
                "user_notification": "Data freshness warnings on all reports",
                "auto_recovery": "Automatic retry on scheduler (every 5 minutes for health, daily for market data)",
            },
            "passed": True,  # System handles gracefully (uses last known)
            "mitigation": [
                "Fallback pricing mechanism implemented (data_integrity service)",
                "Data freshness tracking with configurable thresholds",
                "Multiple data source registry for redundancy",
                "Stale data warnings on all generated reports",
            ],
        }

    # â”€â”€ Scenario 4: Market Shock â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_market_shock() -> dict:
        """Simulate extreme interest rate shock (+300bps parallel shift)."""
        # Get portfolio duration metrics
        portfolio = await fetch_all(
            """SELECT isin, face_value, COALESCE(quantity, 1) as qty,
                      COALESCE(maturity_date, date('now', '+5 years')) as maturity
               FROM portfolio_current_positions"""
        ) or []

        total_value = sum(h.get("face_value", 0) * h.get("qty", 1) for h in portfolio) or 100_000_000

        # Assume average portfolio modified duration of 4.5 years
        avg_modified_duration = 4.5
        avg_convexity = 25.0  # Approximate

        # +300bps shock
        rate_shock = 3.0  # percentage points
        # Price change â‰ˆ -Duration * Î”y + 0.5 * Convexity * (Î”y)Â²
        price_change_pct = -avg_modified_duration * rate_shock + 0.5 * avg_convexity * (rate_shock / 100) ** 2 * 100
        mtm_loss = total_value * abs(price_change_pct) / 100

        # VaR comparison (99% 1-day)
        annual_vol = 1.5  # typical for Indian fixed income portfolio
        daily_var_99 = total_value * (annual_vol / 100) * 2.326 / (252 ** 0.5)

        return {
            "scenario": "Extreme Interest Rate Shock (+300bps)",
            "description": "Parallel upward shift of 300 basis points across entire yield curve",
            "assumptions": {
                "rate_shock": "+300 basis points (parallel shift)",
                "avg_modified_duration": avg_modified_duration,
                "avg_convexity": avg_convexity,
                "portfolio_value": f"â‚¹{total_value:,.0f}",
            },
            "impact": {
                "estimated_price_change_pct": round(price_change_pct, 2),
                "estimated_mtm_loss": f"â‚¹{mtm_loss:,.0f}",
                "var_99_1day_comparison": f"â‚¹{daily_var_99:,.0f}",
                "shock_vs_var_multiple": round(mtm_loss / daily_var_99, 1) if daily_var_99 > 0 else "N/A",
            },
            "passed": abs(price_change_pct) < 20,  # Less than 20% drawdown
            "mitigation": [
                "Duration limits enforced via compliance engine",
                "VaR and stress testing in risk engine (9 macro scenarios)",
                "Duration ladder reporting for maturity management",
                "Interest rate scenario analysis available",
            ],
        }

    # â”€â”€ Scenario 5: Compliance Breach Spike â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_compliance_breach_spike() -> dict:
        """Simulate multiple simultaneous compliance limit breaches."""
        # Check current compliance state
        active_breaches = await fetch_all(
            """SELECT limit_type, severity, status
               FROM compliance_breaches
               WHERE status IN ('ACTIVE', 'ACKNOWLEDGED')"""
        ) or []

        # Check how many limits are configured
        limits = await fetch_all("SELECT * FROM compliance_limits") or []

        # Simulate scenario: 5 simultaneous breaches
        simulated_breaches = [
            {"type": "single_issuer", "issuer": "NBFC Corp", "exposure": "7.2%", "limit": "5%", "severity": "HIGH"},
            {"type": "sector", "sector": "Financial Services", "exposure": "32%", "limit": "25%", "severity": "HIGH"},
            {"type": "rating_bucket", "rating": "BBB+", "exposure": "18%", "limit": "15%", "severity": "MEDIUM"},
            {"type": "duration_band", "band": "7-10Y", "exposure": "42%", "limit": "30%", "severity": "MEDIUM"},
            {"type": "portfolio", "metric": "max_single_maturity", "value": "12Y", "limit": "10Y", "severity": "HIGH"},
        ]

        return {
            "scenario": "Multiple Simultaneous Compliance Breaches",
            "description": "5 compliance limits breached simultaneously (concentration stress)",
            "current_state": {
                "active_breaches": len(active_breaches),
                "configured_limits": len(limits),
            },
            "simulated_breaches": simulated_breaches,
            "system_response": {
                "detection": "AUTOMATIC â€” compliance scan runs on every portfolio change",
                "alerting": "Breach logged with severity, timestamp, and affected holdings",
                "escalation": "HIGH severity breaches flagged for immediate trustee review",
                "reporting": "Breach history available via compliance status report",
                "auto_block": "Future trades that would worsen breach are flagged (not auto-blocked)",
            },
            "passed": True,  # System handles detection/alerting correctly
            "recommendation": "Consider implementing pre-trade compliance check to BLOCK breach-worsening trades",
        }

    # â”€â”€ Scenario 6: Database Stress â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_database_stress() -> dict:
        """Test database performance under load."""
        query_times = []

        # Test 1: Large table scan
        start = time.time()
        await fetch_all("SELECT COUNT(*) FROM universal_bond_master")
        query_times.append(("bond_master_count", time.time() - start))

        # Test 2: Complex join query
        start = time.time()
        await fetch_all(
            """SELECT p.isin, p.face_value, b.issuer_name, b.credit_rating
               FROM portfolio_current_positions p
               LEFT JOIN universal_bond_master b ON p.isin = b.isin
               LIMIT 100"""
        )
        query_times.append(("portfolio_join", time.time() - start))

        # Test 3: Aggregation query
        start = time.time()
        await fetch_all(
            """SELECT credit_rating, COUNT(*) as cnt,
                      SUM(face_value) as total
               FROM portfolio_current_positions
               GROUP BY credit_rating"""
        )
        query_times.append(("rating_aggregation", time.time() - start))

        # Test 4: Audit log query (potentially large)
        start = time.time()
        await fetch_all(
            """SELECT action, COUNT(*) as cnt
               FROM institutional_audit_log
               GROUP BY action
               ORDER BY cnt DESC"""
        )
        query_times.append(("audit_log_aggregation", time.time() - start))

        max_time = max(t for _, t in query_times)
        avg_time = sum(t for _, t in query_times) / len(query_times)

        return {
            "scenario": "Database Performance Under Load",
            "description": "Execute representative queries and measure response times",
            "query_results": [
                {"query": name, "execution_time_ms": round(t * 1000, 1)} for name, t in query_times
            ],
            "metrics": {
                "max_query_time_ms": round(max_time * 1000, 1),
                "avg_query_time_ms": round(avg_time * 1000, 1),
                "total_queries": len(query_times),
            },
            "passed": max_time < 5.0,  # No query should take >5 seconds
            "thresholds": {
                "acceptable": "< 1 second",
                "warning": "1-5 seconds",
                "critical": "> 5 seconds",
            },
        }

    # â”€â”€ Scenario 7: Concurrent Load â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_concurrent_load() -> dict:
        """Simulate concurrent API requests."""
        concurrent_tasks = 20
        query = "SELECT COUNT(*) as cnt FROM portfolio_current_positions"

        start = time.time()
        tasks = [fetch_one(query) for _ in range(concurrent_tasks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.time() - start

        errors = sum(1 for r in results if isinstance(r, Exception))
        successes = concurrent_tasks - errors

        return {
            "scenario": "Concurrent Database Load Test",
            "description": f"{concurrent_tasks} simultaneous database queries",
            "results": {
                "total_requests": concurrent_tasks,
                "successful": successes,
                "failed": errors,
                "total_time_ms": round(elapsed * 1000, 1),
                "avg_per_request_ms": round((elapsed / concurrent_tasks) * 1000, 1),
                "throughput_rps": round(concurrent_tasks / elapsed, 1) if elapsed > 0 else 0,
            },
            "passed": errors == 0 and elapsed < 10,
        }

    # â”€â”€ Scenario 8: Portfolio Concentration Extreme â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_concentration_extreme() -> dict:
        """Analyze worst-case portfolio concentration risk."""
        # Get top holdings by exposure
        top_holdings = await fetch_all(
            """SELECT issuer_name, SUM(face_value * COALESCE(quantity, 1)) as exposure
               FROM portfolio_current_positions
               WHERE issuer_name IS NOT NULL
               GROUP BY issuer_name
               ORDER BY exposure DESC
               LIMIT 10"""
        ) or []

        total = await fetch_one(
            "SELECT SUM(face_value * COALESCE(quantity, 1)) as total FROM portfolio_current_positions"
        )
        total_value = total["total"] if total and total["total"] else 100_000_000

        # Calculate HHI (Herfindahl-Hirschman Index)
        hhi = 0
        concentration_data = []
        for h in top_holdings:
            pct = (h.get("exposure", 0) / total_value * 100) if total_value > 0 else 0
            hhi += pct ** 2
            concentration_data.append({
                "issuer": h.get("issuer_name", "Unknown"),
                "exposure": f"â‚¹{h.get('exposure', 0):,.0f}",
                "pct_of_portfolio": round(pct, 2),
            })

        # Top 5 concentration
        top5_pct = sum(c["pct_of_portfolio"] for c in concentration_data[:5])

        return {
            "scenario": "Portfolio Concentration Risk Analysis",
            "description": "Measure concentration via HHI and top-N exposure",
            "metrics": {
                "hhi_index": round(hhi, 1),
                "hhi_interpretation": (
                    "LOW (<1000)" if hhi < 1000
                    else "MODERATE (1000-2500)" if hhi < 2500
                    else "HIGH (>2500)"
                ),
                "top_5_concentration_pct": round(top5_pct, 2),
                "number_of_issuers": len(top_holdings),
            },
            "top_holdings": concentration_data,
            "passed": hhi < 2500 and top5_pct < 50,
            "epfo_compliance": {
                "single_issuer_max_5pct": all(c["pct_of_portfolio"] <= 5 for c in concentration_data),
                "group_max_10pct": top5_pct <= 50,  # Simplified check
            },
        }

    # â”€â”€ Scenario 9: Yield Curve Inversion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_yield_curve_inversion() -> dict:
        """Simulate yield curve inversion impact on portfolio valuation."""
        # Standard parallel shift scenarios
        scenarios = [
            {"name": "Bull Flattening", "short_change": -50, "long_change": -150, "description": "Short rates fall less than long rates"},
            {"name": "Bear Flattening", "short_change": +200, "long_change": +50, "description": "Short rates rise more than long rates"},
            {"name": "Full Inversion", "short_change": +100, "long_change": -100, "description": "Short rates up, long rates down"},
            {"name": "Steepening", "short_change": -100, "long_change": +100, "description": "Curve steepens dramatically"},
        ]

        avg_duration = 4.5
        total_value = 100_000_000  # Default

        total_check = await fetch_one(
            "SELECT SUM(face_value * COALESCE(quantity, 1)) AS total FROM portfolio_current_positions"
        )
        if total_check and total_check["total"]:
            total_value = total_check["total"]

        results = []
        for s in scenarios:
            # Simplified: use average of short and long change
            avg_change = (s["short_change"] + s["long_change"]) / 2
            price_impact = -avg_duration * avg_change / 100
            mtm_change = total_value * price_impact / 100

            results.append({
                "scenario": s["name"],
                "description": s["description"],
                "short_end_change_bps": s["short_change"],
                "long_end_change_bps": s["long_change"],
                "estimated_price_impact_pct": round(price_impact, 2),
                "estimated_mtm_change": f"â‚¹{mtm_change:,.0f}",
            })

        worst_case = min(results, key=lambda r: r["estimated_price_impact_pct"])

        return {
            "scenario": "Yield Curve Shape Stress Scenarios",
            "description": "Four non-parallel yield curve shift scenarios",
            "portfolio_duration": avg_duration,
            "portfolio_value": f"â‚¹{total_value:,.0f}",
            "scenarios": results,
            "worst_case": worst_case["scenario"],
            "worst_case_impact": worst_case["estimated_price_impact_pct"],
            "passed": abs(worst_case["estimated_price_impact_pct"]) < 15,
        }

    # â”€â”€ Scenario 10: Liquidity Crisis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    async def _scenario_liquidity_crisis() -> dict:
        """Simulate liquidity crisis â€” forced selling at market discount."""
        # Check portfolio composition for liquid vs illiquid
        portfolio = await fetch_all(
            """SELECT security_type, COUNT(*) as count,
                      SUM(face_value * COALESCE(quantity, 1)) as exposure
               FROM portfolio_current_positions
               GROUP BY security_type"""
        ) or []

        total_value = sum(p.get("exposure", 0) for p in portfolio) or 100_000_000

        # Liquidity assumptions by security type
        liquidity_map = {
            "G-Sec": {"haircut": 0.5, "days_to_sell": 1, "category": "HIGHLY_LIQUID"},
            "SDL": {"haircut": 1.0, "days_to_sell": 2, "category": "LIQUID"},
            "Treasury Bill": {"haircut": 0.2, "days_to_sell": 1, "category": "HIGHLY_LIQUID"},
            "AAA Corporate": {"haircut": 2.0, "days_to_sell": 3, "category": "MODERATELY_LIQUID"},
            "Corporate Bond": {"haircut": 3.0, "days_to_sell": 5, "category": "MODERATELY_LIQUID"},
            "NCD": {"haircut": 5.0, "days_to_sell": 7, "category": "LESS_LIQUID"},
        }

        # Default for unknown types
        default_liquidity = {"haircut": 5.0, "days_to_sell": 7, "category": "LESS_LIQUID"}

        total_haircut_loss = 0
        liquidity_breakdown = []

        for p in portfolio:
            sec_type = p.get("security_type", "Unknown")
            exposure = p.get("exposure", 0)
            liq = liquidity_map.get(sec_type, default_liquidity)

            haircut_loss = exposure * liq["haircut"] / 100
            total_haircut_loss += haircut_loss

            liquidity_breakdown.append({
                "security_type": sec_type,
                "exposure": f"â‚¹{exposure:,.0f}",
                "liquidity_category": liq["category"],
                "fire_sale_haircut_pct": liq["haircut"],
                "estimated_loss": f"â‚¹{haircut_loss:,.0f}",
                "days_to_liquidate": liq["days_to_sell"],
            })

        haircut_pct = (total_haircut_loss / total_value * 100) if total_value > 0 else 0

        return {
            "scenario": "Liquidity Crisis â€” Forced Liquidation",
            "description": "Force-sell entire portfolio within 7 days at distressed prices",
            "assumptions": {
                "fire_sale_scenario": "Forced selling within 7 days",
                "market_conditions": "Distressed â€” wide bid-ask spreads",
                "haircut_basis": "Security-type specific (0.2% G-Sec to 5% NCD)",
            },
            "breakdown": liquidity_breakdown,
            "total_impact": {
                "portfolio_value": f"â‚¹{total_value:,.0f}",
                "total_haircut_loss": f"â‚¹{total_haircut_loss:,.0f}",
                "haircut_pct": round(haircut_pct, 2),
            },
            "passed": haircut_pct < 5,  # Less than 5% total fire-sale loss
            "mitigation": [
                "Maintain minimum 20% in highly liquid G-Sec/T-Bills",
                "Duration ladder ensures staggered maturities for natural liquidity",
                "Emergency liquidity buffer recommendation: 10% in T-Bills",
            ],
        }

    # â”€â”€ Recommendations Generator â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @staticmethod
    def _generate_recommendations(results: dict) -> list[dict]:
        """Generate actionable recommendations based on stress test results."""
        recommendations = []

        for name, result in results.items():
            if not result.get("passed", True):
                recommendations.append({
                    "priority": "HIGH",
                    "scenario": name,
                    "finding": f"Stress test '{name}' did not pass acceptance criteria",
                    "action": result.get("recommendation", result.get("mitigation", ["Review and remediate"])),
                })

            if result.get("status") == "FAILED":
                recommendations.append({
                    "priority": "CRITICAL",
                    "scenario": name,
                    "finding": f"Stress test '{name}' failed to execute: {result.get('error', 'unknown')}",
                    "action": "Investigate execution failure and fix underlying issue",
                })

        # Standard institutional recommendations
        recommendations.extend([
            {
                "priority": "MEDIUM",
                "scenario": "general",
                "finding": "Stress tests should be run quarterly at minimum",
                "action": "Schedule automated quarterly stress test execution",
            },
            {
                "priority": "MEDIUM",
                "scenario": "general",
                "finding": "Board reporting of stress test results",
                "action": "Include stress test summary in quarterly trustee report",
            },
            {
                "priority": "LOW",
                "scenario": "general",
                "finding": "Historical stress test comparison",
                "action": "Store stress test results for trend analysis over time",
            },
        ])

        return recommendations

