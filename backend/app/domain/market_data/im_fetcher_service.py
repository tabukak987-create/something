"""
IM Fetcher Service — Unified Gateway
======================================
Scrapes Information Memoranda / Prospectuses from SEBI, BSE and NSE,
downloads the PDFs, extracts text, identifies ISINs and stores results
in the `im_documents` DB table for offline heuristic analysis.

Sources:
  SEBI  — https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=17&smid=39
          Static HTML pages, scrapeable. ~580+ records, 25/page.

  BSE   — https://api.bseindia.com/BseIndiaAPI/api/PrivatePlacements/w
          REST JSON API for private placements / debentures.

  NSE   — https://www.nseindia.com/api/corporateFilings-offer-documents
          REST JSON API for corporate offer documents.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..shared.database import execute_query, fetch_all, fetch_one
from ..shared.time_utils import utcnow_iso

logger = logging.getLogger("im_fetcher")


# ─── Constants ────────────────────────────────────────────────────────────────

SEBI_LISTING_URL = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
    "?doListing=yes&sid=3&ssid=17&smid=39"
)
SEBI_BASE = "https://www.sebi.gov.in"

BSE_PLACEMENTS_API = (
    "https://api.bseindia.com/BseIndiaAPI/api/PrivatePlacements/w"
    "?strType=D&scripcode=&ddMonth=&ddYear=&textdate="
)
BSE_BASE = "https://www.bseindia.com"

NSE_OFFER_DOCS_API = (
    "https://www.nseindia.com/api/corporateFilings-offer-documents"
    "?category=debt&issuerName=&fromDate=&toDate="
)
NSE_BASE = "https://www.nseindia.com"

ISIN_PATTERN = re.compile(r'\b(IN[A-Z0-9]{10})\b')

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# In-memory fetch job state (pid-local, reset on restart)
_fetch_jobs: Dict[str, Dict] = {}


# ─── Text Extraction Helpers ──────────────────────────────────────────────────

def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract all text from PDF bytes using pdfminer (preferred) → PyPDF2 fallback."""
    try:
        from pdfminer.high_level import extract_text as pm_extract
        text = pm_extract(io.BytesIO(pdf_bytes))
        if text and len(text.strip()) > 100:
            return text
    except Exception:
        pass
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                pass
        return " ".join(parts)
    except Exception:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    except Exception as e:
        logger.warning(f"All PDF extraction methods failed: {e}")
        return ""


def _find_isins(text: str) -> List[str]:
    """Return unique ISINs found in extracted text."""
    return list(dict.fromkeys(ISIN_PATTERN.findall(text)))


def _find_pdf_urls_in_html(html: str, base_url: str = "") -> List[str]:
    """
    Extract PDF URLs from raw HTML content.
    Looks for:  src/href/data attributes pointing to .pdf files
    or embedded PDF viewer script blocks.
    """
    candidates: List[str] = []
    # Pattern 1: src="...pdf", href="...pdf", data="...pdf"
    for m in re.finditer(
        r"""(?:src|href|data|url)\s*=\s*['"]([^'"]*?\.pdf[^'"]*)['"]""",
        html, re.IGNORECASE
    ):
        candidates.append(m.group(1).strip())
    # Pattern 2: window.location / location.href with .pdf
    for m in re.finditer(r"""["']((?:https?://|/)[^"']*?\.pdf[^"']*)["']""", html):
        candidates.append(m.group(1).strip())
    # Clean up and resolve relative URLs
    result: List[str] = []
    for u in candidates:
        if u.startswith("http"):
            result.append(u)
        elif u.startswith("//"):
            result.append("https:" + u)
        elif u.startswith("/") and base_url:
            # base_url = https://www.sebi.gov.in
            root = re.match(r"(https?://[^/]+)", base_url)
            if root:
                result.append(root.group(1) + u)
        # skip relative non-root paths
    return list(dict.fromkeys(result))  # deduplicate preserving order


def _clean_issuer_name(title: str) -> str:
    """Strip doc type suffixes from title to get cleaner issuer name."""
    for sfx in [
        " – Tranche", " - Tranche", "– Tranche", "- Tranche",
        " – Prospectus", " - Prospectus", "– Prospectus", "- Prospectus",
        " – Shelf Prospectus", " - Shelf Prospectus",
        " – Information Memorandum", " - Information Memorandum",
        " – Placement Memorandum", " - Placement Memorandum",
    ]:
        if sfx.lower() in title.lower():
            idx = title.lower().find(sfx.lower())
            return title[:idx].strip()
    return title.strip()


def _detect_doc_type(title: str) -> str:
    tl = title.lower()
    if "shelf prospectus" in tl:
        return "SHELF_PROSPECTUS"
    if "tranche" in tl:
        return "TRANCHE"
    if "information memorandum" in tl or "placement memorandum" in tl:
        return "IM"
    if "prospectus" in tl:
        return "PROSPECTUS"
    return "OTHER"


# ─── Database Helpers ─────────────────────────────────────────────────────────

async def _upsert_document(meta: dict) -> Optional[int]:
    """
    Insert a new im_documents row or update if doc_url already exists.
    Returns the row id.
    """
    if not meta.get("doc_url") and not meta.get("doc_title"):
        return None

    existing = None
    if meta.get("doc_url"):
        existing = await fetch_one(
            "SELECT id FROM im_documents WHERE doc_url = :doc_url",
            {"doc_url": meta["doc_url"]}
        )

    now = utcnow_iso()[:19]
    meta.setdefault("created_at", now)
    meta["updated_at"] = now

    if existing:
        doc_id = existing["id"]
        sets = ", ".join(f"{k} = :{k}" for k in meta if k not in ("doc_url", "created_at"))
        await execute_query(
            f"UPDATE im_documents SET {sets} WHERE doc_url = :doc_url", meta
        )
        return doc_id
    else:
        cols = ", ".join(meta.keys())
        vals = ", ".join(f":{k}" for k in meta.keys())
        await execute_query(
            f"INSERT INTO im_documents ({cols}) VALUES ({vals})", meta
        )
        row = await fetch_one(
            "SELECT id FROM im_documents WHERE doc_url = :doc_url",
            {"doc_url": meta.get("doc_url", "")}
        )
        return row["id"] if row else None


async def _update_status(doc_id: int, status: str, error: str = None, extra: dict = None):
    d: dict = {"id": doc_id, "extraction_status": status, "updated_at": utcnow_iso()}
    if error:
        d["error_message"] = error[:500]
    if extra:
        d.update(extra)
    sets = ", ".join(f"{k} = :{k}" for k in d if k != "id")
    await execute_query(f"UPDATE im_documents SET {sets} WHERE id = :id", d)


# ─── SEBI Scraper ─────────────────────────────────────────────────────────────

def _parse_sebi_listing_html(html: str) -> List[Dict]:
    """
    Parse SEBI listing page HTML and return list of
    {'title': str, 'doc_url': str, 'filing_date': str}.
    SEBI uses full absolute URLs in href attributes.
    """
    results: List[Dict] = []
    # SEBI listing entries: absolute URLs to /filings/debt-offer-document/
    # e.g. href="https://www.sebi.gov.in/filings/debt-offer-document/feb-2026/xyz_100039.html"
    for m in re.finditer(
        r'''<a[^>]+href=["']((?:https?://(?:www\.)?sebi\.gov\.in)?/filings/debt-offer-document/[^"']+\.html)["'][^>]*>\s*([^<]+)\s*</a>''',
        html, re.IGNORECASE | re.DOTALL
    ):
        href = m.group(1).strip()
        title = re.sub(r'\s+', ' ', m.group(2)).strip()
        # Fix Windows-1252 encoding artifacts (û → –, etc.)
        title = title.replace('\xfb', '–').replace('\u00fb', '–').replace('û', '–')
        if not title or len(title) < 5:
            continue
        # Normalise to full URL
        if href.startswith("/"):
            doc_url = SEBI_BASE + href
        else:
            doc_url = href
        results.append({"title": title, "doc_url": doc_url, "filing_date": ""})

    # Try to extract dates — look for adjacent <td> cells
    date_pattern = re.compile(
        r'(\d{1,2}[- /]\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[- ,/\s]*\d{4})',
        re.IGNORECASE
    )
    all_dates = [(m.start(), m.group(1)) for m in date_pattern.finditer(html)]
    for entry in results:
        # Search for the URL fragment to locate position in HTML
        url_frag = entry["doc_url"]
        # Try both full URL and path
        idx = html.find(url_frag)
        if idx < 0:
            path_part = url_frag.replace(SEBI_BASE, "")
            idx = html.find(path_part)
        if idx < 0:
            continue
        best_date = ""
        for pos, d_str in reversed(all_dates):
            if pos <= idx:
                best_date = _normalise_date(d_str)
                break
        entry["filing_date"] = best_date

    return results


def _normalise_date(d_str: str) -> str:
    """Try to normalise date string to YYYY-MM-DD."""
    MONTHS = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    d_str = d_str.strip().lower()
    m = re.search(r'(\d{1,2})[- /]\s*([a-z]+)[a-z]*[- ,/\s]*(\d{4})', d_str)
    if m:
        day, mon, yr = m.group(1), m.group(2)[:3], m.group(3)
        month_num = MONTHS.get(mon, "01")
        return f"{yr}-{month_num}-{int(day):02d}"
    return d_str


SEBI_AJAX_URL = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"


def _extract_sebi_form_fields(html: str) -> dict:
    """Extract hidden form fields needed to POST to SEBI for pagination."""
    fields = {}
    for m in re.finditer(r'<input[^>]+type="hidden"[^>]*name="([^"]*)"[^>]*value="([^"]*)"', html, re.IGNORECASE):
        fields[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input[^>]+name="([^"]*)"[^>]*type="hidden"[^>]*value="([^"]*)"', html, re.IGNORECASE):
        fields[m.group(1)] = m.group(2)
    return fields


async def fetch_sebi_listings(
    max_pages: int = 5,
    client: httpx.AsyncClient = None,
) -> List[Dict]:
    """
    Scrape SEBI's debt offer document listing pages and return metadata list.

    SEBI uses a Struts form + AJAX pagination:
      - GET first page → receive HTML with first 25 docs
      - POST to /sebiweb/ajax/home/getnewslistinfo.jsp for subsequent pages
        with params: nextValue=<current>, next=n, doDirect=<0-indexed page>,
        sid=3, ssid=17, smid=39, ssidhidden=17, search=, etc.
      - Response is "html1#@#html2" — we parse html1 for document links
    """
    should_close = False
    if client is None:
        client = httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30, follow_redirects=True)
        should_close = True

    all_entries: List[Dict] = []
    seen_urls: set = set()
    next_value = "1"   # tracks current page number on server side

    try:
        # ── Page 1: GET to establish session + first batch ────────────────────
        try:
            resp = await client.get(SEBI_LISTING_URL)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"SEBI page 1 GET failed: {e}")
            return all_entries

        entries = _parse_sebi_listing_html(resp.text)
        new_entries = [e for e in entries if e["doc_url"] not in seen_urls]
        for e in new_entries:
            seen_urls.add(e["doc_url"])
        all_entries.extend(new_entries)
        logger.info(f"SEBI page 1 (GET): found {len(new_entries)} entries")

        # Determine total pages from "1 to 25 of 584 records"
        m_total = re.search(r'of\s+(\d+)\s+records', resp.text, re.IGNORECASE)
        total_records = int(m_total.group(1)) if m_total else 0
        total_pages = (total_records // 25) + (1 if total_records % 25 else 0) if total_records else max_pages
        pages_to_fetch = min(max_pages, total_pages)
        logger.info(f"SEBI: total={total_records} records, {total_pages} pages, will fetch {pages_to_fetch} pages")

        await asyncio.sleep(0.5)

        # ── Pages 2+: AJAX POST ────────────────────────────────────────────────
        ajax_headers = {
            **BROWSER_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": SEBI_LISTING_URL,
            "Origin": SEBI_BASE,
            "X-Requested-With": "XMLHttpRequest",
        }

        for page_num in range(2, pages_to_fetch + 1):
            # doDirect = 0-indexed page offset (page 2 → '1', page 3 → '2', ...)
            do_direct = str(page_num - 1)
            post_data = {
                "nextValue": next_value,
                "next": "n",
                "search": "",
                "fromDate": "",
                "toDate": "",
                "fromYear": "",
                "toYear": "",
                "deptId": "",
                "sid": "3",
                "ssid": "17",
                "smid": "39",
                "ssidhidden": "17",
                "intmid": "-1",
                "sText": "Filings",
                "ssText": "Debt Offer Document",
                "smText": "",
                "doDirect": do_direct,
            }

            try:
                resp = await client.post(SEBI_AJAX_URL, data=post_data, headers=ajax_headers)
                resp.raise_for_status()
            except Exception as e:
                logger.warning(f"SEBI AJAX page {page_num} failed: {e}")
                break

            # Parse response: "html1#@#html2" or just HTML
            text = resp.text
            if "#@#" in text:
                html_part = text.split("#@#")[0]
            else:
                html_part = text

            entries = _parse_sebi_listing_html(html_part)
            new_entries = [e for e in entries if e["doc_url"] not in seen_urls]

            if not new_entries:
                logger.info(f"SEBI AJAX page {page_num}: no new entries, stopping")
                break

            for e in new_entries:
                seen_urls.add(e["doc_url"])
            all_entries.extend(new_entries)
            # Update nextValue from server response (e.g. <input name='nextValue' value='3'/>)
            m_nv = re.search(r"name='nextValue'\s+value='(\d+)'", html_part)
            if m_nv:
                next_value = m_nv.group(1)
            else:
                next_value = str(page_num)
            logger.info(f"SEBI AJAX page {page_num} (doDirect={do_direct}): found {len(new_entries)} new entries, total={len(all_entries)}, nextValue->{next_value}")
            await asyncio.sleep(0.6)  # polite delay between pages
    finally:
        if should_close:
            await client.aclose()

    return all_entries


async def fetch_sebi_pdf_url(doc_url: str, client: httpx.AsyncClient) -> Optional[str]:
    """
    Fetch the SEBI document HTML detail page and extract the embedded PDF URL.
    Returns the first found .pdf URL or None.
    """
    try:
        resp = await client.get(doc_url, timeout=20)
        resp.raise_for_status()
        pdf_urls = _find_pdf_urls_in_html(resp.text, SEBI_BASE)
        # Prefer SEBI's own domain
        sebi_pdfs = [u for u in pdf_urls if "sebi.gov.in" in u]
        if sebi_pdfs:
            return sebi_pdfs[0]
        return pdf_urls[0] if pdf_urls else None
    except Exception as e:
        logger.warning(f"Could not fetch PDF URL from {doc_url}: {e}")
        return None


async def download_pdf(url: str, client: httpx.AsyncClient) -> Optional[bytes]:
    """Download a PDF from a URL and return raw bytes."""
    try:
        resp = await client.get(url, timeout=60, headers={
            **BROWSER_HEADERS,
            "Accept": "application/pdf,application/octet-stream,*/*",
        })
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "pdf" in ct or url.lower().endswith(".pdf"):
            return resp.content
        return None
    except Exception as e:
        logger.warning(f"PDF download failed {url}: {e}")
        return None


# ─── BSE Scraper ──────────────────────────────────────────────────────────────

BSE_MEMO_URL = "https://www.bseindia.com/markets/debt/memorandum_data.aspx"


def _parse_bse_memo_html(html: str) -> List[Dict]:
    """
    Parse BSE memorandum_data.aspx HTML table.
    Columns: Name of the Company | ISIN | Allotment Date | Redemption Date | Download
    Download link: https://www.bseindia.com/corporates/download/XXXXX/filename.zip
    """
    results: List[Dict] = []

    # Each row: <td class="TTRow_left"><a href="..."><b>Company Name</b></a> ... </td><td>ISIN</td>...
    row_pat = re.compile(
        r'<tr[^>]*class="TTRow[^"]*"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE
    )
    for row_m in row_pat.finditer(html):
        row_html = row_m.group(1)
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL | re.IGNORECASE)
        if len(tds) < 2:
            continue

        # Company name + download link from first <td>
        company_m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>.*?<b>([^<]+)</b>', tds[0], re.DOTALL | re.IGNORECASE)
        if not company_m:
            # Fallback: just grab text
            company_txt = re.sub(r'<[^>]+>', '', tds[0]).strip()
            dl_url = ""
        else:
            dl_url = company_m.group(1).strip()
            company_txt = re.sub(r'\s+', ' ', company_m.group(2)).strip()

        # ISIN from 2nd td
        isin = re.sub(r'<[^>]+>', '', tds[1]).strip()
        if not isin.startswith("IN"):
            isin = ""

        # Allotment date from 3rd td
        allot_date = re.sub(r'<[^>]+>', '', tds[2]).strip() if len(tds) > 2 else ""
        filing_date = _normalise_date(allot_date) if allot_date else ""

        # Download link from 5th td (if separate)
        if len(tds) > 4 and not dl_url:
            dl_link_m = re.search(r'href="([^"]+)"', tds[4], re.IGNORECASE)
            if dl_link_m:
                dl_url = dl_link_m.group(1)

        if not company_txt or len(company_txt) < 3:
            continue

        # Normalise download URL
        if dl_url and not dl_url.startswith("http"):
            dl_url = BSE_BASE + "/" + dl_url.lstrip("/")

        results.append({
            "title": company_txt + " – Information Memorandum",
            "doc_url": dl_url or f"https://www.bseindia.com/markets/debt/memorandum_data.aspx?isin={isin}",
            "pdf_url": None,  # BSE provides ZIP files; we won't auto-download
            "isin": isin,
            "issuer_name": company_txt,
            "source": "BSE",
            "doc_type": "IM",
            "filing_date": filing_date,
        })
    return results


async def _bse_post_page(client: httpx.AsyncClient, post_url: str, form_data: dict) -> str:
    """POST to BSE ASP.NET form to get a paginated page."""
    headers = {
        **BROWSER_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": BSE_MEMO_URL,
        "Origin": "https://www.bseindia.com",
    }
    resp = await client.post(post_url, data=form_data, headers=headers, timeout=25)
    resp.raise_for_status()
    return resp.text


async def fetch_bse_listings(client: httpx.AsyncClient, max_pages: int = 10) -> List[Dict]:
    """
    Scrape BSE memorandum_data.aspx HTML table.
    Uses ASP.NET ViewState + __doPostBack pagination.
    """
    all_results: List[Dict] = []
    seen_isins: set = set()

    try:
        # Page 1: GET
        resp = await client.get(BSE_MEMO_URL, headers=BROWSER_HEADERS, timeout=25)
        resp.raise_for_status()
        html = resp.text

        # Extract form fields for pagination
        # Extract page 1 form fields
        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', html)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', html)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', html)
        viewstate = vs.group(1) if vs else ""
        viewstate_gen = vsg.group(1) if vsg else ""
        event_val = ev.group(1) if ev else ""

        entries = _parse_bse_memo_html(html)
        for e in entries:
            k = e.get("isin") or e.get("doc_url", "")
            if k and k not in seen_isins:
                seen_isins.add(k)
                all_results.append(e)

        logger.info(f"BSE page 1: found {len(entries)} entries")

        # Determine total pages from pagination links
        # BSE renders page links as: Page$N&#39; (HTML entity for single quote)
        page_nums = re.findall(r"Page\$(\d+)&#39;", html)
        total_pages = max((int(n) for n in page_nums), default=max_pages)
        pages_to_fetch = min(max_pages, total_pages)
        logger.info(f"BSE: {total_pages} total pages, fetching {pages_to_fetch}")

        # Pages 2+: POST with __doPostBack
        for page_num in range(2, pages_to_fetch + 1):
            form_data = {
                "__VIEWSTATE": viewstate,
                "__VIEWSTATEGENERATOR": viewstate_gen,
                "__VIEWSTATEENCRYPTED": "",
                "__EVENTVALIDATION": event_val,
                "__EVENTTARGET": "ctl00$ContentPlaceHolder1$gvData",
                "__EVENTARGUMENT": f"Page${page_num}",
                "ctl00$ContentPlaceHolder1$txtCompanyName": "",
                "ctl00$ContentPlaceHolder1$txtFromDate": "",
                "ctl00$ContentPlaceHolder1$txtTodate": "",
            }
            try:
                html = await _bse_post_page(client, BSE_MEMO_URL, form_data)
                # Update ViewState for next page
                vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', html)
                vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', html)
                ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', html)
                if vs:
                    viewstate = vs.group(1)
                if vsg:
                    viewstate_gen = vsg.group(1)
                if ev:
                    event_val = ev.group(1)

                entries = _parse_bse_memo_html(html)
                new_entries = []
                for e in entries:
                    k = e.get("isin") or e.get("doc_url", "")
                    if k and k not in seen_isins:
                        seen_isins.add(k)
                        new_entries.append(e)

                if not new_entries:
                    logger.info(f"BSE page {page_num}: no new entries, stopping")
                    break

                all_results.extend(new_entries)
                logger.info(f"BSE page {page_num}: found {len(new_entries)} new entries, total={len(all_results)}")
                await asyncio.sleep(0.8)

            except Exception as e:
                logger.warning(f"BSE page {page_num} failed: {e}")
                break

    except Exception as e:
        logger.warning(f"BSE fetch failed: {e}")

    return all_results


# ─── NSE Scraper ──────────────────────────────────────────────────────────────

NSE_OFFERDOCS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-offer-documents"


async def fetch_nse_listings(client: httpx.AsyncClient) -> List[Dict]:
    """
    Fetch NSE offer documents for debt category.
    NSE uses /api/corporates/offerdocs with cookies from the page.
    """
    results: List[Dict] = []
    try:
        # Step 1: Get the page to get nsit/bm_sz cookies
        page_headers = {
            **BROWSER_HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        await client.get(NSE_OFFERDOCS_PAGE, headers=page_headers, timeout=25)
        await asyncio.sleep(1.5)

        # Step 2: Fetch the debt offer docs API
        api_headers = {
            **BROWSER_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_OFFERDOCS_PAGE,
            "X-Requested-With": "XMLHttpRequest",
        }
        endpoints = [
            "https://www.nseindia.com/api/corporates/offerdocs?variable=CFdebtOfferDocument",
            "https://www.nseindia.com/api/corporates/offerdocs?category=debt",
            "https://www.nseindia.com/api/corporates/offerdocs",
        ]
        for url in endpoints:
            try:
                resp = await client.get(url, headers=api_headers, timeout=25)
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        data = resp.json()
                        # NSE typically: {"data": [...], "filtered": ...} or list
                        rows = (data if isinstance(data, list) else
                                data.get("data", data.get("documents", data.get("Table", []))))
                        if not rows:
                            logger.info(f"NSE endpoint {url}: empty response")
                            continue
                        for row in rows:
                            issuer = (row.get("company") or row.get("issuerName") or
                                      row.get("companyName") or row.get("ISSUER_NAME") or "")
                            isin = (row.get("isin") or row.get("ISIN") or "")
                            doc_url = (row.get("docUrl") or row.get("url") or
                                       row.get("pdfUrl") or row.get("LINK") or "")
                            if doc_url and not doc_url.startswith("http"):
                                doc_url = NSE_BASE + "/" + doc_url.lstrip("/")
                            if not issuer:
                                continue
                            results.append({
                                "title": issuer + " – Offer Document",
                                "doc_url": doc_url or f"https://www.nseindia.com/companies-listing/corporate-filings-offer-documents",
                                "pdf_url": doc_url if doc_url and doc_url.lower().endswith(".pdf") else None,
                                "isin": isin,
                                "issuer_name": issuer,
                                "source": "NSE",
                                "doc_type": _detect_doc_type(issuer),
                                "filing_date": (row.get("filingDate") or row.get("date") or ""),
                            })
                        logger.info(f"NSE API {url}: found {len(results)} entries")
                        if results:
                            break
                    else:
                        logger.info(f"NSE endpoint {url}: non-JSON response ({ct})")
                else:
                    logger.warning(f"NSE endpoint {url}: status {resp.status_code}")
            except Exception as e:
                logger.warning(f"NSE endpoint {url} failed: {e}")
    except Exception as e:
        logger.warning(f"NSE session init failed: {e}")
    return results


# ─── Full Pipeline ─────────────────────────────────────────────────────────────

async def process_document(meta: dict, client: httpx.AsyncClient, extract_pdf: bool = True) -> dict:
    """
    Full pipeline for one document:
      1. Upsert metadata into im_documents
      2. Optionally find + download PDF and extract text
      3. Update status in DB
    Returns updated meta dict with 'id' and 'extraction_status'.
    """
    # Normalise meta
    meta.setdefault("source", "SEBI")
    meta.setdefault("doc_type", _detect_doc_type(meta.get("title", "")))
    meta.setdefault("doc_title", meta.pop("title", "Untitled"))
    if not meta.get("issuer_name"):
        meta["issuer_name"] = _clean_issuer_name(meta["doc_title"])

    doc_id = await _upsert_document(meta)
    if not doc_id:
        return {**meta, "error": "DB upsert failed"}

    if not extract_pdf:
        return {**meta, "id": doc_id, "extraction_status": "PENDING"}

    # Already done?
    existing = await fetch_one("SELECT extraction_status FROM im_documents WHERE id = :id", {"id": doc_id})
    if existing and existing.get("extraction_status") == "DONE":
        return {**meta, "id": doc_id, "extraction_status": "DONE"}

    await _update_status(doc_id, "PROCESSING")

    # Determine PDF URL
    pdf_url = meta.get("pdf_url")
    doc_url = meta.get("doc_url", "")

    if not pdf_url and doc_url and doc_url.lower().endswith(".pdf"):
        pdf_url = doc_url
    elif not pdf_url and doc_url and meta.get("source") == "SEBI":
        pdf_url = await fetch_sebi_pdf_url(doc_url, client)
        if pdf_url:
            await execute_query(
                "UPDATE im_documents SET pdf_url = :u WHERE id = :id",
                {"u": pdf_url, "id": doc_id}
            )

    if not pdf_url:
        await _update_status(doc_id, "NO_PDF")
        return {**meta, "id": doc_id, "extraction_status": "NO_PDF"}

    # Download
    pdf_bytes = await download_pdf(pdf_url, client)
    if not pdf_bytes:
        await _update_status(doc_id, "FAILED", error="PDF download returned empty")
        return {**meta, "id": doc_id, "extraction_status": "FAILED"}

    # Extract text
    text = _extract_text_from_pdf_bytes(pdf_bytes)
    if not text.strip():
        await _update_status(doc_id, "FAILED", error="Text extraction returned empty")
        return {**meta, "id": doc_id, "extraction_status": "FAILED"}

    # Find ISIN
    isins = _find_isins(text)
    isin = isins[0] if isins else meta.get("isin") or None

    extra = {
        "full_text": text[:200_000],  # cap at 200k chars to avoid DB bloat
        "text_length": len(text),
        "isin": isin,
    }
    await _update_status(doc_id, "DONE", extra=extra)
    logger.info(f"[id={doc_id}] Done: {meta['doc_title'][:60]} | {len(text)} chars | ISIN={isin}")
    return {**meta, "id": doc_id, "extraction_status": "DONE", "isin": isin, "text_length": len(text)}


async def run_fetch_job(
    source: str = "SEBI",
    max_pages: int = 5,
    max_docs: int = 50,
    extract_pdf: bool = True,
    job_id: str = None,
) -> dict:
    """
    Full background fetch job for one source.
    Updates _fetch_jobs[job_id] with live progress.
    """
    if not job_id:
        job_id = f"{source}_{int(time.time())}"

    _fetch_jobs[job_id] = {
        "job_id": job_id, "source": source, "status": "RUNNING",
        "started_at": utcnow_iso(),
        "total": 0, "done": 0, "failed": 0, "no_pdf": 0,
        "errors": []
    }

    async with httpx.AsyncClient(
        headers=BROWSER_HEADERS, timeout=45, follow_redirects=True
    ) as client:
        try:
            # Fetch listings
            if source == "SEBI":
                listings = await fetch_sebi_listings(max_pages=max_pages, client=client)
            elif source == "BSE":
                listings = await fetch_bse_listings(client, max_pages=max_pages)
            elif source == "NSE":
                listings = await fetch_nse_listings(client)
            else:
                listings = []

            listings = listings[:max_docs]
            _fetch_jobs[job_id]["total"] = len(listings)
            logger.info(f"[{job_id}] Fetched {len(listings)} listings from {source}")

            # Pre-store all as PENDING (fast)
            for meta in listings:
                meta.setdefault("source", source)
                meta.setdefault("doc_title", meta.pop("title", "Untitled"))
                meta.setdefault("issuer_name", _clean_issuer_name(meta["doc_title"]))
                meta.setdefault("doc_type", _detect_doc_type(meta["doc_title"]))
                await _upsert_document(meta)

            if extract_pdf:
                # Process in serial with polite delay
                for meta in listings:
                    try:
                        result = await process_document(meta, client, extract_pdf=True)
                        status = result.get("extraction_status", "FAILED")
                        if status == "DONE":
                            _fetch_jobs[job_id]["done"] += 1
                        elif status == "NO_PDF":
                            _fetch_jobs[job_id]["no_pdf"] += 1
                        else:
                            _fetch_jobs[job_id]["failed"] += 1
                    except Exception as e:
                        _fetch_jobs[job_id]["failed"] += 1
                        _fetch_jobs[job_id]["errors"].append(str(e)[:120])
                        logger.error(f"[{job_id}] Error processing doc: {e}")
                    await asyncio.sleep(1.0)  # 1s between PDF downloads

            _fetch_jobs[job_id]["status"] = "COMPLETED"
            _fetch_jobs[job_id]["completed_at"] = utcnow_iso()

        except Exception as e:
            _fetch_jobs[job_id]["status"] = "FAILED"
            _fetch_jobs[job_id]["error"] = str(e)
            logger.error(f"[{job_id}] Job failed: {e}")

    # Log to DB
    try:
        j = _fetch_jobs[job_id]
        await execute_query(
            """INSERT INTO im_fetch_log
               (fetch_date, source, isins_found, new_records, updated_records, errors, fetched_at)
               VALUES (:fd, :src, :isins_found, :new_records, :updated_records, :errors, :fa)""",
            {
                "fd": date.today().isoformat(),
                "src": source,
                "isins_found": j.get("done", 0),
                "new_records": j.get("done", 0),
                "updated_records": 0,
                "errors": json.dumps(j.get("errors", [])) if j.get("errors") else None,
                "fa": utcnow_iso(),
            }
        )
    except Exception:
        pass

    return _fetch_jobs[job_id]


def get_job_status(job_id: str) -> Optional[dict]:
    return _fetch_jobs.get(job_id)


def list_all_jobs() -> List[dict]:
    return list(_fetch_jobs.values())
