"""
Information Memorandum (IM) Fetcher
=====================================
Fetches bond IM metadata from NSE, BSE, and SEBI and stores it in
bond_im_registry + im_fetch_log.

Why IM matters for compliance
------------------------------
SEBI NCS (Non-Convertible Securities) Regulations 2021 (Reg 27) require
issuers to file an IM within 2 working days of allotment for *every*
private-placement NCS.  For public issues a shelf / offer document must be
filed before subscription opens.  PF Trusts are required to verify that a
valid, current IM exists before executing any purchase.

Sources
-------
NSE Corporate Filings (offer documents):
  GET https://www.nseindia.com/api/corporateFilings-offer
      ?industry=&symbol=&documentType=Information+Memorandum
      &fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY&limit=100

BSE Memorandum Disclosure:
  GET https://api.bseindia.com/BseIndiaAPI/api/MemDisclose/w
      ?strtype=IM&strscrip=&strisno=&strdt=DD%2FMM%2FYYYY

SEBI Listing (HTML scrape of IM filings page):
  GET https://www.sebi.gov.in/sebiweb/home/HomeAction.do
      ?doListing=yes&sid=3&ssid=17&smid=39

Usage
-----
    from app.domain.market_data.im_ingestion import IMFetcher

    fetcher = IMFetcher()
    result  = await fetcher.fetch_all_sources(days_back=30)
    im_rec  = await fetcher.get_im(isin="INE001A01036")
    missing = await fetcher.get_bonds_without_im()
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Optional

import httpx

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.market_config import NSE_HEADERS, BSE_HEADERS

log = get_logger("im_fetcher")

# â”€â”€ Endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_NSE_WARMUP      = "https://www.nseindia.com/"
_NSE_FILINGS_URL = (
    "https://www.nseindia.com/api/corporateFilings-offer"
    "?industry=&symbol=&documentType=Information+Memorandum"
    "&fromDate={from_date}&toDate={to_date}&limit=200"
)
_NSE_BASE_URL    = "https://www.nseindia.com"

_BSE_IM_URL      = (
    "https://api.bseindia.com/BseIndiaAPI/api/MemDisclose/w"
    "?strtype=IM&strscrip=&strisno=&strdt={dt}"
)
_BSE_IM_BY_ISIN  = (
    "https://api.bseindia.com/BseIndiaAPI/api/MemDisclose/w"
    "?strtype=IM&strscrip={isin}&strisno=&strdt="
)

_SEBI_IM_URL     = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
    "?doListing=yes&sid=3&ssid=17&smid=39"
)

_TIMEOUT = 25  # seconds


class IMFetcher:
    """Fetches and persists Information Memorandum records from NSE, BSE, SEBI."""

    # â”€â”€ NSE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _nse_session(self) -> httpx.AsyncClient:
        """Returns an httpx client with NSE cookies set."""
        client = httpx.AsyncClient(
            headers=NSE_HEADERS,
            follow_redirects=True,
            timeout=_TIMEOUT,
        )
        try:
            await client.get(_NSE_WARMUP)
        except Exception as exc:
            log.warning("nse_warmup_error", error=str(exc))
        return client

    async def _fetch_nse_im(self, from_dt: date, to_dt: date) -> list[dict]:
        """
        Fetches IM filings from NSE corporate filings API.
        Returns normalised IMRecord dicts.
        """
        url = _NSE_FILINGS_URL.format(
            from_date=from_dt.strftime("%d-%m-%Y"),
            to_date=to_dt.strftime("%d-%m-%Y"),
        )
        client = await self._nse_session()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log.warning("nse_im_fetch_failed", error=str(exc))
            return []
        finally:
            await client.aclose()

        # NSE response: {"data": [{...}]} or list directly
        raw_items = payload.get("data") or (payload if isinstance(payload, list) else [])
        records: list[dict] = []
        for item in raw_items:
            isin = (item.get("isin") or item.get("ISIN") or "").strip()
            if not isin:
                continue
            # Build document URL from relative path
            doc_path = item.get("attachmentFile") or item.get("fileLink") or ""
            doc_url  = (_NSE_BASE_URL + doc_path) if doc_path.startswith("/") else doc_path

            records.append({
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
            })
        log.info("nse_im_fetched", count=len(records))
        return records

    # â”€â”€ BSE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _fetch_bse_im(self, from_dt: date) -> list[dict]:
        """
        Fetches IM disclosures from BSE MemDisclose API.
        BSE returns a list of disclosures for a given date.
        """
        url = _BSE_IM_URL.format(dt=from_dt.strftime("%d%%2F%m%%2F%Y"))
        try:
            async with httpx.AsyncClient(
                headers=BSE_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("bse_im_fetch_failed", error=str(exc))
            return []

        raw_items = (
            payload.get("Table")
            or payload.get("data")
            or (payload if isinstance(payload, list) else [])
        )
        records: list[dict] = []
        for item in raw_items:
            isin = (
                item.get("ISIN") or item.get("Isin") or item.get("isin") or ""
            ).strip()
            if not isin:
                continue
            records.append({
                "isin":          isin,
                "issuer_name":   item.get("CompanyName") or item.get("COMPANY"),
                "source":        "BSE",
                "document_type": "IM",
                "document_url":  item.get("AttchFilePath") or item.get("URL"),
                "symbol":        item.get("SCRIPCD") or item.get("ScripCode"),
                "series":        item.get("Series") or item.get("SERIES"),
                "filing_date":   _parse_date(
                                     item.get("FilingDate") or item.get("DissemDT")
                                 ),
                "issue_size_cr": _f(item.get("IssueSize") or item.get("ISSUE_SIZE")),
                "coupon_rate":   _f(item.get("CouponRate") or item.get("COUPON_RATE")),
                "maturity_date": _parse_date(
                                     item.get("MaturityDate") or item.get("MATURITY_DATE")
                                 ),
                "credit_rating": item.get("Rating") or item.get("CREDIT_RATING"),
                "rating_agency": item.get("RatingAgency"),
                "secured":       1 if "secure" in str(
                                     item.get("SecurityType", "")).lower() else 0,
            })
        log.info("bse_im_fetched", count=len(records), date=from_dt.isoformat())
        return records

    async def fetch_bse_im_for_isin(self, isin: str) -> list[dict]:
        """Fetch BSE IM records specifically for a single ISIN."""
        url = _BSE_IM_BY_ISIN.format(isin=isin)
        try:
            async with httpx.AsyncClient(
                headers=BSE_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("bse_im_isin_fetch_failed", isin=isin, error=str(exc))
            return []

        raw_items = payload.get("Table") or payload.get("data") or []
        records: list[dict] = []
        for item in raw_items:
            records.append({
                "isin":          isin,
                "issuer_name":   item.get("CompanyName") or item.get("COMPANY"),
                "source":        "BSE",
                "document_type": "IM",
                "document_url":  item.get("AttchFilePath") or item.get("URL"),
                "symbol":        item.get("SCRIPCD"),
                "filing_date":   _parse_date(item.get("FilingDate") or item.get("DissemDT")),
                "issue_size_cr": _f(item.get("IssueSize")),
                "coupon_rate":   _f(item.get("CouponRate")),
                "maturity_date": _parse_date(item.get("MaturityDate")),
                "credit_rating": item.get("Rating"),
                "rating_agency": item.get("RatingAgency"),
                "secured":       1 if "secure" in str(
                                     item.get("SecurityType", "")).lower() else 0,
            })
        return records

    # â”€â”€ SEBI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _fetch_sebi_im(self) -> list[dict]:
        """
        Scrapes the SEBI IM listing page (HTML) and extracts ISIN / issuer /
        filing date rows.  Returns normalised dicts.

        SEBI page format (typical):
          <table ...>
            <tr><td>Sr No</td><td>Company Name</td><td>ISIN</td><td>Date</td><td>Link</td></tr>
          </table>
        """
        try:
            async with httpx.AsyncClient(
                headers={**NSE_HEADERS, "Referer": "https://www.sebi.gov.in/"},
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(_SEBI_IM_URL)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            log.warning("sebi_im_fetch_failed", error=str(exc))
            return []

        records: list[dict] = []
        # Extract rows: ISIN pattern (12-char starting with IN)
        isin_pattern = re.compile(r"\b(IN[A-Z0-9]{10})\b")
        # Extract table rows
        row_pattern  = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
        cell_pattern = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
        link_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)

        for row_m in row_pattern.finditer(html):
            row_html = row_m.group(1)
            cells = [re.sub(r"<[^>]+>", "", c.group(1)).strip()
                     for c in cell_pattern.finditer(row_html)]
            if len(cells) < 3:
                continue
            # Find ISIN in any cell
            isin = None
            for cell in cells:
                m = isin_pattern.search(cell)
                if m:
                    isin = m.group(1)
                    break
            if not isin:
                continue
            # Try to find a doc link
            link_m   = link_pattern.search(row_html)
            doc_url  = link_m.group(1) if link_m else None
            if doc_url and doc_url.startswith("/"):
                doc_url = "https://www.sebi.gov.in" + doc_url

            records.append({
                "isin":          isin,
                "issuer_name":   cells[1] if len(cells) > 1 else None,
                "source":        "SEBI",
                "document_type": "IM",
                "document_url":  doc_url,
                "filing_date":   _parse_date(cells[3]) if len(cells) > 3 else None,
            })

        log.info("sebi_im_scraped", count=len(records))
        return records

    # â”€â”€ Persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _store_im_records(
        self, records: list[dict], source: str
    ) -> tuple[int, int]:
        """Upsert IM records into bond_im_registry.  Returns (new, updated)."""
        new_cnt = 0
        upd_cnt = 0
        for rec in records:
            if not rec.get("isin"):
                continue
            existing = await fetch_one(
                """SELECT id FROM bond_im_registry
                   WHERE isin = :isin AND source = :src AND document_type = :dt""",
                {"isin": rec["isin"], "src": rec["source"],
                 "dt":   rec.get("document_type", "IM")},
            )
            try:
                await execute_query(
                    """
                    INSERT INTO bond_im_registry
                        (isin, issuer_name, source, document_type, document_url,
                         issue_size_cr, coupon_rate, maturity_date, allotment_date,
                         listing_date, credit_rating, rating_agency, secured,
                         filing_date, symbol, series)
                    VALUES
                        (:isin, :issuer_name, :source, :document_type, :document_url,
                         :issue_size_cr, :coupon_rate, :maturity_date, :allotment_date,
                         :listing_date, :credit_rating, :rating_agency, :secured,
                         :filing_date, :symbol, :series)
                    ON CONFLICT(isin, source, document_type) DO UPDATE SET
                        document_url  = COALESCE(excluded.document_url,  bond_im_registry.document_url),
                        issue_size_cr = COALESCE(excluded.issue_size_cr, bond_im_registry.issue_size_cr),
                        coupon_rate   = COALESCE(excluded.coupon_rate,   bond_im_registry.coupon_rate),
                        maturity_date = COALESCE(excluded.maturity_date, bond_im_registry.maturity_date),
                        credit_rating = COALESCE(excluded.credit_rating, bond_im_registry.credit_rating),
                        rating_agency = COALESCE(excluded.rating_agency, bond_im_registry.rating_agency),
                        filing_date   = COALESCE(excluded.filing_date,   bond_im_registry.filing_date),
                        registered_at = datetime('now')
                    """,
                    {
                        "isin":          rec.get("isin"),
                        "issuer_name":   rec.get("issuer_name"),
                        "source":        rec.get("source", source),
                        "document_type": rec.get("document_type", "IM"),
                        "document_url":  rec.get("document_url"),
                        "issue_size_cr": rec.get("issue_size_cr"),
                        "coupon_rate":   rec.get("coupon_rate"),
                        "maturity_date": rec.get("maturity_date"),
                        "allotment_date":rec.get("allotment_date"),
                        "listing_date":  rec.get("listing_date"),
                        "credit_rating": rec.get("credit_rating"),
                        "rating_agency": rec.get("rating_agency"),
                        "secured":       rec.get("secured", 0),
                        "filing_date":   rec.get("filing_date"),
                        "symbol":        rec.get("symbol"),
                        "series":        rec.get("series"),
                    },
                )
                if existing:
                    upd_cnt += 1
                else:
                    new_cnt += 1
            except Exception as exc:
                log.warning("im_store_error", isin=rec.get("isin"), error=str(exc))
        return new_cnt, upd_cnt

    async def _log_fetch(
        self,
        source: str,
        isins_found: int,
        new: int,
        updated: int,
        errors: str = "",
    ):
        await execute_query(
            """INSERT INTO im_fetch_log
               (fetch_date, source, isins_found, new_records, updated_records, errors)
               VALUES (:dt, :src, :found, :new, :upd, :err)""",
            {
                "dt":    date.today().isoformat(),
                "src":   source,
                "found": isins_found,
                "new":   new,
                "upd":   updated,
                "err":   errors,
            },
        )

    # â”€â”€ Public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def fetch_all_sources(self, days_back: int = 30) -> dict:
        """
        Fetch IMs from NSE, BSE, and SEBI for the last `days_back` days.
        Returns a consolidated summary.
        """
        today   = date.today()
        from_dt = today - timedelta(days=days_back)
        log.info("im_fetch_all", from_dt=from_dt.isoformat(), to_dt=today.isoformat())

        # Run all three sources concurrently
        nse_task  = self._fetch_nse_im(from_dt, today)
        bse_task  = self._fetch_bse_im(from_dt)
        sebi_task = self._fetch_sebi_im()

        nse_recs, bse_recs, sebi_recs = await asyncio.gather(
            nse_task, bse_task, sebi_task, return_exceptions=True
        )

        results = {}
        for source, recs in (("NSE", nse_recs), ("BSE", bse_recs), ("SEBI", sebi_recs)):
            if isinstance(recs, Exception):
                err = str(recs)
                log.warning("im_source_error", source=source, error=err)
                recs = []
                await self._log_fetch(source, 0, 0, 0, errors=err)
            else:
                new, upd = await self._store_im_records(recs, source)
                await self._log_fetch(source, len(recs), new, upd)
                results[source.lower()] = {
                    "found": len(recs), "new": new, "updated": upd
                }

        log.info("im_fetch_complete", results=results)
        return {
            "from_date": from_dt.isoformat(),
            "to_date":   today.isoformat(),
            **results,
        }

    async def fetch_for_isin(self, isin: str) -> dict:
        """Fetch and store IM for a single ISIN from all available sources."""
        # NSE: fetch today's window (most recent month)
        today   = date.today()
        from_dt = today - timedelta(days=90)  # broader window for a specific ISIN

        nse_recs, bse_recs = await asyncio.gather(
            self._fetch_nse_im(from_dt, today),
            self.fetch_bse_im_for_isin(isin),
        )

        # Filter NSE results to this ISIN only
        nse_filtered = [r for r in (nse_recs if not isinstance(nse_recs, Exception) else [])
                        if r.get("isin") == isin]
        bse_filtered = bse_recs if not isinstance(bse_recs, Exception) else []

        all_recs = nse_filtered + bse_filtered
        if all_recs:
            await self._store_im_records(all_recs, "MULTI")

        return {
            "isin":    isin,
            "nse_recs": len(nse_filtered),
            "bse_recs": len(bse_filtered),
            "stored":  len(all_recs),
        }

    async def get_im(self, isin: str) -> Optional[dict]:
        """Return the most recent IM record for an ISIN (any source)."""
        return await fetch_one(
            """SELECT * FROM bond_im_registry
               WHERE isin = :isin
               ORDER BY filing_date DESC, registered_at DESC
               LIMIT 1""",
            {"isin": isin},
        )

    async def get_all_im(self, isin: str) -> list[dict]:
        """Return all IM records for an ISIN across all sources."""
        return await fetch_all(
            """SELECT * FROM bond_im_registry
               WHERE isin = :isin
               ORDER BY source, filing_date DESC""",
            {"isin": isin},
        )

    async def get_bonds_without_im(self) -> list[dict]:
        """
        Returns bonds in the portfolio that have NO IM record in any source.
        These are candidates for an IM compliance WARN/FAIL.
        """
        return await fetch_all(
            """
            SELECT p.isin, b.bond_name, b.issuer_name, b.issuer_type, b.credit_rating
            FROM   portfolio_current_positions p
            JOIN   universal_bond_master b ON b.isin = p.isin
            WHERE  b.issuer_type NOT IN ('G-SEC', 'T-BILL', 'SDL')
              AND  NOT EXISTS (
                      SELECT 1 FROM bond_im_registry r WHERE r.isin = p.isin
                   )
            ORDER  BY b.issuer_name
            """
        )

    async def verify_im_terms(self, isin: str) -> dict:
        """
        Cross-check IM coupon / maturity against bond master.
        Returns a dict with match flags suitable for compliance.
        """
        im = await self.get_im(isin)
        bond = await fetch_one(
            "SELECT * FROM universal_bond_master WHERE isin = :isin", {"isin": isin}
        )
        if not bond:
            return {"isin": isin, "bond_found": False}
        if not im:
            return {"isin": isin, "bond_found": True, "im_found": False}

        coupon_match   = True
        maturity_match = True

        if im.get("coupon_rate") and bond.get("coupon_rate"):
            coupon_match = abs(float(im["coupon_rate"]) - float(bond["coupon_rate"])) < 0.05

        if im.get("maturity_date") and bond.get("maturity_date"):
            maturity_match = str(im["maturity_date"])[:10] == str(bond["maturity_date"])[:10]

        return {
            "isin":          isin,
            "bond_found":    True,
            "im_found":      True,
            "im_source":     im.get("source"),
            "im_filing_date":im.get("filing_date"),
            "coupon_match":  coupon_match,
            "maturity_match":maturity_match,
            "im_url":        im.get("document_url"),
        }


# â”€â”€ Private helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _f(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "", "âˆ’", "-") else None
    except (TypeError, ValueError):
        return None


def _parse_date(raw) -> Optional[str]:
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw or raw in ("-", "N/A", "NA"):
        return None
    from datetime import datetime as _dt
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d",
                "%d %b %Y", "%d-%b-%Y", "%d %B %Y"):
        try:
            return _dt.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: return first 10 chars if looks like ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return None

