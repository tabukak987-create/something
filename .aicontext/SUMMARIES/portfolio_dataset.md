# Summary: backend/app/domain/portfolio/dataset.py (98KB)

## Overview
Central service for portfolio dataset ingestion, normalization, and auditing. Handles CSV/Excel parsing and synchronizes data into `instrument_master`, `universal_bond_master`, and `portfolio_positions`.

## Key Class: `PortfolioDatasetService`

### Static Constants
- `PORTFOLIO_SHEET_HINTS`: Mapping of common column names to canonical fields (e.g., 'bondname' -> 'bond_name').
- `COLUMN_ALIASES`: Extended aliases for flexible parsing.

### Core Methods (Async)
- `register_upload_batch(...)`: Registers a new file upload, computes SHA256, and stores raw content.
- `get_upload_batch_report(batch_id)`: Fetches batch status, validation reports, and row-level details.
- `parse_upload(...)`: Main entry point for parsing. Normalizes rows, validates against bond rules, and saves results to `portfolio_upload_rows`.
- `sync_upload(...)`: (Transactional) Persists accepted rows into the live database. Handles 'merge' vs 'replace' modes.
- `_ensure_upload_tables()`: Internal setup for database schema.

### Internal Logic
- `_canonicalize_row(raw_row)`: Normalizes keys and values (e.g., stripping whitespace, date conversion).
- `_cap_bucket_from_row(...)`: Infers category and sector buckets for regulatory reporting.
- `_infer_payment_frequency(ip_dates)`: Heuristic for determining coupon frequency from date strings.

## Database Tables Managed
- `portfolio_upload_batches`
- `portfolio_upload_rows`
- `portfolio_upload_versions`
- `portfolio_validation_reports`
- `portfolio_sheet_allocations`
