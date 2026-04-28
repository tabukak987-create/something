"""
Live Market Data Fetcher — Multi-Source
========================================
Fetches REAL bond market data from public sources in priority order:

  1. RBI FBIL G-Sec Benchmark Yields (public REST, no auth)
     https://www.fbil.org.in/#!/gsec-benchmarks  (data via API)
  2. NSE debt-market live bonds (cookie-based scrape, might block)
     https://www.nseindia.com/api/liveBonds-traded-on-exchange?type=corporate
  3. BSE corporate bond reports
     https://api.bseindia.com/BseIndiaAPI/api/CorporateBondsReports/w
  4. Synthetic fallback (curve + spread model — always works)

For G-Sec yield curve:
  RBI publishes daily at https://rbi.org.in/Scripts/BS_ViewBulletin.aspx
  We use the FBIL benchmark page API which is publicly accessible.

For prices:
  NSE/BSE rest APIs return corporate bond last trade prices.
  If blocked, we use the RBI yield curve to compute clean prices.

For IM (Information Memorandum):
  BSE corporate filings API works without auth for public bonds.
  NSE filing search also works with simple headers.

This file is the SINGLE service called by the router. It orchestrates all
sources, merges results, and writes to:
  - unified_market_data      (one row per isin per date)
  - exchange_trades          (raw tick per isin per exchange)
  - bond_im_registry         (IM filing metadata)
  - yield_curve_points       (G-Sec benchmark curve)
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from typing import Optional

import httpx

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso

log = get_logger("live_market_fetcher")

# ─── Timeouts & headers ────────────────────────────────────────────────────────
_TIMEOUT = 20

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "X-Requested-With": "XMLHttpRequest",
}

_BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json,text/html,*/*",
    "Referer": "https://www.bseindia.com/",
}

_FBIL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BondHQ/1.0)",
    "Accept": "application/json",
    "Origin": "https://www.fbil.org.in",
    "Referer": "https://www.fbil.org.in/#!/gsec-benchmarks",
}

# ─── Static G-Sec yield curve (hardcoded Feb 2026, always-available fallback) ─
_STATIC_GSEC_CURVE = {
    0.25: 6.82,  0.50: 6.87,  1.00: 6.95,
    2.00: 7.00,  3.00: 7.02,  5.00: 7.05,
    7.00: 7.08, 10.00: 7.10, 14.00: 7.15,
    20.00: 7.20, 30.00: 7.25, 40.00: 7.28,
}

# ─── Credit spread matrix (bps over G-Sec, Feb 2026 estimates) ────────────────
_SPREADS = {
    "SOV":      0,   "G-SEC":    0,
    "AAA":     45,   "AA+":     75,
    "AA":     110,   "AA-":    140,
    "A+":     180,   "A":      220,
    "A-":     270,   "BBB+":   340,
    "BBB":    420,   "BBB-":   500,
    "UNKNOWN": 200,
}


class LiveMarketFetcher:
    """Orchestrates live + synthetic bond market data refresh."""

    def __init__(self):
        self.today = date.today()
        self._gsec_curve: dict[float, float] = dict(_STATIC_GSEC_CURVE)

    # ══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════════════

    async def refresh_all(self, max_bonds: int = 2000) -> dict:
        """
        Master refresh:
        1. Pull FBIL/RBI G-Sec curve → update yield_curve_points + static dict
        2. Pull NSE live bond prices
        3. Pull BSE corporate bond prices
        4. For every bond in universal_bond_master, upsert unified_market_data
           using live prices where available, synthetic otherwise.
        Returns summary stats.
        """
        log.info("refresh_all_start", date=self.today.isoformat())

        # Step 1 — G-Sec curve
        curve_result = await self._refresh_gsec_curve()

        # Step 2/3 — Exchange prices (run in parallel, failures are silent)
        nse_prices, bse_prices, ccil_prices = await asyncio.gather(
            self._fetch_nse_live_prices(),
            self._fetch_bse_prices(self.today, self.today),
            self._fetch_ccil_nds_om(),
            return_exceptions=True,
        )
        if isinstance(nse_prices, Exception):
            log.warning("nse_fetch_failed", err=str(nse_prices))
            nse_prices = {}
        if isinstance(bse_prices, Exception):
            log.warning("bse_fetch_failed", err=str(bse_prices))
            bse_prices = {}
        if isinstance(ccil_prices, Exception):
            log.warning("ccil_fetch_failed", err=str(ccil_prices))
            ccil_prices = {}

        # Merge: CCIL → BSE → NSE (later sources overwrite = higher priority)
        live_prices: dict[str, dict] = {}
        for isin, p in ccil_prices.items():
            live_prices[isin] = {**p, "source": "CCIL"}
        for isin, p in bse_prices.items():
            live_prices[isin] = {**p, "source": "BSE"}
        for isin, p in nse_prices.items():
            live_prices[isin] = {**p, "source": "NSE"}

        # Step 4 — Upsert all bonds
        bonds = await fetch_all(
            """SELECT isin, issuer_type, credit_rating, coupon_rate,
                      maturity_date, face_value
               FROM universal_bond_master
               WHERE is_active = 1
               ORDER BY issuer_type, credit_rating
               LIMIT :lim""",
            {"lim": max_bonds},
        )
        updated_live = 0
        updated_synth = 0

        for bond in bonds:
            isin = bond["isin"]
            live = live_prices.get(isin)
            if live:
                md = self._build_market_data(bond, live_ytm=live.get("ytm"),
                                              live_price=live.get("price"),
                                              source=live["source"],
                                              volume=live.get("volume"))
                updated_live += 1
            else:
                md = self._build_market_data(bond)
                updated_synth += 1

            await self._upsert_market_data(isin, md)

        # Step 5 — Store raw exchange trade ticks for non-G-Sec live prices
        exchange_stored = await self._store_exchange_trades(nse_prices, bse_prices)

        result = {
            "status":           "ok",
            "date":             self.today.isoformat(),
            "bonds_total":      len(bonds),
            "live_priced":      updated_live,
            "synthetic_priced": updated_synth,
            "exchange_stored":  exchange_stored,
            "gsec_curve":       curve_result,
            "nse_live_count":   len(nse_prices),
            "bse_live_count":   len(bse_prices),
            "ccil_live_count":  len(ccil_prices),
        }
        log.info("refresh_all_done", **result)
        return result

    async def refresh_one(self, isin: str) -> dict:
        """Refresh a single bond. Returns the market data dict stored."""
        bond = await fetch_one(
            """SELECT isin, issuer_type, credit_rating, coupon_rate,
                      maturity_date, face_value
               FROM universal_bond_master WHERE isin = :isin""",
            {"isin": isin},
        )
        if not bond:
            return {"error": f"ISIN {isin} not found in bond master"}

        # Try NSE live first
        nse = await self._fetch_nse_price_for_isin(isin)
        md = self._build_market_data(
            bond,
            live_ytm=nse.get("ytm") if nse else None,
            live_price=nse.get("price") if nse else None,
            source="NSE" if nse else "SYNTHETIC",
        )
        await self._upsert_market_data(isin, md)
        return {"isin": isin, "source": md["data_source"], "ytm": md["ytm"],
                "clean_price": md["clean_price"]}

    # ══════════════════════════════════════════════════════════════════════════
    # G-SEC CURVE FETCHERS
    # ══════════════════════════════════════════════════════════════════════════

    async def _refresh_gsec_curve(self) -> dict:
        """
        Fetch FBIL G-Sec benchmark yields and store in yield_curve_points.
        Falls back to static curve silently.
        """
        result = {"source": "STATIC", "points": len(self._gsec_curve)}
        try:
            live = await self._fetch_fbil_gsec()
            if live:
                self._gsec_curve.update(live)
                result = {"source": "FBIL_LIVE", "points": len(live)}
                await self._store_gsec_curve(live, "FBIL")
            else:
                # Try RBI bulletin page
                rbi = await self._fetch_rbi_gsec()
                if rbi:
                    self._gsec_curve.update(rbi)
                    result = {"source": "RBI_LIVE", "points": len(rbi)}
                    await self._store_gsec_curve(rbi, "RBI")
                else:
                    await self._store_gsec_curve(self._gsec_curve, "STATIC")
        except Exception as e:
            log.warning("gsec_curve_failed", err=str(e))
            await self._store_gsec_curve(self._gsec_curve, "STATIC")
        return result

    async def _fetch_fbil_gsec(self) -> dict[float, float]:
        """
        Fetch G-Sec benchmark yields from FBIL.
        FBIL provides benchmark rates at https://fbil.org.in
        Returns {tenor_years: yield_pct} or empty dict on failure.
        """
        try:
            async with httpx.AsyncClient(headers=_FBIL_HEADERS,
                                          follow_redirects=True,
                                          timeout=_TIMEOUT) as client:
                # FBIL JSON endpoint for G-Sec rates
                resp = await client.get(
                    "https://fbil.org.in/api/gsec-benchmark/latest",
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return self._parse_fbil_response(data)
        except Exception as e:
            log.debug("fbil_fetch_error", err=str(e))
        return {}

    async def _fetch_rbi_gsec(self) -> dict[float, float]:
        """
        Fetch G-Sec yields from RBI's public data warehouse.
        RBI DBIEspApi endpoint: https://dbie.rbi.org.in
        This is free, public, no auth needed.
        Returns {tenor_years: yield_pct} or empty dict.
        """
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                # RBI DBIE API - government securities yield
                resp = await client.get(
                    "https://dbie.rbi.org.in/DBIE/dbie.rbi?site=publications&body=yield",
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    # Try to parse whatever is returned
                    try:
                        data = resp.json()
                        return self._parse_rbi_response(data)
                    except Exception:
                        pass
        except Exception as e:
            log.debug("rbi_fetch_error", err=str(e))
        return {}

    def _parse_fbil_response(self, data: dict | list) -> dict[float, float]:
        """Parse FBIL response into {tenor: yield} dict."""
        result = {}
        # Handle various FBIL response shapes
        items = (
            data.get("data")
            or data.get("rates")
            or data.get("benchmarks")
            or (data if isinstance(data, list) else [])
        )
        tenor_map = {
            "1M": 0.083, "3M": 0.25, "6M": 0.5, "1Y": 1, "2Y": 2,
            "3Y": 3, "5Y": 5, "7Y": 7, "10Y": 10, "14Y": 14,
            "20Y": 20, "30Y": 30, "40Y": 40,
        }
        for item in (items if isinstance(items, list) else []):
            tenor_str = str(item.get("tenor") or item.get("maturity") or "").upper()
            rate_val  = item.get("yield") or item.get("rate") or item.get("value")
            if tenor_str in tenor_map and rate_val:
                try:
                    result[tenor_map[tenor_str]] = float(rate_val)
                except (TypeError, ValueError):
                    pass
        return result

    def _parse_rbi_response(self, data: dict | list) -> dict[float, float]:
        """Parse RBI response into {tenor: yield} dict."""
        result = {}
        items = []
        if isinstance(data, dict):
            items = data.get("data") or data.get("yields") or data.get("records") or []
        elif isinstance(data, list):
            items = data

        tenor_patterns = {
            r"91\s*d": 0.25, r"182\s*d": 0.5, r"364\s*d": 1.0, r"1\s*y": 1.0,
            r"2\s*y": 2.0, r"3\s*y": 3.0, r"5\s*y": 5.0, r"7\s*y": 7.0,
            r"10\s*y": 10.0, r"14\s*y": 14.0, r"20\s*y": 20.0, r"30\s*y": 30.0,
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            tenor_str = str(item.get("tenor") or item.get("maturity") or
                           item.get("description") or "").lower()
            rate = item.get("yield") or item.get("rate") or item.get("value")
            if not rate:
                continue
            for pat, years in tenor_patterns.items():
                if re.search(pat, tenor_str, re.IGNORECASE):
                    try:
                        result[years] = float(rate)
                    except (TypeError, ValueError):
                        pass
                    break
        return result

    async def _store_gsec_curve(self, curve: dict[float, float], source: str) -> None:
        """Persist G-Sec curve to yield_curve_points table."""
        today_str = self.today.isoformat()
        for tenor, yld in curve.items():
            try:
                await execute_query(
                    """INSERT INTO yield_curve_points (curve_date, curve_type, tenor_years, yield_pct, source)
                       VALUES (:dt, 'GSEC', :t, :y, :src)
                       ON CONFLICT(curve_date, curve_type, tenor_years) DO UPDATE SET
                         yield_pct = excluded.yield_pct, source = excluded.source""",
                    {"dt": today_str, "t": tenor, "y": yld, "src": source},
                )
            except Exception:
                pass  # table might not exist yet — skip silently

    # ══════════════════════════════════════════════════════════════════════════
    # NSE LIVE PRICES
    # ══════════════════════════════════════════════════════════════════════════

    async def _fetch_nse_live_prices(self) -> dict[str, dict]:
        """
        Fetch live corporate bond prices from NSE.
        Returns {isin: {ytm, price, volume, change, source}} or {}.
        """
        try:
            async with httpx.AsyncClient(
                headers=_NSE_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                # Warm up session (required by NSE)
                try:
                    await client.get("https://www.nseindia.com/", timeout=8.0)
                except Exception:
                    pass

                # Live corporate bonds traded on exchange
                resp = await client.get(
                    "https://www.nseindia.com/api/liveBonds-traded-on-exchange?type=corporate",
                    timeout=15.0,
                )
                if resp.status_code != 200:
                    log.warning("nse_live_status", code=resp.status_code)
                    return {}

                data = resp.json()
                return self._parse_nse_live(data)
        except Exception as e:
            log.warning("nse_live_error", err=str(e))
            return {}

    async def _fetch_nse_price_for_isin(self, isin: str) -> Optional[dict]:
        """Fetch NSE price for a specific ISIN."""
        try:
            async with httpx.AsyncClient(headers=_NSE_HEADERS,
                                          follow_redirects=True, timeout=_TIMEOUT) as client:
                await client.get("https://www.nseindia.com/", timeout=5.0)
                resp = await client.get(
                    f"https://www.nseindia.com/api/quote-bonds?symbol={isin}",
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    bond_data = data.get("bondDetails") or data
                    price = _f(bond_data.get("lastPrice") or bond_data.get("ltp"))
                    ytm   = _f(bond_data.get("yield") or bond_data.get("ytm"))
                    if price or ytm:
                        return {"ytm": ytm, "price": price}
        except Exception:
            pass
        return None

    def _parse_nse_live(self, data: dict) -> dict[str, dict]:
        """Parse NSE live bonds response into {isin: {ytm, price, volume}} dict."""
        result: dict[str, dict] = {}
        items = data.get("data") or []
        for item in items:
            isin = item.get("isin") or item.get("ISIN") or ""
            if not isin:
                continue
            ytm   = _f(item.get("yield") or item.get("ytm") or item.get("lastYield"))
            price = _f(item.get("lastPrice") or item.get("ltp") or item.get("closedPrice"))
            vol   = _f(item.get("totalTradedQuantity") or item.get("ttq"))
            if price or ytm:
                result[isin] = {
                    "ytm": ytm, "price": price, "volume": vol,
                    "open": _f(item.get("open")),
                    "high": _f(item.get("dayHigh") or item.get("high")),
                    "low":  _f(item.get("dayLow")  or item.get("low")),
                    "prev_close": _f(item.get("previousClose")),
                    "trades": _i(item.get("numberOfTrades") or item.get("totalTrades")),
                    "bond_name": item.get("symbol") or item.get("description"),
                    "exchange": "NSE",
                }
        log.info("nse_live_parsed", count=len(result))
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # BSE LIVE PRICES
    # ══════════════════════════════════════════════════════════════════════════

    async def _fetch_bse_prices(self, from_dt: date, to_dt: date) -> dict[str, dict]:
        """Fetch BSE corporate bond trades. Returns {isin: {ytm, price, volume}}."""
        url = (
            "https://api.bseindia.com/BseIndiaAPI/api/CorporateBondsReports/w"
            f"?Type=EQ&Dtfrom={from_dt.strftime('%d%%2F%m%%2F%Y')}"
            f"&DtTo={to_dt.strftime('%d%%2F%m%%2F%Y')}"
        )
        try:
            async with httpx.AsyncClient(
                headers=_BSE_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(url, timeout=15.0)
                if resp.status_code != 200:
                    log.warning("bse_status", code=resp.status_code)
                    return {}
                data = resp.json()
                return self._parse_bse_response(data)
        except Exception as e:
            log.warning("bse_error", err=str(e))
            return {}

    def _parse_bse_response(self, data: dict) -> dict[str, dict]:
        """Parse BSE response into {isin: {ytm, price, volume}} dict."""
        result: dict[str, dict] = {}
        items = data.get("Table") or data.get("data") or []
        for item in items:
            isin = (item.get("ISIN") or item.get("Isin") or item.get("isin") or "").strip()
            if not isin:
                continue
            price = _f(item.get("LTP") or item.get("ClosePrice") or item.get("CLOSE"))
            ytm   = _f(item.get("YTM") or item.get("Yield"))
            vol   = _f(item.get("TotalVolume") or item.get("Quantity"))
            if price or ytm:
                result[isin] = {
                    "ytm": ytm, "price": price, "volume": vol,
                    "open":       _f(item.get("OpenPrice") or item.get("OPEN")),
                    "high":       _f(item.get("HighPrice") or item.get("HIGH")),
                    "low":        _f(item.get("LowPrice")  or item.get("LOW")),
                    "prev_close": _f(item.get("PrevClosePrice")),
                    "trades":     _i(item.get("NoOfTrades")),
                    "bond_name":  item.get("SecurityName") or item.get("COMPANY"),
                    "exchange": "BSE",
                }
        log.info("bse_parsed", count=len(result))
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # CCIL NDS-OM (Government Securities Trading Data)
    # ══════════════════════════════════════════════════════════════════════════

    async def _fetch_ccil_nds_om(self) -> dict[str, dict]:
        """
        Fetch G-Sec / SDL trade data from CCIL (Clearing Corporation of India).
        Free, public data — no auth required.
        Returns {isin: {ytm, price, volume}} or {}.
        """
        today_str = self.today.strftime("%d%m%Y")
        urls = [
            f"https://www.ccilindia.com/web/ccil/nds-om-market-data?date={today_str}",
            "https://www.ccilindia.com/web/ccil/nds-om-market-data",
        ]
        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json, text/html, text/csv, */*",
        }

        for url in urls:
            try:
                async with httpx.AsyncClient(
                    headers=_headers, follow_redirects=True, timeout=15
                ) as client:
                    resp = await client.get(url, timeout=12)
                    if resp.status_code != 200:
                        continue

                    body = resp.text.strip()
                    ct = resp.headers.get("content-type", "")

                    # JSON response
                    if "json" in ct or body.startswith(("{", "[")):
                        try:
                            data = resp.json()
                            items = (data.get("data") or data.get("records") or
                                     data.get("trades") or
                                     (data if isinstance(data, list) else []))
                            result: dict[str, dict] = {}
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                isin = str(item.get("isin") or item.get("ISIN") or
                                           item.get("securityId") or "").strip()
                                if not isin or len(isin) != 12:
                                    continue
                                price = _f(item.get("weightedAvgPrice") or
                                           item.get("lastTradePrice") or
                                           item.get("closePrice"))
                                ytm = _f(item.get("weightedAvgYield") or
                                         item.get("lastTradeYield") or
                                         item.get("yield"))
                                vol = _f(item.get("totalVolume") or
                                         item.get("tradedQuantity"))
                                if price or ytm:
                                    result[isin] = {
                                        "ytm": ytm, "price": price, "volume": vol,
                                        "exchange": "CCIL",
                                    }
                            if result:
                                log.info("ccil_nds_om_fetched", count=len(result))
                                return result
                        except Exception:
                            pass

                    # CSV fallback
                    if "ISIN" in body.upper() and "," in body:
                        import csv, io
                        result = {}
                        try:
                            reader = csv.DictReader(io.StringIO(body))
                            for row in reader:
                                row = {k.strip().upper(): v.strip()
                                       for k, v in row.items() if k}
                                isin = row.get("ISIN", row.get("SECURITY_ID", ""))
                                if not isin or len(isin) != 12:
                                    continue
                                price = _f(row.get("WTD_AVG_PRICE") or
                                           row.get("CLOSE_PRICE") or
                                           row.get("PRICE"))
                                ytm = _f(row.get("WTD_AVG_YIELD") or
                                         row.get("YIELD") or row.get("YTM"))
                                vol = _f(row.get("TOTAL_VOLUME") or row.get("VOLUME"))
                                if price or ytm:
                                    result[isin] = {
                                        "ytm": ytm, "price": price, "volume": vol,
                                        "exchange": "CCIL",
                                    }
                        except Exception:
                            pass
                        if result:
                            log.info("ccil_csv_parsed", count=len(result))
                            return result

            except Exception as exc:
                log.debug("ccil_url_failed", url=url[:60], err=str(exc)[:60])
                continue

        log.debug("ccil_nds_om_no_data")
        return {}

    # ══════════════════════════════════════════════════════════════════════════
    # MARKET DATA BUILDER (synthetic fallback)
    # ══════════════════════════════════════════════════════════════════════════

    def _build_market_data(
        self,
        bond: dict,
        live_ytm: Optional[float] = None,
        live_price: Optional[float] = None,
        source: str = "SYNTHETIC",
        volume: Optional[float] = None,
    ) -> dict:
        """Build a unified_market_data row for a bond."""
        maturity_str = str(bond.get("maturity_date") or "")
        coupon       = float(bond.get("coupon_rate") or 0)
        face         = float(bond.get("face_value") or 100)
        rating       = (bond.get("credit_rating") or "UNKNOWN").replace(
            "CRISIL ", "").replace("ICRA ", "").replace("CARE ", "").strip()
        issuer_type  = (bond.get("issuer_type") or "CORPORATE").upper()

        # Tenor
        tenor = 5.0
        try:
            mat = date.fromisoformat(maturity_str[:10])
            tenor = max((mat - self.today).days / 365.25, 0.1)
        except Exception:
            pass

        # Yield
        if live_ytm and 2.0 <= live_ytm <= 25.0:
            ytm = round(live_ytm, 4)
        else:
            ytm = self._synthetic_ytm(tenor, rating, issuer_type)

        # Price
        if live_price and 10.0 <= live_price <= face * 3:
            clean_price = round(live_price, 4)
        else:
            clean_price = self._ytm_to_price(coupon, ytm, tenor, face)

        # Risk metrics
        mod_dur  = round(min(tenor * 0.92, 15.0), 4)
        convex   = round(mod_dur ** 2 / 100 + 0.5, 4)
        dv01     = round(clean_price * mod_dur / 10000, 6)
        gsec_y   = self._interp_gsec(tenor)
        spread   = round((ytm - gsec_y) * 100, 1)

        return {
            "price_date":         self.today.isoformat(),
            "ytm":                ytm,
            "clean_price":        clean_price,
            "dirty_price":        round(clean_price + face * coupon / 200, 4),
            "modified_duration":  mod_dur,
            "macaulay_duration":  round(mod_dur * (1 + ytm / 100), 4),
            "convexity":          convex,
            "dv01":               dv01,
            "spread_over_gsec":   spread,
            "z_spread":           round(spread + 2, 1),
            "traded_volume":      volume,
            "data_source":        source,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # DB WRITES
    # ══════════════════════════════════════════════════════════════════════════

    async def _upsert_market_data(self, isin: str, md: dict) -> None:
        """Upsert a row into unified_market_data with anomaly validation gate."""
        # ── P2B: Enforce anomaly rejection before DB write ──
        price = md.get("clean_price")
        source = md.get("data_source", "UNKNOWN")
        if price is not None and price > 0:
            try:
                from app.domain.core_trust.data_integrity import DataIntegrityService
                validation = await DataIntegrityService.validate_price(isin, price, source)
                if validation.get("reject"):
                    log.warning(
                        "price_rejected_anomaly",
                        isin=isin, price=price, source=source,
                        anomalies=validation.get("anomalies", []),
                    )
                    return  # CRITICAL: Do NOT write anomalous price to DB
                if validation.get("anomalies"):
                    log.info(
                        "price_accepted_with_warnings",
                        isin=isin, price=price, source=source,
                        warnings=[a["message"] for a in validation["anomalies"]],
                    )
            except Exception as exc:
                log.debug("anomaly_check_skipped", isin=isin, error=str(exc)[:80])

        try:
            await execute_query(
                """INSERT INTO unified_market_data
                    (isin, price_date, clean_price, dirty_price, ytm,
                     modified_duration, macaulay_duration, convexity, dv01,
                                                 spread_over_gsec, z_spread, traded_volume, data_source,
                                                 source_name, source_type, ingestion_method, source_reference,
                                                 timestamp_ingested, timestamp_effective, provenance_json,
                                                 last_updated)
                   VALUES
                    (:isin, :price_date, :clean_price, :dirty_price, :ytm,
                     :modified_duration, :macaulay_duration, :convexity, :dv01,
                                                 :spread_over_gsec, :z_spread, :traded_volume, :data_source,
                                                 :source_name, :source_type, :ingestion_method, :source_reference,
                                                 :timestamp_ingested, :timestamp_effective, :provenance_json,
                     datetime('now'))
                   ON CONFLICT(isin, price_date) DO UPDATE SET
                     clean_price       = excluded.clean_price,
                     dirty_price       = excluded.dirty_price,
                     ytm               = excluded.ytm,
                     modified_duration = excluded.modified_duration,
                     macaulay_duration = excluded.macaulay_duration,
                     convexity         = excluded.convexity,
                     dv01              = excluded.dv01,
                     spread_over_gsec  = excluded.spread_over_gsec,
                     z_spread          = excluded.z_spread,
                     traded_volume     = COALESCE(excluded.traded_volume, unified_market_data.traded_volume),
                     data_source       = excluded.data_source,
                     source_name       = excluded.source_name,
                     source_type       = excluded.source_type,
                     ingestion_method  = excluded.ingestion_method,
                     source_reference  = excluded.source_reference,
                     timestamp_ingested = excluded.timestamp_ingested,
                     timestamp_effective = excluded.timestamp_effective,
                     provenance_json   = excluded.provenance_json,
                     last_updated      = datetime('now')
                   WHERE excluded.data_source IN ('NSE','BSE')
                      OR unified_market_data.data_source = 'SYNTHETIC'
                      OR unified_market_data.price_date != excluded.price_date""",
                {
                    "isin": isin,
                    **md,
                    "source_name": md.get("source_name") or md.get("data_source", "PUBLIC"),
                    "source_type": md.get("source_type") or "public",
                    "ingestion_method": md.get("ingestion_method") or "api",
                    "source_reference": md.get("source_reference") or md.get("data_source", "PUBLIC"),
                    "timestamp_ingested": md.get("timestamp_ingested") or utcnow_iso(),
                    "timestamp_effective": md.get("timestamp_effective") or md.get("price_date") or date.today().isoformat(),
                    "provenance_json": json.dumps({
                        "source_name": md.get("source_name") or md.get("data_source", "PUBLIC"),
                        "source_type": md.get("source_type") or "public",
                        "ingestion_method": md.get("ingestion_method") or "api",
                        "source_reference": md.get("source_reference") or md.get("data_source", "PUBLIC"),
                        "timestamp_ingested": md.get("timestamp_ingested") or utcnow_iso(),
                        "timestamp_effective": md.get("timestamp_effective") or md.get("price_date") or date.today().isoformat(),
                    }, default=str),
                },
            )
        except Exception as e:
            log.debug("upsert_market_data_error", isin=isin, err=str(e)[:80])

    async def _store_exchange_trades(
        self,
        nse: dict[str, dict],
        bse: dict[str, dict],
    ) -> int:
        """Store raw tick data in exchange_trades table."""
        stored = 0
        today_str = self.today.isoformat()
        for exchange, prices in [("NSE", nse), ("BSE", bse)]:
            for isin, p in prices.items():
                try:
                    await execute_query(
                        """INSERT INTO exchange_trades
                            (isin, trade_date, exchange, bond_name,
                             last_price, open_price, high_price, low_price,
                             prev_close, total_volume, number_of_trades, ytm)
                           VALUES
                            (:isin, :td, :ex, :bn,
                             :lp, :op, :hp, :lop,
                             :pc, :tv, :nt, :ytm)
                           ON CONFLICT(isin, trade_date, exchange) DO UPDATE SET
                             last_price = excluded.last_price,
                             ytm        = COALESCE(excluded.ytm, exchange_trades.ytm),
                             total_volume = excluded.total_volume,
                             fetched_at = datetime('now')""",
                        {
                            "isin": isin,      "td": today_str,    "ex": exchange,
                            "bn":  p.get("bond_name"),
                            "lp":  p.get("price"),    "op": p.get("open"),
                            "hp":  p.get("high"),     "lop": p.get("low"),
                            "pc":  p.get("prev_close"), "tv": p.get("volume"),
                            "nt":  p.get("trades"),   "ytm": p.get("ytm"),
                        },
                    )
                    stored += 1
                except Exception as e:
                    log.debug("exchange_trade_store_err", isin=isin, err=str(e)[:60])
        return stored

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    def _interp_gsec(self, tenor: float) -> float:
        tenors = sorted(self._gsec_curve)
        if tenor <= tenors[0]:  return self._gsec_curve[tenors[0]]
        if tenor >= tenors[-1]: return self._gsec_curve[tenors[-1]]
        for i in range(len(tenors) - 1):
            t1, t2 = tenors[i], tenors[i + 1]
            if t1 <= tenor <= t2:
                w = (tenor - t1) / (t2 - t1)
                return self._gsec_curve[t1] + w * (self._gsec_curve[t2] - self._gsec_curve[t1])
        return 7.10

    def _synthetic_ytm(self, tenor: float, rating: str, issuer_type: str) -> float:
        base   = self._interp_gsec(tenor)
        spread = _SPREADS.get(rating, _SPREADS.get(rating.rstrip("+-"), 200))
        if spread is None:
            spread = 200
        if issuer_type == "SDL":
            spread = max(spread, 15)
        return round(max(base + spread / 100, 3.0), 4)

    @staticmethod
    def _ytm_to_price(coupon: float, ytm: float, tenor: float, face: float = 100.0) -> float:
        if ytm <= 0 or tenor <= 0:
            return face
        if coupon <= 0:
            return round(face / ((1 + ytm / 100) ** tenor), 4)
        r = ytm / 200
        n = max(1, int(tenor * 2))
        c = coupon * face / 200
        if r == 0:
            return c * n + face
        pv_c = c * (1 - (1 + r) ** -n) / r
        pv_f = face / (1 + r) ** n
        return round(pv_c + pv_f, 4)


# ── IM FETCHER ────────────────────────────────────────────────────────────────

class IMDataService:
    """
    Fetches and stores Information Memorandum (IM) records.
    Sources: NSE filings API, BSE MemDisclose API, synthetic seed for testing.
    """

    async def fetch_and_store(self, days_back: int = 30) -> dict:
        """Fetch IMs from NSE + BSE. Fall back to synthetic seed if both fail."""
        from_dt  = date.today() - timedelta(days=days_back)
        to_dt    = date.today()

        nse_recs, bse_recs = await asyncio.gather(
            self._fetch_nse_im(from_dt, to_dt),
            self._fetch_bse_im(from_dt),
            return_exceptions=True,
        )
        if isinstance(nse_recs, Exception):
            log.warning("nse_im_error", err=str(nse_recs))
            nse_recs = []
        if isinstance(bse_recs, Exception):
            log.warning("bse_im_error", err=str(bse_recs))
            bse_recs = []

        all_recs: list[dict] = list(nse_recs) + list(bse_recs)

        # If we got nothing from exchanges, seed IM records from bond master
        if not all_recs:
            log.info("im_seed_from_master")
            all_recs = await self._synthetic_im_seed()

        stored = await self._store_im_records(all_recs)
        log.stored = stored
        await self._log_fetch(len(nse_recs), len(bse_recs), stored)

        return {
            "nse_found":     len(nse_recs) if isinstance(nse_recs, list) else 0,
            "bse_found":     len(bse_recs) if isinstance(bse_recs, list) else 0,
            "synthetic_seed": len(all_recs) if not nse_recs and not bse_recs else 0,
            "stored":        stored,
        }

    async def fetch_for_isin(self, isin: str) -> dict:
        """Fetch IM for a specific ISIN from BSE (most reliable for single ISIN)."""
        url = (
            "https://api.bseindia.com/BseIndiaAPI/api/MemDisclose/w"
            f"?strtype=IM&strscrip={isin}&strisno=&strdt="
        )
        records = []
        try:
            async with httpx.AsyncClient(headers=_BSE_HEADERS,
                                          follow_redirects=True, timeout=20) as client:
                resp = await client.get(url, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("Table") or data.get("data") or []
                    for item in items:
                        records.append(self._bse_item_to_record(isin, item))
        except Exception as e:
            log.warning("im_isin_fetch_error", isin=isin, err=str(e))

        stored = await self._store_im_records(records)
        return {"isin": isin, "found": len(records), "stored": stored}

    async def get_all(self, isin: str) -> list[dict]:
        return await fetch_all(
            "SELECT * FROM bond_im_registry WHERE isin = :isin ORDER BY filing_date DESC",
            {"isin": isin},
        )

    async def get_latest(self, isin: str) -> Optional[dict]:
        return await fetch_one(
            "SELECT * FROM bond_im_registry WHERE isin = :isin ORDER BY filing_date DESC LIMIT 1",
            {"isin": isin},
        )

    async def get_bonds_without_im(self) -> list[dict]:
        """Portfolio bonds with no IM filed (non-sovereign only)."""
        return await fetch_all(
            """SELECT b.isin, b.bond_name, b.issuer_name, b.credit_rating, b.issuer_type
               FROM universal_bond_master b
               WHERE b.issuer_type NOT IN ('G-SEC', 'T-BILL', 'SDL')
                 AND b.is_active = 1
                 AND NOT EXISTS (
                     SELECT 1 FROM bond_im_registry r WHERE r.isin = b.isin
                 )
               LIMIT 200""",
        )

    async def verify_terms(self, isin: str) -> dict:
        bond = await fetch_one(
            "SELECT * FROM universal_bond_master WHERE isin = :isin", {"isin": isin}
        )
        if not bond:
            return {"bond_found": False, "isin": isin}
        im = await self.get_latest(isin)
        if not im:
            return {"bond_found": True, "isin": isin, "im_found": False}

        coupon_match  = (abs((im.get("coupon_rate") or 0) -
                             (bond.get("coupon_rate") or 0)) < 0.05)
        mat_match     = str(im.get("maturity_date", ""))[:10] == str(bond.get("maturity_date", ""))[:10]
        return {
            "bond_found":   True,
            "isin":         isin,
            "im_found":     True,
            "source":       im.get("source"),
            "coupon_match": coupon_match,
            "maturity_match": mat_match,
            "im_coupon":    im.get("coupon_rate"),
            "master_coupon": bond.get("coupon_rate"),
            "im_maturity":  im.get("maturity_date"),
            "master_maturity": bond.get("maturity_date"),
            "filing_date":  im.get("filing_date"),
        }

    # ── NSE ──────────────────────────────────────────────────────────────────

    async def _fetch_nse_im(self, from_dt: date, to_dt: date) -> list[dict]:
        url = (
            "https://www.nseindia.com/api/corporateFilings-offer"
            "?industry=&symbol=&documentType=Information+Memorandum"
            f"&fromDate={from_dt.strftime('%d-%m-%Y')}"
            f"&toDate={to_dt.strftime('%d-%m-%Y')}&limit=200"
        )
        try:
            async with httpx.AsyncClient(
                headers=_NSE_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                await client.get("https://www.nseindia.com/", timeout=8.0)
                resp = await client.get(url, timeout=15.0)
                if resp.status_code != 200:
                    return []
                data = resp.json()
                items = data.get("data") or (data if isinstance(data, list) else [])
                return [self._nse_item_to_record(item) for item in items if item.get("isin")]
        except Exception as e:
            log.warning("nse_im_error", err=str(e))
            return []

    def _nse_item_to_record(self, item: dict) -> dict:
        isin = (item.get("isin") or item.get("ISIN") or "").strip()
        doc_path = item.get("attachmentFile") or item.get("fileLink") or ""
        doc_url  = ("https://www.nseindia.com" + doc_path
                    if doc_path.startswith("/") else doc_path)
        return {
            "isin":          isin,
            "issuer_name":   item.get("companyName") or item.get("issuerName"),
            "source":        "NSE",
            "document_type": "IM",
            "document_url":  doc_url or None,
            "symbol":        item.get("symbol"),
            "series":        item.get("series") or item.get("debtSeriesNo"),
            "filing_date":   _parse_date(item.get("filingDate") or item.get("date")),
            "issue_size_cr": _f(item.get("issueSize") or item.get("issueSizeCr")),
            "coupon_rate":   _f(item.get("couponRate") or item.get("interestRate")),
            "maturity_date": _parse_date(item.get("maturityDate") or item.get("redemptionDate")),
            "credit_rating": item.get("creditRating") or item.get("rating"),
            "rating_agency": item.get("ratingAgency"),
            "secured":       1 if "secure" in str(item.get("securityType", "")).lower() else 0,
        }

    # ── BSE ──────────────────────────────────────────────────────────────────

    async def _fetch_bse_im(self, from_dt: date) -> list[dict]:
        url = (
            "https://api.bseindia.com/BseIndiaAPI/api/MemDisclose/w"
            f"?strtype=IM&strscrip=&strisno=&strdt={from_dt.strftime('%d%%2F%m%%2F%Y')}"
        )
        try:
            async with httpx.AsyncClient(
                headers=_BSE_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(url, timeout=15.0)
                if resp.status_code != 200:
                    return []
                data = resp.json()
                items = data.get("Table") or data.get("data") or []
                return [
                    self._bse_item_to_record(
                        (item.get("ISIN") or item.get("Isin") or "").strip(), item
                    )
                    for item in items
                    if item.get("ISIN") or item.get("Isin")
                ]
        except Exception as e:
            log.warning("bse_im_error", err=str(e))
            return []

    def _bse_item_to_record(self, isin: str, item: dict) -> dict:
        return {
            "isin":          isin,
            "issuer_name":   item.get("CompanyName") or item.get("COMPANY"),
            "source":        "BSE",
            "document_type": "IM",
            "document_url":  item.get("AttchFilePath") or item.get("URL"),
            "symbol":        item.get("SCRIPCD"),
            "series":        item.get("Series") or item.get("SERIES"),
            "filing_date":   _parse_date(item.get("FilingDate") or item.get("DissemDT")),
            "issue_size_cr": _f(item.get("IssueSize")),
            "coupon_rate":   _f(item.get("CouponRate") or item.get("COUPON_RATE")),
            "maturity_date": _parse_date(item.get("MaturityDate") or item.get("MATURITY_DATE")),
            "credit_rating": item.get("Rating") or item.get("CREDIT_RATING"),
            "rating_agency": item.get("RatingAgency"),
            "secured":       1 if "secure" in str(item.get("SecurityType", "")).lower() else 0,
        }

    # ── Synthetic seed ────────────────────────────────────────────────────────

    async def _synthetic_im_seed(self) -> list[dict]:
        """
        Build IM records from bond master for all non-G-Sec bonds.
        This is the testing/deployment fallback when exchanges block scraping.
        Each record is marked source='MASTER_SEED' so the UI can differentiate.
        """
        bonds = await fetch_all(
            """SELECT isin, bond_name, issuer_name, coupon_rate,
                      maturity_date, credit_rating, issue_date, face_value
               FROM universal_bond_master
               WHERE issuer_type NOT IN ('G-SEC', 'T-BILL', 'SDL')
                 AND is_active = 1
               LIMIT 5000""",
        )
        today = date.today().isoformat()
        records = []
        for b in bonds:
            # Build a synthetic IM record derived from master data
            issue_dt = str(b.get("issue_date") or today)[:10]
            records.append({
                "isin":          b["isin"],
                "issuer_name":   b.get("issuer_name") or b.get("bond_name"),
                "source":        "MASTER_SEED",
                "document_type": "IM",
                "document_url":  None,
                "symbol":        b["isin"],
                "series":        None,
                "filing_date":   issue_dt,
                "issue_size_cr": float(b.get("face_value") or 100) * 1000 / 10_000_000,
                "coupon_rate":   b.get("coupon_rate"),
                "maturity_date": b.get("maturity_date"),
                "credit_rating": b.get("credit_rating"),
                "rating_agency": "CRISIL",
                "secured":       1,
            })
        return records

    # ── DB write ──────────────────────────────────────────────────────────────

    async def _store_im_records(self, records: list[dict]) -> int:
        stored = 0
        for rec in records:
            if not rec.get("isin"):
                continue
            try:
                await execute_query(
                    """INSERT INTO bond_im_registry
                        (isin, issuer_name, source, document_type, document_url,
                         symbol, series, filing_date, issue_size_cr, coupon_rate,
                         maturity_date, credit_rating, rating_agency, secured)
                       VALUES
                        (:isin, :issuer_name, :source, :document_type, :document_url,
                         :symbol, :series, :filing_date, :issue_size_cr, :coupon_rate,
                         :maturity_date, :credit_rating, :rating_agency, :secured)
                       ON CONFLICT(isin, source, document_type) DO UPDATE SET
                         filing_date   = COALESCE(excluded.filing_date, bond_im_registry.filing_date),
                         document_url  = COALESCE(excluded.document_url, bond_im_registry.document_url),
                         coupon_rate   = COALESCE(excluded.coupon_rate, bond_im_registry.coupon_rate),
                         maturity_date = COALESCE(excluded.maturity_date, bond_im_registry.maturity_date),
                         credit_rating = COALESCE(excluded.credit_rating, bond_im_registry.credit_rating),
                         registered_at = datetime('now')""",
                    {k: rec.get(k) for k in (
                        "isin", "issuer_name", "source", "document_type", "document_url",
                        "symbol", "series", "filing_date", "issue_size_cr", "coupon_rate",
                        "maturity_date", "credit_rating", "rating_agency", "secured",
                    )},
                )
                stored += 1
            except Exception as e:
                log.debug("im_store_err", isin=rec.get("isin"), err=str(e)[:80])
        return stored

    async def _log_fetch(self, nse_count: int, bse_count: int, stored: int) -> None:
        try:
            await execute_query(
                """INSERT INTO im_fetch_log (fetched_at, source, records_found, records_stored)
                   VALUES (datetime('now'), 'NSE+BSE', :found, :stored)""",
                {"found": nse_count + bse_count, "stored": stored},
            )
        except Exception:
            pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _f(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "", "−", "-") else None
    except (TypeError, ValueError):
        return None


def _i(val) -> Optional[int]:
    try:
        return int(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_date(raw) -> Optional[str]:
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y",
                "%d %b %Y", "%Y%m%d"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
