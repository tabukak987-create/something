# Market Data Domain

## Purpose
The `market_data` domain is strictly responsible for ingesting, standardizing, and providing internal access to external data sources. This includes live quotes, exchange trades, news, and Information Memorandums (IM).

## MUST NOT Do
- MUST NOT contain any business rules about *whether* a bond is compliant.
- MUST NOT modify portfolio state.
- MUST NOT execute trades.

## Entry Files
- `fetchers.py`: Pulls data from external APIs (NSE, BSE, FBIL).
- `parsers.py`: Normalizes incoming data structures.
- `im_ingestion.py`: Specific pipeline for IM document retrieval.

## Dependencies
- None (Base domain). Operates completely independently.
