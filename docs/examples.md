# Examples

## API

### Endpoint: `POST /api/v1/market-data/refresh/{isin}`
Input (Path parameter): 
`isin="INE002A08432"`

Output (JSON):
```json
{
  "status": "ok",
  "isin": "INE002A08432",
  "price": {
    "status": "refreshed",
    "last_price": 102.45
  },
  "im": {
    "status": "found",
    "stored": 1
  },
  "refreshed_at": "2026-04-28T10:30:00Z"
}
```

### Endpoint: `GET /api/v1/market-data/exchange/{isin}`
Input (Query/Path parameter):
`isin="INE002A08432"`, `limit=30`

Output (JSON):
```json
{
  "isin": "INE002A08432",
  "count": 1,
  "trades": [
    {
      "trade_date": "2026-04-28",
      "exchange": "NSE",
      "last_traded_price": 102.45,
      "volume": 500000
    }
  ],
  "fetched_at": "2026-04-28T10:30:00Z"
}
```

## Functions

### Function: `run_timed_task(task_group, task_name, coro)`
Input:
`task_group="market_data_fetcher"`, `task_name="SCHEDULED_REFRESH"`, `coro=fetcher.refresh_all()`

Output:
Returns the result of the awaited coroutine, automatically logging execution time and capturing exceptions.

## Data Models
- **universal_bond_master**: Central repository for ISINs (`isin`, `bond_name`, `issuer_name`, `credit_rating`, `coupon_rate`, `maturity_date`, `issuer_type`).
- **exchange_trades**: Tick-level trade data (`isin`, `trade_date`, `exchange`, `last_traded_price`, `volume`).
- **bond_im_registry**: Information Memorandum compliance log (`isin`, `source`, `filing_date`, `url`).
