"""
Unified Gmail Ingestion Utility
================================
Full pipeline — direct Gmail API, NO bond_quote_system.GmailMonitorService dependency:
  1. Authenticate with Gmail via OAuth2 (google-api-python-client, already installed)
  2. Poll inbox, download new PDF/XLSX attachments to data_dir
  3. Parse every new file using the gateway's own BrokerQuoteParser
  4. Upsert into broker_daily_quotes  (ON CONFLICT DO UPDATE)
  5. Log each file to broker_ingest_log
  6. Refresh broker profiles

Credentials (searched in order)
--------------------------------
  1. GMAIL_CREDENTIALS_PATH   env var
  2. Private3/bond_quote_system/config/credentials/credentials.json
  3. Any  client_secret*.json  found in Private3/

Token stored at:
  Private3/bond_quote_system/config/credentials/token.json
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── ensure project roots are importable ───────────────────────────────────────
_PRIVATE3 = Path(__file__).resolve().parent.parent.parent   # …/Private3
_PROJECT  = _PRIVATE3.parent                                 # …/investment_j
for _p in (_PROJECT, _PRIVATE3):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ..domain.market_data.parsers import parse_files
from ..domain.market_data.broker_analytics import refresh_broker_profiles
from ..shared.database import execute_query, fetch_all
from ..shared.logger import get_logger

log = get_logger("utils.gmail_ingest")

_SUPPORTED_EXT = {".pdf", ".xlsx", ".xls"}

# ── Gmail / OAuth2 constants ──────────────────────────────────────────────────
_SCOPES      = ["https://www.googleapis.com/auth/gmail.readonly"]
_MAX_EMAILS  = 50
_SUBJECT_FILTERS = [
    "bond quote", "bond quotes", "bond pricing", "fixed income",
    "price sheet", "price list", "bond prices",
    "yield", "ncd", "debenture", "indicative",
    "debt", "securities", "quote sheet", "daily prices",
    "daily quotes", "rate sheet", "market rates",
]
# Max look-back window for Gmail search; avoids scanning entire inbox every cycle
_NEWER_THAN_DAYS = 90  # Increased from 30 to catch older broker emails

# Fallback: if subject filter returns nothing, search for any mail with a
# spreadsheet / PDF attachment (catches brokers using non-standard subjects)
_FALLBACK_EXTENSIONS = ["pdf", "xls", "xlsx", "csv"]

# Downloaded attachments land here: Private3/bond_quote_system/data/{broker}/{date}/
_DATA_DIR   = _PRIVATE3 / "bond_quote_system" / "data"

# Credentials / token
_CREDS_DIR  = _PRIVATE3 / "bond_quote_system" / "config" / "credentials"
_TOKEN_FILE = _CREDS_DIR / "token.json"


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _find_client_secrets() -> Optional[Path]:
    """Locate OAuth2 client-secrets JSON."""
    env_path = os.getenv("GMAIL_CREDENTIALS_PATH", "")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    default = _CREDS_DIR / "credentials.json"
    if default.exists():
        return default

    # Search for the file Google Cloud Console generates
    matches = sorted(glob.glob(str(_PRIVATE3 / "client_secret*.json")))
    if matches:
        return Path(matches[0])

    return None


def _gmail_authenticate():
    """
    Perform OAuth2 auth and return a Gmail API service object.
    Raises RuntimeError with actionable message on failure.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            f"Google API packages not importable: {exc}. "
            "Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        ) from exc

    _CREDS_DIR.mkdir(parents=True, exist_ok=True)
    creds = None

    if _TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
        except Exception as exc:
            log.warning("token_load_failed", error=str(exc))
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("gmail_token_refreshing")
            try:
                creds.refresh(Request())
            except Exception as exc:
                log.warning("token_refresh_failed", error=str(exc))
                creds = None

        if not creds or not creds.valid:
            secrets_file = _find_client_secrets()
            if not secrets_file:
                raise RuntimeError(
                    "OAuth2 credentials file not found. "
                    f"Looked in: {_CREDS_DIR}/credentials.json  and  {_PRIVATE3}/client_secret*.json\n"
                    "Fix: download the client-secrets JSON from "
                    "Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs "
                    f"and save it as  {_CREDS_DIR / 'credentials.json'}"
                )
            # ── Server-mode guard ──────────────────────────────────────────
            # run_local_server() requires a browser and will HANG in a server
            # (uvicorn) context.  Instead, raise a clear error with instructions
            # so the user knows exactly how to re-authenticate.
            log.error(
                "gmail_reauth_required",
                secrets=str(secrets_file),
                token=str(_TOKEN_FILE),
                hint=(
                    "Token is missing or expired. "
                    "Run the reauth helper from a machine with a browser, then restart the gateway."
                ),
            )
            reauth_cmd = (
                f"python -c \""
                f"from google_auth_oauthlib.flow import InstalledAppFlow; "
                f"from pathlib import Path; "
                f"creds = InstalledAppFlow.from_client_secrets_file("
                f"r'{secrets_file}', ['{_SCOPES[0]}']).run_local_server(port=0); "
                f"Path(r'{_TOKEN_FILE}').write_text(creds.to_json()); "
                f"print('Token saved to {_TOKEN_FILE}')\""
            )
            raise RuntimeError(
                "Gmail token is missing or expired and CANNOT be refreshed automatically.\n\n"
                "To fix this, open a terminal on your desktop (where a browser is available) "
                "and run:\n\n"
                f"  {reauth_cmd}\n\n"
                "A browser window will open asking you to authorise the app. "
                "After completing the flow the token will be written to:\n"
                f"  {_TOKEN_FILE}\n\n"
                "Then restart the gateway and Gmail polling will resume automatically."
            )

        # Persist token for future runs
        try:
            with open(str(_TOKEN_FILE), "w") as fh:
                fh.write(creds.to_json())
            log.info("gmail_token_saved", path=str(_TOKEN_FILE))
        except Exception as exc:
            log.warning("token_save_failed", error=str(exc))

    service = build("gmail", "v1", credentials=creds)
    log.info("gmail_authenticated_ok")
    return service


# ── Gmail polling helpers ─────────────────────────────────────────────────────

def _extract_broker_from_sender(sender: str) -> str:
    """Map sender email domain to a human-readable broker name."""
    email_m = re.search(r"@([\w.-]+)", sender or "")
    if not email_m:
        return "Unknown"
    domain = email_m.group(1).lower()
    domain_map = {
        "nuvamawealth": "Nuvama",
        "edelweiss":    "Edelweiss",
        "darashaw":     "Darashaw",
        "birlasl":      "Birla",
        "motilal":      "MotilalOswal",
        "hdfcsec":      "HDFC",
        "icicisec":     "ICICI",
        "sbicap":       "SBI",
        "axissec":      "Axis",
        "kotak":        "Kotak",
        "jmfl":         "JM",
        "trustmf":      "TrustMF",
        "trustplutus":  "TrustPlutus",
        "invesco":      "Invesco",
        "nj":           "NJ",
    }
    for key, name in domain_map.items():
        if key in domain:
            return name
    return domain.split(".")[0].title()


def _download_attachment(
    service,
    message_id: str,
    part: dict,
    broker: str,
    today_str: str,
) -> Optional[Path]:
    """Download one attachment, return local Path or None if skipped."""
    filename = (part.get("filename") or "attachment").strip()
    if not filename:
        return None
    ext = Path(filename).suffix.lower()
    if ext not in _SUPPORTED_EXT:
        return None

    att_id   = (part.get("body") or {}).get("attachmentId")
    data_b64 = ""

    if att_id:
        try:
            att      = service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=att_id
            ).execute()
            data_b64 = att.get("data", "")
        except Exception as exc:
            log.warning("attachment_fetch_failed", msg_id=message_id, error=str(exc))
            return None
    else:
        data_b64 = (part.get("body") or {}).get("data", "")

    if not data_b64:
        return None

    try:
        data = base64.urlsafe_b64decode(data_b64 + "==")
    except Exception as exc:
        log.warning("base64_decode_failed", filename=filename, error=str(exc))
        return None

    save_dir = _DATA_DIR / broker / today_str
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / filename

    try:
        save_path.write_bytes(data)
        log.info("attachment_saved", path=str(save_path), bytes=len(data))
        return save_path
    except Exception as exc:
        log.warning("attachment_write_failed", path=str(save_path), error=str(exc))
        return None


def _walk_parts(parts_list: list) -> list:
    """Recursively collect MIME parts that have an attachmentId or inline data."""
    found = []
    for p in (parts_list or []):
        body = p.get("body") or {}
        if p.get("filename") and (body.get("attachmentId") or body.get("data")):
            found.append(p)
        if p.get("parts"):
            found.extend(_walk_parts(p["parts"]))
    return found


def _poll_gmail_sync(service) -> tuple[int, int, List[Path]]:
    """
    Synchronous Gmail poll (runs in a thread pool).
    Returns (emails_processed, attachments_downloaded, saved_file_paths).
    """
    today_str = date.today().isoformat()
    subject_clause = " OR ".join(f'subject:"{kw}"' for kw in _SUBJECT_FILTERS)
    query = f"has:attachment newer_than:{_NEWER_THAN_DAYS}d ({subject_clause})"

    log.info("gmail_query", query=query)

    try:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=_MAX_EMAILS
        ).execute()
    except Exception as exc:
        log.warning("gmail_list_failed", error=str(exc))
        return 0, 0, []

    messages = resp.get("messages") or []
    if not messages:
        # Primary subject-filter returned nothing — try broader attachment search
        ext_clause = " OR ".join(f'filename:{e}' for e in _FALLBACK_EXTENSIONS)
        fallback_query = f"has:attachment newer_than:{_NEWER_THAN_DAYS}d ({ext_clause})"
        log.info("gmail_fallback_query", query=fallback_query)
        try:
            resp2 = service.users().messages().list(
                userId="me", q=fallback_query, maxResults=_MAX_EMAILS
            ).execute()
            messages = resp2.get("messages") or []
        except Exception as exc:
            log.warning("gmail_fallback_failed", error=str(exc))
        if not messages:
            log.info("gmail_no_matching_messages",
                     hint="Check _SUBJECT_FILTERS vs actual broker email subjects")
            return 0, 0, []

    emails_proc  = 0
    attachs_dl   = 0
    saved_files: List[Path] = []

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
        except Exception as exc:
            log.warning("gmail_get_msg_failed", msg_id=msg_id, error=str(exc))
            continue

        headers  = {h["name"]: h["value"]
                    for h in (msg.get("payload") or {}).get("headers") or []}
        sender   = headers.get("From", "")
        broker   = _extract_broker_from_sender(sender)

        parts    = _walk_parts((msg.get("payload") or {}).get("parts") or [])
        emails_proc += 1

        for part in parts:
            saved = _download_attachment(service, msg_id, part, broker, today_str)
            if saved:
                attachs_dl += 1
                saved_files.append(saved)

    log.info("gmail_poll_complete",
             emails=emails_proc, attachments=attachs_dl, files=len(saved_files))
    return emails_proc, attachs_dl, saved_files


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_gmail_ingest_cycle() -> Dict[str, Any]:
    """
    Full Gmail → parse → store cycle.

    Returns:
        emails_processed, attachments_downloaded,
        files_parsed, quotes_extracted, quotes_stored,
        isins (up to 20), errors (up to 20)
    """
    errors: List[str] = []

    # ─ 0. Token pre-flight check — log status before attempting anything ──────
    if not _TOKEN_FILE.exists():
        log.warning(
            "gmail_token_missing",
            path=str(_TOKEN_FILE),
            hint="No token.json — run the reauth command shown in the RuntimeError below",
        )
    else:
        try:
            from google.oauth2.credentials import Credentials as _Creds
            _tc = _Creds.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
            if _tc.valid:
                log.info("gmail_token_status", status="valid", expires=str(getattr(_tc, 'expiry', 'unknown')))
            elif _tc.expired and _tc.refresh_token:
                log.info("gmail_token_status", status="expired_refreshable", expires=str(getattr(_tc, 'expiry', 'unknown')))
            else:
                log.warning(
                    "gmail_token_status",
                    status="expired_no_refresh_token",
                    hint="Re-authentication required — see error message below",
                )
        except Exception as _te:
            log.warning("gmail_token_precheck_failed", error=str(_te))

    # ─ 1. Authenticate ────────────────────────────────────────────────────────
    try:
        service = _gmail_authenticate()
    except RuntimeError as exc:
        return {
            "error":                  str(exc),
            "emails_processed":       0,
            "attachments_downloaded": 0,
            "files_parsed":           0,
            "quotes_extracted":       0,
            "quotes_stored":          0,
            "isins":                  [],
            "errors":                 [str(exc)],
        }

    # ─ 2. Poll Gmail in a thread (avoid blocking the async event loop) ────────
    try:
        emails_processed, attachments_downloaded, _ = \
            await asyncio.to_thread(_poll_gmail_sync, service)
    except Exception as exc:
        err = f"Gmail poll exception: {exc}"
        log.warning("poll_exception", error=str(exc))
        return {
            "error":                  err,
            "emails_processed":       0,
            "attachments_downloaded": 0,
            "files_parsed":           0,
            "quotes_extracted":       0,
            "quotes_stored":          0,
            "isins":                  [],
            "errors":                 [err],
        }

    # ─ 3. Find all unprocessed files in data_dir (includes previous misses) ──
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    candidate_files = [
        p for p in _DATA_DIR.rglob("*")
        if p.suffix.lower() in _SUPPORTED_EXT and p.is_file()
    ]

    already_done: set = set()
    try:
        # Build a set of "broker/date/filename" relative keys so the same filename
        # from a different broker or date is not incorrectly de-duped.
        # Also include bare filenames for backward-compat with older log rows.
        rows = await fetch_all(
            "SELECT broker_name, quote_date, filename FROM broker_ingest_log", {}
        )
        for r in (rows or []):
            already_done.add(r["filename"])  # bare name (legacy)
            already_done.add(
                f"{r['broker_name']}/{r['quote_date']}/{r['filename']}"
            )  # full relative key
    except Exception as exc:
        log.warning("ingest_log_read_fail", error=str(exc))

    def _file_key(p: Path) -> str:
        """Return broker/date/filename relative key, falling back to bare name."""
        try:
            parts = p.relative_to(_DATA_DIR).parts  # (broker, date, filename)
            return "/".join(parts)
        except ValueError:
            return p.name

    new_files = [p for p in candidate_files if _file_key(p) not in already_done]

    if not new_files:
        log.info("no_new_files_to_parse")
        return {
            "emails_processed":       emails_processed,
            "attachments_downloaded": attachments_downloaded,
            "files_parsed":           0,
            "quotes_extracted":       0,
            "quotes_stored":          0,
            "isins":                  [],
            "errors":                 errors,
            "message":                "No new files to parse — all attachments already ingested.",
        }

    log.info("parsing_new_files", count=len(new_files))

    # ─ 4. Parse with BrokerQuoteParser ───────────────────────────────────────
    parse_results = parse_files(new_files)

    # ─ 5. Upsert into broker_daily_quotes ────────────────────────────────────
    total_extracted = 0
    total_stored    = 0
    all_isins: List[str] = []

    for result in parse_results:
        if result.errors and not result.quotes:
            errors.append(f"{result.filename}: {result.errors[0]}")
            log.warning("parse_failed", file=result.filename, errors=result.errors)

        stored_this_file = 0
        file_errors: List[str] = []

        for q in result.quotes:
            if not q.isin:
                continue
            isin_clean = q.isin.strip().upper()
            try:
                await execute_query(
                    """
                    INSERT INTO broker_daily_quotes
                        (isin, broker_name, quote_date, bond_name, category,
                         coupon_rate, maturity_date, rating, rating_agency,
                         clean_price, ytm, ytm_semi, ytm_screen,
                         bid_price, ask_price, bid_yield, ask_yield,
                         lot_size, quantity_lacs, put_call_date, ip_dates,
                         settlement_days, source_file, source_format)
                    VALUES
                        (:isin, :broker, :date, :bond_name, :category,
                         :coupon, :maturity, :rating, :rating_agency,
                         :price, :ytm, :ytm_semi, :ytm_screen,
                         :bid_price, :ask_price, :bid_yield, :ask_yield,
                         :lot, :qty_lacs, :put_call, :ip_dates,
                         :settle, :source_file, :source_format)
                    ON CONFLICT(isin, broker_name, quote_date) DO UPDATE SET
                        bond_name     = excluded.bond_name,
                        category      = excluded.category,
                        coupon_rate   = excluded.coupon_rate,
                        maturity_date = excluded.maturity_date,
                        rating        = excluded.rating,
                        rating_agency = excluded.rating_agency,
                        clean_price   = COALESCE(excluded.clean_price,   broker_daily_quotes.clean_price),
                        ytm           = COALESCE(excluded.ytm,            broker_daily_quotes.ytm),
                        ytm_semi      = COALESCE(excluded.ytm_semi,       broker_daily_quotes.ytm_semi),
                        ytm_screen    = COALESCE(excluded.ytm_screen,     broker_daily_quotes.ytm_screen),
                        bid_price     = COALESCE(excluded.bid_price,      broker_daily_quotes.bid_price),
                        ask_price     = COALESCE(excluded.ask_price,      broker_daily_quotes.ask_price),
                        bid_yield     = COALESCE(excluded.bid_yield,      broker_daily_quotes.bid_yield),
                        ask_yield     = COALESCE(excluded.ask_yield,      broker_daily_quotes.ask_yield),
                        lot_size      = COALESCE(excluded.lot_size,       broker_daily_quotes.lot_size),
                        quantity_lacs = COALESCE(excluded.quantity_lacs,  broker_daily_quotes.quantity_lacs),
                        source_file   = excluded.source_file,
                        extracted_at  = datetime('now')
                    """,
                    {
                        "isin":          isin_clean,
                        "broker":        q.broker_name,
                        "date":          q.quote_date,
                        "bond_name":     q.bond_name,
                        "category":      q.category,
                        "coupon":        q.coupon_rate,
                        "maturity":      q.maturity_date,
                        "rating":        q.rating,
                        "rating_agency": q.rating_agency,
                        "price":         q.clean_price,
                        "ytm":           q.ytm,
                        "ytm_semi":      q.ytm_semi,
                        "ytm_screen":    q.ytm_screen,
                        "bid_price":     q.bid_price,
                        "ask_price":     q.ask_price,
                        "bid_yield":     q.bid_yield,
                        "ask_yield":     q.ask_yield,
                        "lot":           q.lot_size,
                        "qty_lacs":      q.quantity_lacs,
                        "put_call":      q.put_call_date,
                        "ip_dates":      q.ip_dates,
                        "settle":        q.settlement_days,
                        "source_file":   q.source_file or result.filename,
                        "source_format": q.source_format or result.source_format,
                    },
                )
                stored_this_file += 1
                all_isins.append(isin_clean)
            except Exception as db_exc:
                file_errors.append(f"{q.isin}: {db_exc}")
                log.warning("db_upsert_fail", isin=q.isin, error=str(db_exc))

        total_extracted += len(result.quotes)
        total_stored    += stored_this_file

        # ─ 6. Record in broker_ingest_log ─────────────────────────────────────
        try:
            await execute_query(
                """
                INSERT INTO broker_ingest_log
                    (filename, broker_name, quote_date, source_format,
                     quotes_parsed, quotes_inserted, quotes_updated, errors_json)
                VALUES
                    (:filename, :broker, :date, :fmt,
                     :parsed, :inserted, 0, :errors)
                """,
                {
                    "filename": result.filename,
                    "broker":   result.broker_name,
                    "date":     result.quote_date,
                    "fmt":      result.source_format,
                    "parsed":   len(result.quotes),
                    "inserted": stored_this_file,
                    "errors":   json.dumps((result.errors + file_errors)[:20])
                                if (result.errors or file_errors) else None,
                },
            )
        except Exception as log_exc:
            log.warning("ingest_log_fail", file=result.filename, error=str(log_exc))

    # ─ 7. Refresh broker profiles ─────────────────────────────────────────────
    try:
        await asyncio.to_thread(refresh_broker_profiles)
    except Exception as exc:
        log.warning("profile_refresh_fail", error=str(exc))

    log.info(
        "gmail_ingest_complete",
        emails=emails_processed,
        files=len(new_files),
        quotes_extracted=total_extracted,
        quotes_stored=total_stored,
    )

    return {
        "emails_processed":       emails_processed,
        "attachments_downloaded": attachments_downloaded,
        "files_parsed":           len(new_files),
        "quotes_extracted":       total_extracted,
        "quotes_stored":          total_stored,
        "isins":                  list(set(all_isins))[:20],
        "errors":                 errors[:20],
    }
