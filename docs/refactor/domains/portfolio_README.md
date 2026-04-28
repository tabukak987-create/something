# Portfolio Domain

## Purpose
The `portfolio` domain tracks the system's state of holdings, allocations, and overall analytics for the institutional trust. It is the source of truth for "what we own" and "what we should own."

## MUST NOT Do
- MUST NOT execute market orders.
- MUST NOT directly query external APIs for pricing (delegate to `market_data`).
- MUST NOT bypass compliance checks when suggesting allocations.

## Entry Files
- `engine.py`: Core portfolio logic and dataset wrappers.
- `analytics.py`: Calculation of portfolio metrics (duration, yield, etc.).
- `allocator.py`: Logic for optimizing allocation.

## Dependencies
- `market_data` (for current bond valuations, read-only)
- `compliance` (for checking allocation limits)
