"""Deprecated legacy seed script.

This script wrote directly into unified_portfolio and is no longer safe.
Use the institutional dataset service pipeline instead.
"""

raise SystemExit(
    "DEPRECATED: seeds/ingest_investment_register.py is disabled. "
    "Use PortfolioDatasetService ingestion into instrument_master/portfolio_holdings/market_prices/cashflows."
)
