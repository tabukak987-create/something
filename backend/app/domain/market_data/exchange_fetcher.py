"""
Exchange Trade Data Fetcher
============================
Pulls REAL corporate bond trade data from the NSE and BSE debt-market APIs.

Sources
-------
NSE (Corporate Bonds traded on exchange):
  Step-1  GET https://www.nseindia.com/          ← establish session + cookies
  Step-2  GET https://www.nseindia.com/api/liveBonds-traded-on-exchange?type=corporate
  History GET https://www.nseindia.com/api/historical-bonds-traded
              ?instrument=CB&fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY

BSE (Debt Market Trade Report):
  GET https://api.bseindia.com/BseIndiaAPI/api/CorporateBondsReports/w
      ?Type=EQ&Dtfrom=DD%2FMM%2FYYYY&DtTo=DD%2FMM%2FYYYY

Destinations
------------
  exchange_trades    — one row per ISIN per date per exchange (raw ticks)
  unified_market_data — updated with last_price / ytm when better than SYNTHETIC

Usage
-----
    from app.domain.market_data.exchange_fetcher import ExchangeTradeFetcher

    fetcher = ExchangeTradeFetcher()
    result  = await fetcher.fetch_today()          # NSE + BSE for today
    result  = await fetcher.fetch_historical(days=5)  # last N calendar days
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Optional

import httpx

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso
from app.shared.market_config import NSE_HEADERS, BSE_HEADERS

log = get_logger("exchange_trade_fetcher")

# ── Endpoints ─────────────────────────────────────────────────────────────────
_NSE_WARMUP    = "https://www.nseindia.com/"
_NSE_LIVE_URL  = ("https://www.nseindia.com/api/"
                  "liveBonds-traded-on-exchange?type=corporate")
_NSE_HIST_URL  = ("https://www.nseindia.com/api/"
                  "historical-bonds-traded"
                  "?instrument=CB&fromDate={from_date}&toDate={to_date}")
_BSE_CORP_URL  = ("https://api.bseindia.com/BseIndiaAPI/api/"
                  "CorporateBondsReports/w"
                  "?Type=EQ&Dtfrom={from_d}&DtTo={to_d}")

_REQUEST_TIMEOUT = 20  # seconds


class ExchangeTradeFetcher:
    """Fetches real bond trade data from NSE & BSE and persists to DB."""

    # ── NSE helpers ────────────────────────────────────────────────────────

    async def _nse_session(self) -> tuple[httpx.AsyncClient, dict]:
        """
        Returns an httpx client that has fetched the NSE homepage
        (required to set cookies) and a dict of ready-to-use headers.
        """
        client = httpx.AsyncClient(
            headers=NSE_HEADERS,
            follow_redirects=True,
            timeout=_REQUEST_TIMEOUT,
        )
        try:
            await client.get(_NSE_WARMUP)   # sets cookies
        except Exception as exc:
            log.warning("nse_warmup_failed", error=str(exc))
        return client, dict(client.headers)

    async def _fetch_nse_live(self) -> list[dict]:
        """Returns a list of parsed trade records from NSE live endpoint."""
        client, _ = await self._nse_session()
        try:
            resp = await client.get(_NSE_LIVE_URL)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log.warning("nse_live_fetch_failed", error=str(exc))
            return []
        finally:
            await client.aclose()

        # NSE response: {"data": [...], "timestamp": "..."}
        raw_items = payload.get("data") or []
        records: list[dict] = []
        today = date.today().isoformat()
        for item in raw_items:
            isin = item.get("isin") or item.get("ISIN")
            if not isin:
                continue
            records.append({
                "isin":             isin,
                "trade_date":       today,
                "exchange":         "NSE",
                "bond_name":        item.get("symbol") or item.get("description"),
                "last_price":       _f(item.get("lastPrice") or item.get("ltp")),
                "prev_close":       _f(item.get("previousClose")),
                "price_change":     _f(item.get("change")),
                "pct_change":       _f(item.get("pChange")),
                "high_52w":         _f(item.get("high52")),
                "low_52w":          _f(item.get("low52")),
                "total_volume":     _f(item.get("totalTradedQuantity")),
                "total_value":      _f(item.get("totalTradedValue")),
                "number_of_trades": _i(item.get("numberOfTrades") or item.get("totalTrades")),
                "ytm":              _f(item.get("yield") or item.get("ytm")),
                "open_price":       _f(item.get("open")),
                "high_price":       _f(item.get("dayHigh") or item.get("high")),
                "low_price":        _f(item.get("dayLow")  or item.get("low")),
            })
        return records

    async def _fetch_nse_historical(self, from_dt: date, to_dt: date) -> list[dict]:
        """Fetch NSE historical bond trade data for a date range."""
        url = _NSE_HIST_URL.format(
            from_date=from_dt.strftime("%d-%m-%Y"),
            to_date=to_dt.strftime("%d-%m-%Y"),
        )
        client, _ = await self._nse_session()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log.warning("nse_hist_fetch_failed", error=str(exc))
            return []
        finally:
            await client.aclose()

        raw_items = payload.get("data") or []
        records: list[dict] = []
        for item in raw_items:
            isin = item.get("isin") or item.get("ISIN")
            raw_date = item.get("tradeDate") or item.get("trdDt") or item.get("date")
            if not isin or not raw_date:
                continue
            trade_date = _parse_date(raw_date)
            records.append({
                "isin":         isin,
                "trade_date":   trade_date,
                "exchange":     "NSE",
                "bond_name":    item.get("symbol") or item.get("description"),
                "last_price":   _f(item.get("closedPrice") or item.get("lastPrice")),
                "prev_close":   _f(item.get("previousClose")),
                "price_change": _f(item.get("change")),
                "pct_change":   _f(item.get("pChange")),
                "total_volume": _f(item.get("totalTradedQuantity")),
                "total_value":  _f(item.get("totalTradedValue")),
                "ytm":          _f(item.get("yield") or item.get("ytm")),
            })
        return records

    # ── BSE helpers ────────────────────────────────────────────────────────

    async def _fetch_bse_trades(self, from_dt: date, to_dt: date) -> list[dict]:
        """Fetch corporate bond trades from BSE for a date range."""
        url = _BSE_CORP_URL.format(
            from_d=from_dt.strftime("%d%%2F%m%%2F%Y"),
            to_d=to_dt.strftime("%d%%2F%m%%2F%Y"),
        )
        try:
            async with httpx.AsyncClient(
                headers=BSE_HEADERS,
                follow_redirects=True,
                timeout=_REQUEST_TIMEOUT,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("bse_fetch_failed", error=str(exc))
            return []

        # BSE response: {"Table": [{...}]}  or  {"data": [...]}
        raw_items = (
            payload.get("Table")
            or payload.get("data")
            or []
        )
        records: list[dict] = []
        for item in raw_items:
            isin = item.get("ISIN") or item.get("Isin") or item.get("isin")
            raw_date = item.get("TradeDate") or item.get("Date") or item.get("DT")
            if not isin:
                continue
            trade_date = _parse_date(raw_date) if raw_date else from_dt.isoformat()
            records.append({
                "isin":             isin,
                "trade_date":       trade_date,
                "exchange":         "BSE",
                "bond_name":        (item.get("SecurityName") or
                                     item.get("COMPANY") or
                                     item.get("ScripName")),
                "last_price":       _f(item.get("LTP") or item.get("ClosePrice") or
                                       item.get("LastTradedPrice")),
                "prev_close":       _f(item.get("PrevClosePrice")),
                "price_change":     _f(item.get("Change")),
                "pct_change":       _f(item.get("PercentChange")),
                "open_price":       _f(item.get("OpenPrice")),
                "high_price":       _f(item.get("HighPrice")),
                "low_price":        _f(item.get("LowPrice")),
                "total_volume":     _f(item.get("TotalVolume") or item.get("Quantity")),
                "total_value":      _f(item.get("TotalValue") or item.get("TurnOver")),
                "number_of_trades": _i(item.get("NoOfTrades")),
                "ytm":              _f(item.get("YTM") or item.get("Yield")),
            })
        return records

    # ── Persistence ─────────────────────────────────────────────────────────

    async def _store_trades(self, records: list[dict]) -> int:
        """
        Upsert trade records into exchange_trades.
        Also back-fills unified_market_data for any ISIN where the
        exchange price is newer or the existing source is SYNTHETIC.
        Returns number of records inserted/updated.
        """
        stored = 0
        for rec in records:
            try:
                await execute_query(
                    """
                    INSERT INTO exchange_trades
                        (isin, trade_date, exchange, bond_name, last_price,
                         prev_close, price_change, pct_change, open_price,
                         high_price, low_price, high_52w, low_52w,
                         total_volume, total_value, number_of_trades, ytm)
                    VALUES
                        (:isin, :trade_date, :exchange, :bond_name, :last_price,
                         :prev_close, :price_change, :pct_change, :open_price,
                         :high_price, :low_price, :high_52w, :low_52w,
                         :total_volume, :total_value, :number_of_trades, :ytm)
                    ON CONFLICT(isin, trade_date, exchange) DO UPDATE SET
                        last_price      = excluded.last_price,
                        total_volume    = excluded.total_volume,
                        total_value     = excluded.total_value,
                        ytm             = excluded.ytm,
                        fetched_at      = datetime('now')
                    """,
                    {k: rec.get(k) for k in (
                        "isin", "trade_date", "exchange", "bond_name",
                        "last_price", "prev_close", "price_change", "pct_change",
                        "open_price", "high_price", "low_price", "high_52w",
                        "low_52w", "total_volume", "total_value",
                        "number_of_trades", "ytm",
                    )},
                )
                stored += 1

                # Promote into unified_market_data if ISIN exists in bond master
                if rec.get("last_price"):
                    await execute_query(
                        """
                        INSERT INTO unified_market_data
                            (isin, price_date, clean_price, ytm, data_source,
                             source_name, source_type, ingestion_method, source_reference,
                             timestamp_ingested, timestamp_effective, provenance_json,
                             traded_volume, last_updated)
                        SELECT
                            :isin, :trade_date, :last_price, :ytm, :src,
                            :source_name, :source_type, :ingestion_method, :source_reference,
                            :timestamp_ingested, :timestamp_effective, :provenance_json,
                            :vol, datetime('now')
                        WHERE EXISTS (
                            SELECT 1 FROM universal_bond_master WHERE isin = :isin
                        )
                        ON CONFLICT(isin, price_date) DO UPDATE SET
                            clean_price  = excluded.clean_price,
                            ytm          = COALESCE(excluded.ytm, unified_market_data.ytm),
                            data_source  = excluded.data_source,
                            source_name  = excluded.source_name,
                            source_type  = excluded.source_type,
                            ingestion_method = excluded.ingestion_method,
                            source_reference = excluded.source_reference,
                            timestamp_ingested = excluded.timestamp_ingested,
                            timestamp_effective = excluded.timestamp_effective,
                            provenance_json = excluded.provenance_json,
                            traded_volume= excluded.traded_volume,
                            last_updated = datetime('now')
                        WHERE unified_market_data.data_source = 'SYNTHETIC'
                           OR excluded.data_source IN ('NSE','BSE')
                        """,
                        {
                            "isin":       rec["isin"],
                            "trade_date": rec["trade_date"],
                            "last_price": rec.get("last_price"),
                            "ytm":        rec.get("ytm"),
                            "src":        rec["exchange"],
                            "vol":        rec.get("total_volume"),
                            "source_name": rec["exchange"],
                            "source_type": "public",
                            "ingestion_method": "api",
                            "source_reference": rec["exchange"],
                            "timestamp_ingested": utcnow_iso(),
                            "timestamp_effective": rec["trade_date"],
                            "provenance_json": json.dumps({
                                "source_name": rec["exchange"],
                                "source_type": "public",
                                "ingestion_method": "api",
                                "source_reference": rec["exchange"],
                                "timestamp_ingested": utcnow_iso(),
                                "timestamp_effective": rec["trade_date"],
                            }, default=str),
                        },
                    )
            except Exception as exc:
                log.warning("trade_store_error", isin=rec.get("isin"), error=str(exc))
        return stored

    # ── Public API ──────────────────────────────────────────────────────────

    async def fetch_today(self) -> dict:
        """
        Fetch NSE live + BSE today's trades and persist them.
        Returns a summary dict.
        """
        today = date.today()
        log.info("exchange_fetch_today", date=today.isoformat())

        nse_records, bse_records = await asyncio.gather(
            self._fetch_nse_live(),
            self._fetch_bse_trades(today, today),
            return_exceptions=True,
        )

        if isinstance(nse_records, Exception):
            log.warning("nse_gather_error", error=str(nse_records))
            nse_records = []
        if isinstance(bse_records, Exception):
            log.warning("bse_gather_error", error=str(bse_records))
            bse_records = []

        all_records = list(nse_records) + list(bse_records)
        stored = await self._store_trades(all_records)

        summary = {
            "fetch_date":    today.isoformat(),
            "nse_records":   len(nse_records),
            "bse_records":   len(bse_records),
            "total_fetched": len(all_records),
            "stored":        stored,
        }
        log.info("exchange_fetch_done", **summary)
        return summary

    async def fetch_historical(self, days: int = 5) -> dict:
        """
        Fetch NSE historical + BSE for the last `days` calendar days.
        Returns a summary dict.
        """
        today     = date.today()
        from_dt   = today - timedelta(days=days)
        log.info("exchange_fetch_historical",
                 from_date=from_dt.isoformat(), to_date=today.isoformat())

        nse_records, bse_records = await asyncio.gather(
            self._fetch_nse_historical(from_dt, today),
            self._fetch_bse_trades(from_dt, today),
            return_exceptions=True,
        )

        if isinstance(nse_records, Exception):
            log.warning("nse_hist_gather_error", error=str(nse_records))
            nse_records = []
        if isinstance(bse_records, Exception):
            log.warning("bse_hist_gather_error", error=str(bse_records))
            bse_records = []

        all_records = list(nse_records) + list(bse_records)
        stored = await self._store_trades(all_records)

        summary = {
            "from_date":     from_dt.isoformat(),
            "to_date":       today.isoformat(),
            "nse_records":   len(nse_records),
            "bse_records":   len(bse_records),
            "total_fetched": len(all_records),
            "stored":        stored,
        }
        log.info("exchange_hist_fetch_done", **summary)
        return summary

    async def get_trades(self, isin: str, limit: int = 30) -> list[dict]:
        """Return recent exchange trade records for an ISIN."""
        return await fetch_all(
            """SELECT * FROM exchange_trades
               WHERE isin = :isin
               ORDER BY trade_date DESC, exchange
               LIMIT :lim""",
            {"isin": isin, "lim": limit},
        )

    async def get_latest_price(self, isin: str) -> Optional[dict]:
        """Return the most recent exchange trade record for an ISIN."""
        return await fetch_one(
            """SELECT * FROM exchange_trades
               WHERE isin = :isin
               ORDER BY trade_date DESC, exchange
               LIMIT 1""",
            {"isin": isin},
        )


# ── Private helpers ────────────────────────────────────────────────────────────

def _f(val) -> Optional[float]:
    """Safely cast to float."""
    try:
        return float(val) if val not in (None, "", "−", "-") else None
    except (TypeError, ValueError):
        return None


def _i(val) -> Optional[int]:
    """Safely cast to int."""
    try:
        return int(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_date(raw: str) -> str:
    """
    Normalise exchange date strings to YYYY-MM-DD.
    NSE typically uses DD-MM-YYYY; BSE uses DD/MM/YYYY or YYYY-MM-DD.
    """
    raw = str(raw).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # fallback — return as-is
    return raw
