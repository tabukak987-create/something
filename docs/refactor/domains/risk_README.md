# Risk Domain

## Purpose
The `risk` domain calculates financial risk exposures, stress testing, and Value at Risk (VaR). It models how the portfolio behaves under adverse scenarios.

## MUST NOT Do
- MUST NOT enforce strict compliance limits (that is the job of `compliance`, though `risk` may provide the numbers).
- MUST NOT execute any market transactions.

## Entry Files
- `engine.py`: Primary risk and VaR calculator.
- `stress_test.py`: Scenario analysis framework.

## Dependencies
- `portfolio` (for current holdings)
- `market_data` (for yield curves and volatilities)
