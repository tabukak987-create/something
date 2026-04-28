"""
Enhanced Market Data Fetcher
============================
Priority fetch chain (highest priority last — later sources overwrite):
  1. Synthetic Fallback  (benchmark curve + rating spread model — always works)
  2. FIMMDA Valuation    (end-of-day mark-to-market for all bonds)
  3. CCIL NDS-OM         (intra-day G-Sec / SDL trading data)
  4. BSE Debt Segment    (corporate bond day-end trade data)
  5. NSE Capital Market  (corporate / PSU / bank bonds)
  6. NSE NDS-OM API      (government securities — highest priority)
  7. RBI DBIE            (benchmark yields — updates G-Sec curve)

All sources are **free, public, and legal**. No API keys required.
After every successful batch update the engine broadcasts a WebSocket
event to all subscribed clients on the "prices" topic.
"""

from __future__ import annotations

import asyncio
import csv
import io
import math
import json
import random
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from app.shared.database import fetch_all, execute_query
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso
from app.shared.websocket_manager import ws_manager

# ── Shared market constants (single source of truth) ─────────────────────────
from app.shared.market_config import (
    NSE_HEADERS as _NSE_HEADERS,
    BSE_HEADERS as _BSE_HEADERS,
    GSEC_YIELD_CURVE,
    RATING_SPREAD_BPS,
    interpolate_gsec_yield,
    get_synthetic_yield,
)

log = get_logger("market_data_fetcher")


# ─────────────────────────────────────────────────────────────────────────────
class EnhancedMarketDataFetcher:
    """
    Unified market-data engine with multi-source fetch + WebSocket pushes.
    """

    def __init__(self) -> None:
        self.today = date.today()
        self._legacy_market_columns_checked = False

    async def _ensure_unified_market_data_columns(self) -> None:
        """Best-effort schema compatibility for legacy SQLite snapshots."""
        if self._legacy_market_columns_checked:
            return
        for col, col_type in (("ytm_source", "TEXT"), ("bid_yield", "REAL")):
            try:
                await execute_query(
                    f"ALTER TABLE unified_market_data ADD COLUMN {col} {col_type}",
                    {},
                )
            except Exception:
                # Column already exists or engine doesn't require migration.
                pass
        self._legacy_market_columns_checked = True

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC ENTRY POINTS
    # ══════════════════════════════════════════════════════════════════

    async def refresh_all(self, max_bonds: int = 1000) -> dict:
        """
        Refresh market data for every active bond in the universal registry.
        Returns a summary dict; broadcasts a WebSocket event when done.
        """
        bonds = await fetch_all(
            """
            SELECT isin, issuer_type, credit_rating, coupon_rate,
                   maturity_date, face_value
            FROM   universal_bond_master
            WHERE  is_active = 1
            ORDER  BY issuer_type, credit_rating
            LIMIT  :lim
            """,
            {"lim": max_bonds},
        )
        if not bonds:
            log.warning("refresh_all_no_bonds")
            return {"status": "no_bonds", "updated": 0}

        log.info("refresh_starting", bond_count=len(bonds))

        # Fetch live prices from ALL sources in parallel — failures are silent
        nse_gsec, nse_corp, bse_debt, fimmda_val, ccil_data, rbi_curve = (
            await asyncio.gather(
                self._fetch_nse_gsec(),
                self._fetch_nse_corporate(),
                self._fetch_bse_debt(),
                self._fetch_fimmda(),
                self._fetch_ccil_nds_om(),
                self._fetch_rbi_dbie_yields(),
                return_exceptions=True,
            )
        )
        # Convert any exceptions to empty dicts
        for name, val in [("nse_gsec", nse_gsec), ("nse_corp", nse_corp),
                          ("bse_debt", bse_debt), ("fimmda", fimmda_val),
                          ("ccil", ccil_data), ("rbi", rbi_curve)]:
            if isinstance(val, Exception):
                log.warning(f"{name}_fetch_exception", err=str(val))
        nse_gsec   = nse_gsec   if not isinstance(nse_gsec, Exception)   else {}
        nse_corp   = nse_corp   if not isinstance(nse_corp, Exception)   else {}
        bse_debt   = bse_debt   if not isinstance(bse_debt, Exception)   else {}
        fimmda_val = fimmda_val if not isinstance(fimmda_val, Exception) else {}
        ccil_data  = ccil_data  if not isinstance(ccil_data, Exception)  else {}
        rbi_curve  = rbi_curve  if not isinstance(rbi_curve, Exception)  else {}

        # Update G-Sec curve if RBI DBIE returned data
        if rbi_curve and isinstance(rbi_curve, dict) and rbi_curve.get("_curve"):
            curve_data = rbi_curve.pop("_curve")
            # Update the shared config curve at runtime
            try:
                GSEC_YIELD_CURVE.update(curve_data)
                log.info("gsec_curve_updated_from_rbi", points=len(curve_data))
            except Exception:
                pass

        # Merge: lower priority first, higher priority overwrites
        live_prices: dict[str, dict] = {}
        sources_active = []
        for src_name, src_data in [
            ("FIMMDA", fimmda_val),
            ("CCIL", ccil_data),
            ("BSE_DEBT", bse_debt),
            ("NSE_CM", nse_corp),
            ("NSE_NDSM", nse_gsec),
        ]:
            if src_data and isinstance(src_data, dict):
                live_prices.update(src_data)
                if src_data:
                    sources_active.append(f"{src_name}({len(src_data)})")

        updated = 0
        errors = 0
        changed_isins: list[str] = []

        for bond in bonds:
            try:
                md = self._build_market_data(bond, live_prices.get(bond["isin"]))
                await self._upsert(bond["isin"], md)
                updated += 1
                changed_isins.append(bond["isin"])
            except Exception as exc:
                errors += 1
                log.warning("refresh_bond_failed", isin=bond.get("isin"), error=str(exc))

        # ── WebSocket broadcast ──────────────────────────────────────
        await ws_manager.broadcast(
            "prices",
            {
                "event": "market_data_refreshed",
                "updated": updated,
                "errors": errors,
                "as_of": str(self.today),
                "sample_isins": changed_isins[:10],
            },
        )

        log.info("refresh_complete", updated=updated, errors=errors,
                 sources=sources_active)
        return {
            "status": "ok",
            "updated": updated,
            "errors": errors,
            "sources": sources_active or ["SYNTHETIC"],
            "live_count": len(live_prices),
            "as_of": str(self.today),
        }

    async def refresh_one(self, isin: str) -> dict:
        """Refresh a single bond and push its updated data via WebSocket."""
        bond = await fetch_all(
            "SELECT * FROM universal_bond_master WHERE isin = :isin",
            {"isin": isin},
        )
        if not bond:
            return {"status": "not_found"}
        b = bond[0]
        live = (await self._fetch_nse_gsec()).get(isin) or {}
        md = self._build_market_data(b, live or None)
        await self._upsert(isin, md)

        await ws_manager.broadcast(
            "prices",
            {"event": "single_update", "isin": isin, "ytm": md.get("ytm"), "as_of": str(self.today)},
        )
        return {"status": "ok", "isin": isin, **md}

    # ══════════════════════════════════════════════════════════════════
    # SOURCE 1: NSE NDS-OM  (G-Secs)
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_nse_gsec(self) -> dict[str, dict]:
        url = "https://www.nseindia.com/api/liveBonds-traded-on-NDS-OM"
        try:
            async with httpx.AsyncClient(headers=_NSE_HEADERS, timeout=10) as client:
                # Warm up cookie
                await client.get("https://www.nseindia.com", timeout=8)
                resp = await client.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data", []) if isinstance(data, dict) else data

            result: dict[str, dict] = {}
            for item in items:
                isin = item.get("isin") or item.get("ISIN", "")
                if not isin:
                    continue
                result[isin] = {
                    "clean_price": float(item.get("cp", item.get("cleanPrice", 0)) or 0),
                    "ytm": float(item.get("ytm", item.get("yieldToMaturity", 0)) or 0),
                    "traded_volume": float(item.get("tradedQuantity", 0) or 0),
                    "source": "NSE_NDSM",
                }
            log.info("nse_gsec_fetched", count=len(result))
            return result
        except Exception as exc:
            log.warning("nse_gsec_fetch_failed", error=str(exc))
            return {}

    # ══════════════════════════════════════════════════════════════════
    # SOURCE 2: NSE Capital Market  (Corporate / PSU / Bank bonds)
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_nse_corporate(self) -> dict[str, dict]:
        url = "https://www.nseindia.com/api/liveBonds-traded-in-capital-market"
        try:
            async with httpx.AsyncClient(headers=_NSE_HEADERS, timeout=10) as client:
                await client.get("https://www.nseindia.com", timeout=8)
                resp = await client.get(url, timeout=10)
                resp.raise_for_status()
                items = resp.json().get("data", [])

            result: dict[str, dict] = {}
            for item in items:
                isin = item.get("isin", "")
                if not isin:
                    continue
                result[isin] = {
                    "clean_price": float(item.get("cp", 0) or 0),
                    "ytm": float(item.get("yld", 0) or 0),
                    "traded_volume": float(item.get("trdqty", 0) or 0),
                    "bid_yield": float(item.get("bidYield", 0) or 0),
                    "ask_yield": float(item.get("askYield", 0) or 0),
                    "source": "NSE_CM",
                }
            log.info("nse_corp_fetched", count=len(result))
            return result
        except Exception as exc:
            log.warning("nse_corp_fetch_failed", error=str(exc))
            return {}

    # ══════════════════════════════════════════════════════════════════
    # SOURCE 3: BSE Debt Segment
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_bse_debt(self) -> dict[str, dict]:
        """
        BSE provides a CSV download for the debt-segment trade data.
        We parse it and return clean_price / ytm per ISIN.
        """
        today_str = self.today.strftime("%d%m%Y")
        url = (
            f"https://api.bseindia.com/BseIndiaAPI/api/ProduceCSVForDate/w"
            f"?strdate={today_str}&type=BD"
        )
        try:
            async with httpx.AsyncClient(headers=_BSE_HEADERS, timeout=15) as client:
                resp = await client.get(url, timeout=15)
                resp.raise_for_status()
                lines = resp.text.strip().splitlines()

            result: dict[str, dict] = {}
            header = None
            for line in lines:
                cols = [c.strip() for c in line.split(",")]
                if not header:
                    header = [c.upper() for c in cols]
                    continue
                row = dict(zip(header, cols))
                isin = row.get("ISIN", "")
                if not isin or len(isin) != 12:
                    continue
                try:
                    price = float(row.get("CLOSE_PRICE", row.get("CWAP", 0)) or 0)
                    ytm   = float(row.get("YIELD", 0) or 0)
                    vol   = float(row.get("VOLUME", 0) or 0)
                    if price > 0:
                        result[isin] = {"clean_price": price, "ytm": ytm,
                                        "traded_volume": vol, "source": "BSE_DEBT"}
                except ValueError:
                    continue
            log.info("bse_debt_fetched", count=len(result))
            return result
        except Exception as exc:
            log.warning("bse_debt_fetch_failed", error=str(exc))
            return {}

    # ══════════════════════════════════════════════════════════════════
    # SOURCE 4: FIMMDA Daily Valuation
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_fimmda(self) -> dict[str, dict]:
        """
        FIMMDA publishes a daily bond valuation file (typically a CSV/Excel).
        The URL format varies; we attempt the standard dated CSV endpoint.
        Falls back gracefully if the site is unreachable.
        """
        today_str = self.today.strftime("%d-%m-%Y")
        # FIMMDA posts both a CSV and an Excel; try CSV first
        url = f"https://www.fimmda.org/Valuation/DownloadExcelFile?date={today_str}"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, timeout=15)
                resp.raise_for_status()
                lines = resp.text.strip().splitlines()

            result: dict[str, dict] = {}
            header = None
            for line in lines:
                cols = [c.strip().strip('"') for c in line.split(",")]
                if not header:
                    header = [c.upper() for c in cols]
                    continue
                row = dict(zip(header, cols))
                isin = row.get("ISIN", "")
                if not isin or len(isin) != 12:
                    continue
                try:
                    price = float(row.get("SECURITY_PRICE", row.get("PRICE", 0)) or 0)
                    ytm   = float(row.get("YIELD", row.get("YTM", 0)) or 0)
                    if price > 0:
                        result[isin] = {"clean_price": price, "ytm": ytm, "source": "FIMMDA"}
                except ValueError:
                    continue
            log.info("fimmda_fetched", count=len(result))
            return result
        except Exception as exc:
            log.warning("fimmda_fetch_failed", error=str(exc))
            return {}

    # ══════════════════════════════════════════════════════════════════
    # SOURCE 5: CCIL NDS-OM (G-Sec / SDL intra-day trade data)
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_ccil_nds_om(self) -> dict[str, dict]:
        """
        CCIL (Clearing Corporation of India) publishes daily NDS-OM
        (Negotiated Dealing System - Order Matching) trade data.
        This is the primary government securities trading platform.
        Free, public data — no authentication required.

        Sources tried in order:
        1. CCIL NDS-OM daily turnover report (PDF/CSV)
        2. CCIL market data API
        """
        result: dict[str, dict] = {}

        # Try CCIL daily trade data endpoint
        today_str = self.today.strftime("%d%m%Y")
        urls = [
            f"https://www.ccilindia.com/web/ccil/nds-om-market-data?date={today_str}",
            f"https://www.ccilindia.com/web/ccil/nds-om-daily-turnover/-/asset_publisher/data?date={today_str}",
            "https://www.ccilindia.com/web/ccil/nds-om-market-data",
        ]

        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/html, text/csv, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

        for url in urls:
            try:
                async with httpx.AsyncClient(
                    headers=_headers, follow_redirects=True, timeout=15
                ) as client:
                    resp = await client.get(url, timeout=12)
                    if resp.status_code != 200:
                        continue

                    content_type = resp.headers.get("content-type", "")
                    body = resp.text.strip()

                    # Try JSON first
                    if "json" in content_type or body.startswith(("{", "[")):
                        try:
                            data = resp.json()
                            parsed = self._parse_ccil_json(data)
                            if parsed:
                                result.update(parsed)
                                break
                        except Exception:
                            pass

                    # Try CSV parse
                    if body and ("ISIN" in body.upper() or "," in body):
                        parsed = self._parse_ccil_csv(body)
                        if parsed:
                            result.update(parsed)
                            break

            except Exception as exc:
                log.debug("ccil_url_failed", url=url[:60], err=str(exc)[:60])
                continue

        log.info("ccil_nds_om_fetched", count=len(result))
        return result

    def _parse_ccil_json(self, data) -> dict[str, dict]:
        """Parse CCIL JSON response into {isin: {clean_price, ytm, ...}}."""
        result: dict[str, dict] = {}
        items = []
        if isinstance(data, dict):
            items = (data.get("data") or data.get("records") or
                     data.get("trades") or data.get("turnover") or [])
        elif isinstance(data, list):
            items = data

        for item in items:
            if not isinstance(item, dict):
                continue
            isin = str(item.get("isin") or item.get("ISIN") or
                       item.get("securityId") or "").strip()
            if not isin or len(isin) != 12:
                continue
            try:
                price = float(item.get("weightedAvgPrice") or item.get("wtdAvgPrice") or
                              item.get("lastTradePrice") or item.get("closePrice") or 0)
                ytm = float(item.get("weightedAvgYield") or item.get("wtdAvgYield") or
                            item.get("lastTradeYield") or item.get("yield") or 0)
                vol = float(item.get("totalVolume") or item.get("tradedQuantity") or
                            item.get("turnover") or 0)
                if price > 0 or ytm > 0:
                    result[isin] = {
                        "clean_price": price,
                        "ytm": ytm,
                        "traded_volume": vol,
                        "source": "CCIL_NDSM",
                    }
            except (TypeError, ValueError):
                continue
        return result

    def _parse_ccil_csv(self, body: str) -> dict[str, dict]:
        """Parse CCIL CSV data into {isin: {...}}."""
        result: dict[str, dict] = {}
        try:
            reader = csv.DictReader(io.StringIO(body))
            for row in reader:
                # Normalize column names to uppercase
                row = {k.strip().upper(): v.strip() for k, v in row.items() if k}
                isin = row.get("ISIN", row.get("SECURITY_ID", "")).strip()
                if not isin or len(isin) != 12:
                    continue
                try:
                    price = float(row.get("WTD_AVG_PRICE", row.get("CLOSE_PRICE",
                                  row.get("PRICE", 0))) or 0)
                    ytm = float(row.get("WTD_AVG_YIELD", row.get("YIELD",
                                row.get("YTM", 0))) or 0)
                    vol = float(row.get("TOTAL_VOLUME", row.get("VOLUME", 0)) or 0)
                    if price > 0 or ytm > 0:
                        result[isin] = {
                            "clean_price": price,
                            "ytm": ytm,
                            "traded_volume": vol,
                            "source": "CCIL_NDSM",
                        }
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass
        return result

    # ══════════════════════════════════════════════════════════════════
    # SOURCE 6: RBI DBIE — Benchmark G-Sec Yields
    # ══════════════════════════════════════════════════════════════════

    async def _fetch_rbi_dbie_yields(self) -> dict:
        """
        Fetch G-Sec benchmark yields from RBI's Database on Indian Economy (DBIE).
        Free, public API — no auth needed.

        Returns dict with '_curve' key containing yield curve points,
        plus individual bond ISINs if available.

        Endpoints tried:
        1. DBIE REST search API (structured data)
        2. RBI Weekly Statistical Supplement (WSS)
        3. RBI G-Sec yield curve data
        """
        result: dict = {}
        curve: dict[float, float] = {}

        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json, text/html, */*",
        }

        # Try 1: DBIE G-Sec Yield (primary)
        try:
            async with httpx.AsyncClient(
                headers=_headers, follow_redirects=True, timeout=15
            ) as client:
                # DBIE publishes yield curve data at this endpoint
                resp = await client.get(
                    "https://dbie.rbi.org.in/DBIE/dbie.rbi"
                    "?site=publications&body=yield&format=json",
                    timeout=12,
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        curve = self._parse_rbi_yield_data(data)
                    except Exception:
                        # Try parsing as HTML / text for yields
                        curve = self._extract_yields_from_text(resp.text)
        except Exception as exc:
            log.debug("rbi_dbie_primary_failed", err=str(exc)[:60])

        # Try 2: RBI WSS G-Sec yields
        if not curve:
            try:
                async with httpx.AsyncClient(
                    headers=_headers, follow_redirects=True, timeout=15
                ) as client:
                    resp = await client.get(
                        "https://www.rbi.org.in/Scripts/WSSView.aspx",
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        curve = self._extract_yields_from_text(resp.text)
            except Exception as exc:
                log.debug("rbi_wss_failed", err=str(exc)[:60])

        # Try 3: FBIL benchmark (backup for curve)
        if not curve:
            try:
                async with httpx.AsyncClient(
                    headers=_headers, follow_redirects=True, timeout=12
                ) as client:
                    for fbil_url in [
                        "https://fbil.org.in/api/gsec-benchmark/latest",
                        "https://www.fbil.org.in/api/benchmark-rates",
                    ]:
                        try:
                            resp = await client.get(fbil_url, timeout=8)
                            if resp.status_code == 200:
                                data = resp.json()
                                curve = self._parse_fbil_curve(data)
                                if curve:
                                    break
                        except Exception:
                            continue
            except Exception as exc:
                log.debug("fbil_backup_failed", err=str(exc)[:60])

        if curve:
            result["_curve"] = curve
            log.info("rbi_yield_curve_fetched", points=len(curve))
        else:
            log.debug("rbi_yield_curve_no_data")

        return result

    def _parse_rbi_yield_data(self, data) -> dict[float, float]:
        """Parse RBI DBIE JSON yield curve data."""
        curve: dict[float, float] = {}
        items = []
        if isinstance(data, dict):
            items = (data.get("data") or data.get("yields") or
                     data.get("records") or [])
        elif isinstance(data, list):
            items = data

        _tenor_patterns = {
            r"(?:^|\b)91\s*(?:day|d)": 0.25,
            r"(?:^|\b)182\s*(?:day|d)": 0.5,
            r"(?:^|\b)364\s*(?:day|d)": 1.0,
            r"(?:^|\b)1\s*(?:year|yr|y)": 1.0,
            r"(?:^|\b)2\s*(?:year|yr|y)": 2.0,
            r"(?:^|\b)3\s*(?:year|yr|y)": 3.0,
            r"(?:^|\b)5\s*(?:year|yr|y)": 5.0,
            r"(?:^|\b)7\s*(?:year|yr|y)": 7.0,
            r"(?:^|\b)10\s*(?:year|yr|y)": 10.0,
            r"(?:^|\b)14\s*(?:year|yr|y)": 14.0,
            r"(?:^|\b)20\s*(?:year|yr|y)": 20.0,
            r"(?:^|\b)30\s*(?:year|yr|y)": 30.0,
            r"(?:^|\b)40\s*(?:year|yr|y)": 40.0,
        }

        for item in items:
            if not isinstance(item, dict):
                continue
            tenor_str = str(item.get("tenor") or item.get("maturity") or
                           item.get("description") or "").lower()
            rate = item.get("yield") or item.get("rate") or item.get("value")
            if not rate:
                continue
            for pattern, years in _tenor_patterns.items():
                if re.search(pattern, tenor_str, re.IGNORECASE):
                    try:
                        curve[years] = float(rate)
                    except (TypeError, ValueError):
                        pass
                    break
        return curve

    def _extract_yields_from_text(self, html: str) -> dict[float, float]:
        """
        Extract yield percentages from RBI HTML/text pages.
        Looks for patterns like "10-year: 7.10%" or table data.
        """
        curve: dict[float, float] = {}
        _patterns = [
            (r"91[- ]?day.*?(\d+\.\d+)\s*%?", 0.25),
            (r"182[- ]?day.*?(\d+\.\d+)\s*%?", 0.5),
            (r"364[- ]?day.*?(\d+\.\d+)\s*%?", 1.0),
            (r"1[- ]?year.*?(\d+\.\d+)\s*%?", 1.0),
            (r"2[- ]?year.*?(\d+\.\d+)\s*%?", 2.0),
            (r"5[- ]?year.*?(\d+\.\d+)\s*%?", 5.0),
            (r"10[- ]?year.*?(\d+\.\d+)\s*%?", 10.0),
            (r"30[- ]?year.*?(\d+\.\d+)\s*%?", 30.0),
        ]
        for pattern, tenor in _patterns:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                try:
                    val = float(m.group(1))
                    if 2.0 <= val <= 15.0:  # sanity check
                        curve[tenor] = val
                except (ValueError, TypeError):
                    pass
        return curve

    def _parse_fbil_curve(self, data) -> dict[float, float]:
        """Parse FBIL benchmark rates JSON into yield curve."""
        curve: dict[float, float] = {}
        items = (data.get("data") or data.get("rates") or
                 data.get("benchmarks") or (data if isinstance(data, list) else []))
        _tenor_map = {
            "1M": 0.083, "3M": 0.25, "6M": 0.5, "1Y": 1, "2Y": 2,
            "3Y": 3, "5Y": 5, "7Y": 7, "10Y": 10, "14Y": 14,
            "20Y": 20, "30Y": 30, "40Y": 40,
        }
        for item in (items if isinstance(items, list) else []):
            tenor_str = str(item.get("tenor") or item.get("maturity") or "").upper()
            rate_val = item.get("yield") or item.get("rate") or item.get("value")
            if tenor_str in _tenor_map and rate_val:
                try:
                    curve[_tenor_map[tenor_str]] = float(rate_val)
                except (TypeError, ValueError):
                    pass
        return curve

    # ══════════════════════════════════════════════════════════════════
    # FALLBACK SYNTHETIC PRICING
    # ══════════════════════════════════════════════════════════════════

    def _interpolate_gsec_yield(self, years: float) -> float:
        """Linear interpolation across the benchmark G-Sec curve — delegates to shared config."""
        return interpolate_gsec_yield(years)

    def _synthetic_price(self, bond: dict) -> dict:
        """Generate realistic synthetic market data using spread model."""
        today = self.today
        try:
            mat = date.fromisoformat(str(bond["maturity_date"]))
        except Exception:
            mat = today + timedelta(days=365 * 5)

        years = max((mat - today).days / 365.25, 0.01)
        base_yield = self._interpolate_gsec_yield(years)
        rating = str(bond.get("credit_rating") or "AAA").upper()
        spread_bps = RATING_SPREAD_BPS.get(rating, 100)
        ytm = round(base_yield + spread_bps / 100 + random.uniform(-0.05, 0.05), 4)

        coupon = float(bond.get("coupon_rate") or 0)
        face = float(bond.get("face_value") or 1000)
        freq = int(bond.get("coupon_frequency") or 2) or 2

        # Approximate clean price via present-value formula
        n = max(int(years * freq), 1)
        r = ytm / 100 / freq
        cp = coupon / 100 * face / freq
        if r > 0:
            pv_coupons = cp * (1 - (1 + r) ** -n) / r
        else:
            pv_coupons = cp * n
        pv_principal = face / (1 + r) ** n
        clean_price = round(pv_coupons + pv_principal, 4)

        # Modified Duration (approximate)
        macaulay = sum(
            (t / freq) * cp / (1 + r) ** t for t in range(1, n + 1)
        ) + (n / freq) * face / (1 + r) ** n
        macaulay /= clean_price
        mod_dur = round(macaulay / (1 + r), 4)
        dv01 = round(clean_price * mod_dur / 10_000, 6)

        spread = round((ytm - base_yield) * 100, 2)

        return {
            "clean_price": clean_price,
            "ytm": ytm,
            "modified_duration": mod_dur,
            "dv01": dv01,
            "convexity": round(mod_dur ** 2 * 1.1, 4),  # approximation
            "spread_over_gsec": spread,
            "traded_volume": 0.0,
            "liquidity_score": 90 if rating == "SOVEREIGN" else 70 if rating in ("AAA", "AA+") else 50,
            "data_source": "SYNTHETIC",
        }

    def _build_market_data(self, bond: dict, live: Optional[dict]) -> dict:
        """Merge live feed data with computed analytics; fall back to synthetic."""
        synth = self._synthetic_price(bond)

        if live and live.get("clean_price", 0) > 0:
            synth["clean_price"] = live["clean_price"]
            if live.get("ytm", 0) > 0:
                synth["ytm"] = live["ytm"]
            synth["data_source"] = live.get("source", "LIVE")
            synth["traded_volume"] = live.get("traded_volume", 0)
            if live.get("bid_yield"):
                synth["bid_yield"] = live["bid_yield"]
            if live.get("ask_yield"):
                synth["ask_yield"] = live["ask_yield"]

        return synth

    # ══════════════════════════════════════════════════════════════════
    # DB UPSERT
    # ══════════════════════════════════════════════════════════════════

    async def _upsert(self, isin: str, md: dict) -> None:
        today_str = str(self.today)
        query = (
            """
            INSERT INTO unified_market_data (
                isin, price_date, clean_price, ytm, ytm_source,
                bid_yield, ask_yield, modified_duration, dv01, convexity,
                spread_over_gsec, traded_volume, liquidity_score,
                data_source, source_name, source_type, ingestion_method,
                source_reference, timestamp_ingested, timestamp_effective,
                provenance_json, last_updated
            ) VALUES (
                :isin, :price_date, :clean_price, :ytm, :ytm_source,
                :bid_yield, :ask_yield, :modified_duration, :dv01, :convexity,
                :spread_over_gsec, :traded_volume, :liquidity_score,
                :data_source, :source_name, :source_type, :ingestion_method,
                :source_reference, :timestamp_ingested, :timestamp_effective,
                :provenance_json, datetime('now')
            )
            ON CONFLICT(isin, price_date) DO UPDATE SET
                clean_price       = excluded.clean_price,
                ytm               = excluded.ytm,
                ytm_source        = excluded.ytm_source,
                modified_duration = excluded.modified_duration,
                dv01              = excluded.dv01,
                convexity         = excluded.convexity,
                spread_over_gsec  = excluded.spread_over_gsec,
                traded_volume     = excluded.traded_volume,
                liquidity_score   = excluded.liquidity_score,
                data_source       = excluded.data_source,
                source_name       = excluded.source_name,
                source_type       = excluded.source_type,
                ingestion_method  = excluded.ingestion_method,
                source_reference  = excluded.source_reference,
                timestamp_ingested = excluded.timestamp_ingested,
                timestamp_effective = excluded.timestamp_effective,
                provenance_json   = excluded.provenance_json,
                last_updated      = datetime('now')
            """
        )
        params = {
                "isin": isin,
                "price_date": today_str,
                "clean_price": md.get("clean_price"),
                "ytm": md.get("ytm"),
                "ytm_source": md.get("data_source", "SYNTHETIC"),
                "bid_yield": md.get("bid_yield"),
                "ask_yield": md.get("ask_yield"),
                "modified_duration": md.get("modified_duration"),
                "dv01": md.get("dv01"),
                "convexity": md.get("convexity"),
                "spread_over_gsec": md.get("spread_over_gsec"),
                "traded_volume": md.get("traded_volume", 0),
                "liquidity_score": md.get("liquidity_score"),
                "data_source": md.get("data_source", "SYNTHETIC"),
                "source_name": md.get("source_name") or md.get("data_source", "SYNTHETIC"),
                "source_type": md.get("source_type") or "public",
                "ingestion_method": md.get("ingestion_method") or "api",
                "source_reference": md.get("source_reference") or md.get("data_source", "SYNTHETIC"),
                "timestamp_ingested": md.get("timestamp_ingested") or utcnow_iso(),
                "timestamp_effective": md.get("timestamp_effective") or today_str,
                "provenance_json": json.dumps({
                    "source_name": md.get("source_name") or md.get("data_source", "SYNTHETIC"),
                    "source_type": md.get("source_type") or "public",
                    "ingestion_method": md.get("ingestion_method") or "api",
                    "source_reference": md.get("source_reference") or md.get("data_source", "SYNTHETIC"),
                    "timestamp_ingested": md.get("timestamp_ingested") or utcnow_iso(),
                    "timestamp_effective": md.get("timestamp_effective") or today_str,
                }, default=str),
            }
        try:
            await execute_query(query, params)
        except Exception as exc:
            err = str(exc).lower()
            if "no column named ytm_source" not in err and "no column named bid_yield" not in err:
                raise
            await self._ensure_unified_market_data_columns()
            await execute_query(query, params)
