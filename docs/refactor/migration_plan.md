# Backend Domain-Driven Refactor: Migration Plan

## Detected Domains
Based on an analysis of `backend/app/services`, the backend logic naturally falls into the following isolated domains:
1. **compliance**: Enforces rules, limits, and post-trade validations.
2. **portfolio**: Manages allocations, portfolio state, and broad analytical datasets.
3. **market_data**: Ingests, fetches, and unifies exchange and information memorandum (IM) data.
4. **risk**: Performs stress testing, limit checking, and risk engine computations.
5. **core_trust**: Orchestrates decisions, maintains immutable audit ledgers, and handles four-eyes approvals.

## New Structure
```text
backend/app/domain/
├── compliance/
│   ├── __init__.py
│   ├── README.md
│   ├── engine.py
│   └── validators.py
├── portfolio/
│   ├── __init__.py
│   ├── README.md
│   ├── engine.py
│   └── analytics.py
├── market_data/
│   ├── __init__.py
│   ├── README.md
│   ├── fetchers.py
│   └── parsers.py
├── risk/
│   ├── __init__.py
│   ├── README.md
│   ├── engine.py
│   └── stress_test.py
└── core_trust/
    ├── __init__.py
    ├── README.md
    ├── orchestrator.py
    └── audit.py
```

## File Migration Plan
The following files from `backend/app/services` will be mapped and consolidated into the new structure:

### Compliance Domain
- `ips_engine.py`, `pf_compliance_engine.py` → `compliance/engine.py`
- `pretrade_validator.py`, `posttrade_monitor.py` → `compliance/validators.py`
- `compliance_hardened.py`, `compliance_rule_versioning.py` → `compliance/rules.py`

### Portfolio Domain
- `portfolio_institutional_engine.py`, `portfolio_dataset_service.py` → `portfolio/engine.py` (split heavily)
- `portfolio_analytics.py`, `portfolio_analyzer.py` → `portfolio/analytics.py`
- `allocation_optimizer.py` → `portfolio/allocator.py`

### Market Data Domain
- `live_market_fetcher.py`, `market_data_fetcher.py`, `exchange_trade_fetcher.py` → `market_data/fetchers.py`
- `im_fetcher.py`, `im_pipeline.py` → `market_data/im_ingestion.py`
- `broker_quote_parser.py` → `market_data/parsers.py`

### Risk Domain
- `risk_engine.py` → `risk/engine.py`
- `stress_testing.py` → `risk/stress_test.py`

### Core Trust Domain
- `decision_orchestrator.py`, `trust_decision_pipeline.py` → `core_trust/orchestrator.py`
- `audit_service.py`, `immutable_audit_ledger.py` → `core_trust/audit.py`
- `four_eyes_approval.py` → `core_trust/approvals.py`

## Violations to Fix During Migration
1. **Direct Cross-Domain Imports**: Services currently import directly from other services (e.g., portfolio importing risk). These will be routed through explicit API layers or domain interface boundaries.
2. **God Files**: Files like `portfolio_dataset_service.py` (>90KB) will be broken into logical helpers within `portfolio/`.
3. **Circular Dependencies**: Refactoring into discrete domains will enforce a strict directed acyclic graph (DAG) dependency flow.
