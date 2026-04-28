# Compliance Domain

## Purpose
The `compliance` domain is responsible for enforcing all Investment Policy Statement (IPS) rules, checking pre-trade constraints, and running post-trade compliance monitoring.

## MUST NOT Do
- MUST NOT fetch market data or quotes directly (use `market_data` interfaces).
- MUST NOT execute trades or alter portfolio holdings directly.
- MUST NOT perform heavy risk modeling (delegate to `risk`).

## Entry Files
- `engine.py`: The core compliance evaluation logic.
- `validators.py`: Specific pre/post trade validation checks.

## Dependencies
- `core_trust` (for audit logging)
- `portfolio` (for current holdings state, read-only)
