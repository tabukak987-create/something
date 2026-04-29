"""
Calculation Transparency Engine
=================================
Provides full breakdown of every financial calculation:
  - Formula used (LaTeX + plain text)
  - Input values used
  - Assumptions made
  - Data sources referenced
  - Intermediate steps
  - Final result

Ensures every financial metric is fully reproducible and auditable
per SEBI/EPFO institutional requirements.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso

log = get_logger("service.calc_transparency")


class CalculationBreakdown:
    """Structured breakdown of a single financial calculation."""

    def __init__(
        self,
        metric_name: str,
        formula_text: str,
        formula_latex: str,
        inputs: Dict[str, Any],
        assumptions: List[str],
        data_sources: List[Dict[str, str]],
        steps: Optional[List[Dict[str, Any]]] = None,
        result: Any = None,
        unit: str = "",
        precision: int = 4,
    ):
        self.metric_name = metric_name
        self.formula_text = formula_text
        self.formula_latex = formula_latex
        self.inputs = inputs
        self.assumptions = assumptions
        self.data_sources = data_sources
        self.steps = steps or []
        self.result = result
        self.unit = unit
        self.precision = precision
        self.computed_at = utcnow_iso()

    def to_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "formula": {
                "text": self.formula_text,
                "latex": self.formula_latex,
            },
            "inputs": self.inputs,
            "assumptions": self.assumptions,
            "data_sources": self.data_sources,
            "steps": self.steps,
            "result": self.result,
            "unit": self.unit,
            "precision": self.precision,
            "computed_at": self.computed_at,
            "reproducible": True,
        }


class CalcTransparencyEngine:
    """
    Generates detailed calculation breakdowns for all financial metrics.
    """

    @staticmethod
    def ytm_breakdown(
        face_value: float,
        coupon_rate: float,
        clean_price: float,
        years_to_maturity: float,
        frequency: int = 2,
    ) -> CalculationBreakdown:
        """
        Yield to Maturity calculation with full transparency.
        Uses Newton-Raphson iterative method.
        """
        coupon = face_value * coupon_rate / 100
        period_coupon = coupon / frequency
        n_periods = int(years_to_maturity * frequency)

        # Approximate YTM using current yield + capital gain component
        current_yield = coupon / clean_price * 100
        capital_gain_yield = (face_value - clean_price) / years_to_maturity / clean_price * 100
        ytm_approx = current_yield + capital_gain_yield

        # Newton-Raphson refinement
        steps = []
        ytm = ytm_approx / 100
        for iteration in range(50):
            r = ytm / frequency
            if r <= 0:
                r = 0.0001
            pv = sum(period_coupon / (1 + r) ** t for t in range(1, n_periods + 1))
            pv += face_value / (1 + r) ** n_periods
            f_val = pv - clean_price

            # Derivative
            dpv = sum(-t * period_coupon / frequency / (1 + r) ** (t + 1) for t in range(1, n_periods + 1))
            dpv += -n_periods * face_value / frequency / (1 + r) ** (n_periods + 1)

            if abs(dpv) < 1e-12:
                break

            ytm_new = ytm - f_val / dpv
            steps.append({
                "iteration": iteration + 1,
                "ytm_estimate": round(ytm * 100, 6),
                "pv_cashflows": round(pv, 2),
                "error": round(f_val, 4),
                "converged": abs(f_val) < 0.001,
            })

            if abs(ytm_new - ytm) < 1e-10:
                ytm = ytm_new
                break
            ytm = ytm_new

        final_ytm = round(ytm * 100, 4)

        return CalculationBreakdown(
            metric_name="Yield to Maturity (YTM)",
            formula_text="YTM is the discount rate that equates PV of all future cash flows to the clean price. "
                         "P = Î£[C/(1+r)^t] + FV/(1+r)^n, solved for r using Newton-Raphson.",
            formula_latex=r"P_{clean} = \sum_{t=1}^{n} \frac{C/f}{(1+r/f)^t} + \frac{FV}{(1+r/f)^n}",
            inputs={
                "face_value": face_value,
                "coupon_rate_pct": coupon_rate,
                "clean_price": clean_price,
                "years_to_maturity": years_to_maturity,
                "coupon_frequency": frequency,
                "total_periods": n_periods,
                "period_coupon": round(period_coupon, 2),
            },
            assumptions=[
                "Flat yield curve assumed",
                "Coupons reinvested at YTM rate",
                "No default assumed before maturity",
                "Day count convention: 30/360",
                f"Compounding frequency: {frequency}x per year",
                "No embedded options (call/put) considered",
            ],
            data_sources=[
                {"source": "Portfolio Holdings", "field": "face_value, coupon_rate"},
                {"source": "Market Data Feed", "field": "clean_price"},
                {"source": "Bond Master", "field": "maturity_date, coupon_frequency"},
            ],
            steps=steps,
            result=final_ytm,
            unit="%",
            precision=4,
        )

    @staticmethod
    def duration_breakdown(
        face_value: float,
        coupon_rate: float,
        ytm: float,
        years_to_maturity: float,
        frequency: int = 2,
    ) -> CalculationBreakdown:
        """
        Macaulay Duration calculation with full transparency.
        """
        coupon = face_value * coupon_rate / 100
        period_coupon = coupon / frequency
        n_periods = int(years_to_maturity * frequency)
        r = ytm / 100 / frequency

        steps = []
        weighted_pv_sum = 0.0
        pv_sum = 0.0

        for t in range(1, n_periods + 1):
            cf = period_coupon if t < n_periods else period_coupon + face_value
            pv_cf = cf / (1 + r) ** t
            time_years = t / frequency
            weighted = time_years * pv_cf
            weighted_pv_sum += weighted
            pv_sum += pv_cf
            steps.append({
                "period": t,
                "time_years": round(time_years, 4),
                "cash_flow": round(cf, 2),
                "discount_factor": round(1 / (1 + r) ** t, 6),
                "pv_cash_flow": round(pv_cf, 2),
                "weighted_pv": round(weighted, 4),
            })

        mac_duration = weighted_pv_sum / pv_sum if pv_sum else 0
        mod_duration = mac_duration / (1 + r)

        return CalculationBreakdown(
            metric_name="Macaulay Duration & Modified Duration",
            formula_text="D_mac = Î£[t Ã— PV(CF_t)] / Î£[PV(CF_t)] where t is time in years. "
                         "D_mod = D_mac / (1 + r/f)",
            formula_latex=r"D_{mac} = \frac{\sum_{t=1}^{n} \frac{t}{f} \cdot \frac{CF_t}{(1+r/f)^t}}"
                          r"{\sum_{t=1}^{n} \frac{CF_t}{(1+r/f)^t}} \quad;\quad "
                          r"D_{mod} = \frac{D_{mac}}{1 + r/f}",
            inputs={
                "face_value": face_value,
                "coupon_rate_pct": coupon_rate,
                "ytm_pct": ytm,
                "years_to_maturity": years_to_maturity,
                "frequency": frequency,
                "total_periods": n_periods,
                "period_rate": round(r, 6),
            },
            assumptions=[
                "Parallel yield curve shift",
                "All coupons paid on schedule",
                "No optionality adjustments",
                f"Compounding: {frequency}x/year",
            ],
            data_sources=[
                {"source": "Calculated", "field": "YTM from bond pricing"},
                {"source": "Bond Master", "field": "coupon_rate, maturity_date"},
            ],
            steps=steps,
            result={
                "macaulay_duration_years": round(mac_duration, 4),
                "modified_duration": round(mod_duration, 4),
            },
            unit="years",
            precision=4,
        )

    @staticmethod
    def convexity_breakdown(
        face_value: float,
        coupon_rate: float,
        ytm: float,
        years_to_maturity: float,
        frequency: int = 2,
    ) -> CalculationBreakdown:
        """Convexity calculation with full transparency."""
        coupon = face_value * coupon_rate / 100
        period_coupon = coupon / frequency
        n_periods = int(years_to_maturity * frequency)
        r = ytm / 100 / frequency

        steps = []
        conv_sum = 0.0
        pv_sum = 0.0

        for t in range(1, n_periods + 1):
            cf = period_coupon if t < n_periods else period_coupon + face_value
            pv_cf = cf / (1 + r) ** t
            pv_sum += pv_cf
            conv_contrib = t * (t + 1) * pv_cf / (frequency ** 2)
            conv_sum += conv_contrib
            steps.append({
                "period": t,
                "cash_flow": round(cf, 2),
                "pv_cash_flow": round(pv_cf, 2),
                "t*(t+1)*PV": round(conv_contrib, 4),
            })

        convexity = conv_sum / (pv_sum * (1 + r) ** 2) if pv_sum else 0

        return CalculationBreakdown(
            metric_name="Convexity",
            formula_text="C = [Î£ t(t+1) Ã— PV(CF_t) / fÂ²] / [P Ã— (1+r/f)Â²]",
            formula_latex=r"C = \frac{1}{P \cdot (1+r/f)^2} \sum_{t=1}^{n} "
                          r"\frac{t(t+1)}{f^2} \cdot \frac{CF_t}{(1+r/f)^t}",
            inputs={
                "face_value": face_value,
                "coupon_rate_pct": coupon_rate,
                "ytm_pct": ytm,
                "years_to_maturity": years_to_maturity,
                "frequency": frequency,
                "total_pv": round(pv_sum, 2),
            },
            assumptions=[
                "Constant yield assumption",
                "No embedded options",
                f"Compounding frequency: {frequency}x/year",
            ],
            data_sources=[
                {"source": "Calculated", "field": "YTM, cash flow schedule"},
            ],
            steps=steps,
            result=round(convexity, 4),
            unit="yearsÂ²",
            precision=4,
        )

    @staticmethod
    def accrued_interest_breakdown(
        face_value: float,
        coupon_rate: float,
        last_coupon_date: str,
        settlement_date: str,
        frequency: int = 2,
        day_count: str = "30/360",
    ) -> CalculationBreakdown:
        """Accrued Interest calculation with full transparency."""
        lcd = datetime.strptime(last_coupon_date, "%Y-%m-%d").date()
        sd = datetime.strptime(settlement_date, "%Y-%m-%d").date()
        coupon_amount = face_value * coupon_rate / 100 / frequency

        if day_count == "30/360":
            days_accrued = (sd.year - lcd.year) * 360 + (sd.month - lcd.month) * 30 + min(sd.day, 30) - min(lcd.day, 30)
            period_days = 360 / frequency
        else:  # Actual/Actual
            days_accrued = (sd - lcd).days
            ncd = lcd + timedelta(days=int(365 / frequency))
            period_days = (ncd - lcd).days

        accrued = coupon_amount * days_accrued / period_days
        accrued_per_100 = accrued / face_value * 100

        return CalculationBreakdown(
            metric_name="Accrued Interest",
            formula_text=f"AI = (C/f) Ã— (days_accrued / days_in_period), using {day_count} convention",
            formula_latex=r"AI = \frac{C}{f} \times \frac{d_{settled}}{d_{period}}",
            inputs={
                "face_value": face_value,
                "coupon_rate_pct": coupon_rate,
                "coupon_per_period": round(coupon_amount, 2),
                "last_coupon_date": last_coupon_date,
                "settlement_date": settlement_date,
                "days_accrued": days_accrued,
                "period_days": period_days,
                "day_count_convention": day_count,
                "frequency": frequency,
            },
            assumptions=[
                f"Day count convention: {day_count}",
                f"Coupon frequency: {frequency}x/year",
                "Settlement on T+1 basis (Indian market standard)",
            ],
            data_sources=[
                {"source": "Bond Master", "field": "coupon_rate, coupon_frequency"},
                {"source": "Coupon Schedule", "field": "last_coupon_date"},
                {"source": "System", "field": "settlement_date (T+1)"},
            ],
            steps=[
                {"step": 1, "description": "Calculate annual coupon", "value": round(face_value * coupon_rate / 100, 2)},
                {"step": 2, "description": "Divide by frequency for period coupon", "value": round(coupon_amount, 2)},
                {"step": 3, "description": f"Count accrued days ({day_count})", "value": days_accrued},
                {"step": 4, "description": "Calculate accrual fraction", "value": round(days_accrued / period_days, 6)},
                {"step": 5, "description": "Accrued interest (absolute)", "value": round(accrued, 2)},
                {"step": 6, "description": "Accrued interest (per â‚¹100 face)", "value": round(accrued_per_100, 4)},
            ],
            result={
                "accrued_interest": round(accrued, 2),
                "accrued_per_100": round(accrued_per_100, 4),
            },
            unit="â‚¹",
            precision=2,
        )

    @staticmethod
    def clean_dirty_price_breakdown(
        clean_price: float,
        accrued_per_100: float,
        face_value: float,
    ) -> CalculationBreakdown:
        """Clean vs Dirty Price breakdown with full transparency."""
        dirty_price = clean_price + accrued_per_100
        market_value = dirty_price / 100 * face_value

        return CalculationBreakdown(
            metric_name="Clean Price vs Dirty Price",
            formula_text="Dirty Price = Clean Price + Accrued Interest (per â‚¹100). "
                         "Market Value = (Dirty Price / 100) Ã— Face Value",
            formula_latex=r"P_{dirty} = P_{clean} + AI_{per100} \quad;\quad "
                          r"MV = \frac{P_{dirty}}{100} \times FV",
            inputs={
                "clean_price": clean_price,
                "accrued_interest_per_100": accrued_per_100,
                "face_value": face_value,
            },
            assumptions=[
                "Clean price quoted on exchange/broker",
                "Accrued interest calculated using day count convention",
                "Settlement on T+1 basis",
            ],
            data_sources=[
                {"source": "Market Data / Broker Quote", "field": "clean_price"},
                {"source": "Calculated", "field": "accrued_interest"},
                {"source": "Portfolio Holdings", "field": "face_value"},
            ],
            steps=[
                {"step": 1, "description": "Start with clean price", "value": clean_price},
                {"step": 2, "description": "Add accrued interest per â‚¹100 face", "value": accrued_per_100},
                {"step": 3, "description": "Dirty price (invoice price)", "value": round(dirty_price, 4)},
                {"step": 4, "description": "Scale to face value for market value", "value": round(market_value, 2)},
            ],
            result={
                "clean_price": clean_price,
                "dirty_price": round(dirty_price, 4),
                "market_value": round(market_value, 2),
            },
            unit="â‚¹",
            precision=4,
        )

    @staticmethod
    def dv01_breakdown(
        dirty_price: float,
        face_value: float,
        modified_duration: float,
    ) -> CalculationBreakdown:
        """DV01 (Dollar Value of 01) calculation breakdown."""
        dv01 = dirty_price / 100 * face_value * modified_duration / 10000

        return CalculationBreakdown(
            metric_name="DV01 (Basis Point Value)",
            formula_text="DV01 = (Dirty Price / 100) Ã— Face Value Ã— Modified Duration / 10,000",
            formula_latex=r"DV01 = \frac{P_{dirty}}{100} \times FV \times \frac{D_{mod}}{10000}",
            inputs={
                "dirty_price": dirty_price,
                "face_value": face_value,
                "modified_duration": modified_duration,
            },
            assumptions=[
                "Linear approximation of price-yield relationship",
                "Valid for small parallel yield shifts only",
                "Ignores convexity adjustment for large moves",
            ],
            data_sources=[
                {"source": "Calculated", "field": "dirty_price, modified_duration"},
                {"source": "Portfolio Holdings", "field": "face_value"},
            ],
            steps=[
                {"step": 1, "description": "Market value", "value": round(dirty_price / 100 * face_value, 2)},
                {"step": 2, "description": "Scale by modified duration", "value": round(dirty_price / 100 * face_value * modified_duration, 2)},
                {"step": 3, "description": "Divide by 10,000 for per-basis-point", "value": round(dv01, 2)},
            ],
            result=round(dv01, 2),
            unit="â‚¹",
            precision=2,
        )

    @staticmethod
    def spread_over_gsec_breakdown(
        bond_ytm: float,
        gsec_yield: float,
        tenor_years: float,
        rating: str,
    ) -> CalculationBreakdown:
        """Credit spread over G-Sec benchmark breakdown."""
        spread_bps = (bond_ytm - gsec_yield) * 100

        return CalculationBreakdown(
            metric_name="Credit Spread over G-Sec",
            formula_text="Spread (bps) = (Bond YTM - Interpolated G-Sec Yield) Ã— 100",
            formula_latex=r"S = (Y_{bond} - Y_{GSec}) \times 100 \text{ bps}",
            inputs={
                "bond_ytm_pct": bond_ytm,
                "gsec_benchmark_yield_pct": gsec_yield,
                "tenor_years": tenor_years,
                "bond_rating": rating,
            },
            assumptions=[
                "G-Sec yield linearly interpolated from benchmark curve",
                "Spread reflects credit premium over risk-free rate",
                "No liquidity premium decomposition",
                "Benchmark: India Government Securities yield curve",
            ],
            data_sources=[
                {"source": "Calculated / Market", "field": "bond_ytm"},
                {"source": "RBI / CCIL / FIMMDA", "field": "gsec_yield_curve"},
                {"source": "Bond Master", "field": "maturity_date, rating"},
            ],
            steps=[
                {"step": 1, "description": "Bond YTM", "value": f"{bond_ytm}%"},
                {"step": 2, "description": f"G-Sec yield at {tenor_years}Y tenor", "value": f"{gsec_yield}%"},
                {"step": 3, "description": "Spread = Bond YTM âˆ’ G-Sec", "value": f"{round(bond_ytm - gsec_yield, 4)}%"},
                {"step": 4, "description": "Convert to basis points", "value": f"{round(spread_bps, 1)} bps"},
            ],
            result=round(spread_bps, 1),
            unit="bps",
            precision=1,
        )

    @staticmethod
    def var_breakdown(
        portfolio_value: float,
        portfolio_vol: float,
        confidence_level: float = 0.95,
        holding_period_days: int = 1,
    ) -> CalculationBreakdown:
        """Parametric Value-at-Risk breakdown."""
        normal = statistics.NormalDist()
        z_score = normal.inv_cdf(confidence_level)
        var = z_score * portfolio_vol * math.sqrt(holding_period_days) * portfolio_value
        es_phi = normal.pdf(z_score)
        es = es_phi / (1 - confidence_level) * portfolio_vol * portfolio_value

        return CalculationBreakdown(
            metric_name="Parametric Value-at-Risk (VaR)",
            formula_text=f"VaR({confidence_level*100}%) = Z_Î± Ã— Ïƒ_p Ã— âˆšt Ã— V. "
                         f"Expected Shortfall = Ï†(Z_Î±)/(1-Î±) Ã— Ïƒ_p Ã— V",
            formula_latex=r"VaR_\alpha = Z_\alpha \times \sigma_p \times \sqrt{t} \times V"
                          r"\quad;\quad ES_\alpha = \frac{\phi(Z_\alpha)}{1-\alpha} \times \sigma_p \times V",
            inputs={
                "portfolio_value": portfolio_value,
                "portfolio_volatility": portfolio_vol,
                "confidence_level": confidence_level,
                "holding_period_days": holding_period_days,
                "z_score": round(z_score, 4),
            },
            assumptions=[
                "Returns assumed normally distributed",
                "Volatility estimated from rating-based yield vol model",
                "Block-diagonal correlation: intra-rating=0.85, inter-rating=0.60, sov-corp=0.50",
                f"Holding period: {holding_period_days} day(s)",
                "No fat-tail adjustment (Cornish-Fisher not applied)",
            ],
            data_sources=[
                {"source": "Calculated", "field": "portfolio_value from mark-to-market"},
                {"source": "Risk Model", "field": "portfolio_volatility from yield vol model"},
                {"source": "Statistical Tables", "field": "z_score, normal PDF"},
            ],
            steps=[
                {"step": 1, "description": f"Z-score at {confidence_level*100}% confidence", "value": round(z_score, 4)},
                {"step": 2, "description": "Portfolio volatility (daily)", "value": f"{round(portfolio_vol*100, 4)}%"},
                {"step": 3, "description": f"Scale for {holding_period_days}-day holding", "value": round(math.sqrt(holding_period_days), 4)},
                {"step": 4, "description": "VaR (absolute â‚¹)", "value": round(var, 2)},
                {"step": 5, "description": "Expected Shortfall (CVaR)", "value": round(es, 2)},
            ],
            result={
                "var_amount": round(var, 2),
                "var_pct": round(var / portfolio_value * 100, 4) if portfolio_value else 0,
                "expected_shortfall": round(es, 2),
                "es_pct": round(es / portfolio_value * 100, 4) if portfolio_value else 0,
            },
            unit="â‚¹",
            precision=2,
        )

    @staticmethod
    def mark_to_market_breakdown(
        holdings: List[Dict],
    ) -> CalculationBreakdown:
        """Mark-to-Market portfolio valuation breakdown."""
        steps = []
        total_cost = 0
        total_market = 0

        for i, h in enumerate(holdings[:20]):  # Limit to 20 for readability
            fv = h.get("face_value", 0)
            clean = h.get("clean_price", 100)
            accrued = h.get("accrued_per_100", 0)
            dirty = clean + accrued
            mv = dirty / 100 * fv
            cost = h.get("cost_value", fv)
            total_cost += cost
            total_market += mv
            steps.append({
                "bond": h.get("isin", f"Bond_{i+1}"),
                "face_value": fv,
                "clean_price": clean,
                "accrued_interest": round(accrued, 2),
                "dirty_price": round(dirty, 2),
                "market_value": round(mv, 2),
                "cost_value": round(cost, 2),
                "unrealized_pnl": round(mv - cost, 2),
            })

        return CalculationBreakdown(
            metric_name="Mark-to-Market Valuation",
            formula_text="For each holding: MV = (Clean Price + Accrued Interest) / 100 Ã— Face Value. "
                         "Portfolio MV = Î£ MV_i",
            formula_latex=r"MV_i = \frac{P_{clean,i} + AI_i}{100} \times FV_i \quad;\quad "
                          r"MV_{portfolio} = \sum_{i=1}^{N} MV_i",
            inputs={
                "total_holdings": len(holdings),
                "valuation_date": date.today().isoformat(),
            },
            assumptions=[
                "Clean prices from latest available source (NSE > BSE > FIMMDA > Broker > Synthetic)",
                "Accrued interest calculated using 30/360 day count",
                "Holdings at face value from trade records",
                "Unrealized P&L = Market Value âˆ’ Cost Value",
            ],
            data_sources=[
                {"source": "unified_market_data", "field": "clean_price, price_source"},
                {"source": "portfolio_current_positions", "field": "face_value, buy_price"},
                {"source": "Calculated", "field": "accrued_interest"},
            ],
            steps=steps,
            result={
                "total_market_value": round(total_market, 2),
                "total_cost_value": round(total_cost, 2),
                "total_unrealized_pnl": round(total_market - total_cost, 2),
                "pnl_pct": round((total_market - total_cost) / total_cost * 100, 2) if total_cost else 0,
            },
            unit="â‚¹",
            precision=2,
        )

    @staticmethod
    def health_score_breakdown(
        credit_score: float,
        diversification_score: float,
        duration_score: float,
        liquidity_score: float,
        concentration_score: float,
    ) -> CalculationBreakdown:
        """Portfolio Health Score (0-100) breakdown with component weights."""
        total = (
            credit_score * 0.30 +
            diversification_score * 0.20 +
            duration_score * 0.15 +
            liquidity_score * 0.15 +
            concentration_score * 0.20
        )

        return CalculationBreakdown(
            metric_name="Portfolio Health Score",
            formula_text="Score = 0.30Ã—Credit + 0.20Ã—Diversification + 0.15Ã—Duration + 0.15Ã—Liquidity + 0.20Ã—Concentration",
            formula_latex=r"S = 0.30 \cdot S_{credit} + 0.20 \cdot S_{div} + 0.15 \cdot S_{dur} "
                          r"+ 0.15 \cdot S_{liq} + 0.20 \cdot S_{conc}",
            inputs={
                "credit_score": round(credit_score, 2),
                "diversification_score": round(diversification_score, 2),
                "duration_score": round(duration_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "concentration_score": round(concentration_score, 2),
            },
            assumptions=[
                "Weights calibrated for PF Trust risk appetite",
                "Credit: Higher = better average rating quality",
                "Diversification: Penalizes sector/issuer concentration",
                "Duration: Closer to target ALM = higher score",
                "Liquidity: More exchange-traded + higher volume = better",
                "Concentration: Lower single-name concentration = better",
            ],
            data_sources=[
                {"source": "Portfolio Analytics", "field": "computed from holdings"},
            ],
            steps=[
                {"component": "Credit Quality", "weight": 0.30, "score": round(credit_score, 2), "contribution": round(credit_score * 0.30, 2)},
                {"component": "Diversification", "weight": 0.20, "score": round(diversification_score, 2), "contribution": round(diversification_score * 0.20, 2)},
                {"component": "Duration Alignment", "weight": 0.15, "score": round(duration_score, 2), "contribution": round(duration_score * 0.15, 2)},
                {"component": "Liquidity", "weight": 0.15, "score": round(liquidity_score, 2), "contribution": round(liquidity_score * 0.15, 2)},
                {"component": "Concentration", "weight": 0.20, "score": round(concentration_score, 2), "contribution": round(concentration_score * 0.20, 2)},
            ],
            result=round(total, 2),
            unit="/100",
            precision=2,
        )

