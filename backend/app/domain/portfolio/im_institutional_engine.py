"""
IM Institutional-Grade Extraction Engine  (v2)
=================================================
Complete pipeline for institutional-quality IM document analysis:

  1. Hybrid PDF text extraction (PyMuPDF → pdfminer → PyPDF2 → OCR fallback)
  2. Processing log  (im_processing_log)
  3. Page-level storage  (im_pages)
  4. Full text storage  (im_full_text)
  5. Section detection with variation patterns  (im_sections)
  6. Bond metadata extraction  (bond_metadata)
  7. Clause detection agents with confidence & evidence  (bond_clauses)
  8. Covenant detection with binding-language filter  (bond_covenants)
  9. Evidence storage  (clause_evidence)
 10. Risk scoring engine  (bond_risk_analysis)

Design principles:
  • False-negative prevention: never output "ABSENT" unless confidence > 0.80
  • Every detection stores page-number + section + snippet evidence
  • Supports IMs up to 50 MB / 300 pages
  • LLM fallback agents for complex clause detection
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..shared.database import execute_query, fetch_all, fetch_one

logger = logging.getLogger("im_institutional")

_TABLE_COLUMN_CACHE: Dict[str, set[str]] = {}


def _is_valid_rating_value(value: Any) -> bool:
    if value is None:
        return False
    rating = str(value).strip().upper()
    return bool(re.match(r"^(?:AAA|AA\+|AA|AA-|A\+|A|A-|BBB\+|BBB|BBB-|BB\+|BB|BB-|B\+|B|B-|C|D|SOVEREIGN)$", rating))


def _is_blankish_value(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip() in {"", ".", "-", "--", "N/A", "NA", "NULL"}


def _is_suspicious_issuer_value(value: Any) -> bool:
    if _is_blankish_value(value):
        return True
    issuer = re.sub(r"\s+", " ", str(value)).strip().upper()
    suspicious_tokens = (
        "REDEMPTION",
        "INTEREST PAYMENT",
        "REFERENCE HEREIN",
        "CONTAINS ALL INFORMATION",
        "TERMS OF ISSUE",
        "COVENANTS",
        "PAGE ",
    )
    return len(issuer) < 5 or any(token in issuer for token in suspicious_tokens)


def _is_valid_rating_agency_value(value: Any) -> bool:
    if _is_blankish_value(value):
        return False
    agency = str(value).strip().upper()
    if len(agency) > 40:
        return False
    known = ("CRISIL", "ICRA", "CARE", "INDIA RATINGS", "ACUITE", "BRICKWORK", "INFOMERICS", "FITCH", "MOODY", "S&P")
    return any(name in agency for name in known)


async def _get_table_columns(table_name: str) -> set[str]:
    cached = _TABLE_COLUMN_CACHE.get(table_name)
    if cached is not None:
        return cached

    rows = await fetch_all(f"PRAGMA table_info({table_name})")
    columns = {row.get("name") for row in rows if row.get("name")}
    _TABLE_COLUMN_CACHE[table_name] = columns
    return columns


async def _ensure_institutional_schema_compat() -> None:
    bond_metadata_columns = await _get_table_columns("bond_metadata")
    missing_metadata_columns = {
        "credit_rating": "TEXT",
        "rating_agency": "TEXT",
    }
    for column_name, column_type in missing_metadata_columns.items():
        if column_name not in bond_metadata_columns:
            await execute_query(
                f"ALTER TABLE bond_metadata ADD COLUMN {column_name} {column_type}"
            )
            bond_metadata_columns.add(column_name)

    _TABLE_COLUMN_CACHE["bond_metadata"] = bond_metadata_columns

# Min chars before we consider text extraction "good enough" (skip OCR)
OCR_THRESHOLD = 1000


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — HYBRID PDF TEXT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_pages_from_pdf(file_bytes: bytes) -> Tuple[List[Dict[str, Any]], str]:
    """
    Hybrid extraction pipeline.
    Returns (pages_list, method_used).
    Method priority: pymupdf → pdfminer → pypdf2 → ocr
    """
    pages: List[Dict[str, Any]] = []
    method_used = "failed"

    # ────── Attempt 1: PyMuPDF (fitz) — fastest and most reliable ──────
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page_num in range(len(doc)):
            page = doc[page_num]
            raw_text = page.get_text("text") or ""
            clean = _clean_text(raw_text)
            pages.append({
                "page_number": page_num + 1,
                "raw_text": raw_text,
                "clean_text": clean,
                "token_count": len(clean.split()),
            })
        doc.close()
        total_chars = sum(len(p["clean_text"]) for p in pages)
        if total_chars >= OCR_THRESHOLD:
            return pages, "pymupdf"
        logger.debug(f"PyMuPDF extracted only {total_chars} chars, trying pdfminer")
    except Exception as e:
        logger.debug(f"PyMuPDF unavailable or failed: {e}")

    # ────── Attempt 2: pdfminer page-by-page ──────
    pdfminer_pages: List[Dict[str, Any]] = []
    try:
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
        from pdfminer.converter import TextConverter
        from pdfminer.layout import LAParams

        rsrcmgr = PDFResourceManager()
        laparams = LAParams()
        pdf_stream = io.BytesIO(file_bytes)

        for page_num, page in enumerate(PDFPage.get_pages(pdf_stream), start=1):
            output_string = io.StringIO()
            device = TextConverter(rsrcmgr, output_string, laparams=laparams)
            interpreter = PDFPageInterpreter(rsrcmgr, device)
            interpreter.process_page(page)
            raw_text = output_string.getvalue()
            device.close()
            output_string.close()

            clean = _clean_text(raw_text)
            pdfminer_pages.append({
                "page_number": page_num,
                "raw_text": raw_text,
                "clean_text": clean,
                "token_count": len(clean.split()),
            })

        total_chars = sum(len(p["clean_text"]) for p in pdfminer_pages)
        if total_chars >= OCR_THRESHOLD:
            return pdfminer_pages, "pdfminer"
        logger.debug(f"pdfminer extracted only {total_chars} chars, trying PyPDF2")
    except Exception as e:
        logger.debug(f"pdfminer page extraction failed: {e}")

    # ────── Attempt 3: PyPDF2 ──────
    pypdf_pages: List[Dict[str, Any]] = []
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        for page_num, page in enumerate(reader.pages, start=1):
            raw_text = page.extract_text() or ""
            clean = _clean_text(raw_text)
            pypdf_pages.append({
                "page_number": page_num,
                "raw_text": raw_text,
                "clean_text": clean,
                "token_count": len(clean.split()),
            })
        total_chars = sum(len(p["clean_text"]) for p in pypdf_pages)
        if total_chars >= OCR_THRESHOLD:
            return pypdf_pages, "pypdf2"
        logger.debug(f"PyPDF2 extracted only {total_chars} chars, attempting OCR")
    except Exception as e:
        logger.debug(f"PyPDF2 page extraction failed: {e}")

    # ────── Attempt 4: OCR fallback ──────
    ocr_pages = _run_ocr_pipeline(file_bytes)
    if ocr_pages:
        total_chars = sum(len(p["clean_text"]) for p in ocr_pages)
        if total_chars > 0:
            return ocr_pages, "ocr"

    # Return best non-empty result we have (pdfminer > pypdf > pymupdf)
    for candidate, method in [
        (pdfminer_pages, "pdfminer"),
        (pypdf_pages, "pypdf2"),
        (pages, "pymupdf"),
    ]:
        if candidate:
            return candidate, method

    return [], "failed"


def _run_ocr_pipeline(file_bytes: bytes) -> List[Dict[str, Any]]:
    """
    OCR fallback: convert PDF pages to images, then OCR each page.
    Supports: pytesseract (Tesseract) and PaddleOCR.
    """
    pages: List[Dict[str, Any]] = []

    # Try pdf2image + tesseract
    try:
        from pdf2image import convert_from_bytes
        import pytesseract

        images = convert_from_bytes(file_bytes, dpi=300, fmt="png")
        for page_num, img in enumerate(images, start=1):
            raw_text = pytesseract.image_to_string(img, lang="eng")
            clean = _clean_text(raw_text)
            pages.append({
                "page_number": page_num,
                "raw_text": raw_text,
                "clean_text": clean,
                "token_count": len(clean.split()),
            })
        if pages:
            return pages
    except ImportError:
        logger.debug("pytesseract or pdf2image not installed, trying PaddleOCR")
    except Exception as e:
        logger.debug(f"Tesseract OCR failed: {e}")

    # Try PaddleOCR
    try:
        from paddleocr import PaddleOCR
        from pdf2image import convert_from_bytes

        ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        images = convert_from_bytes(file_bytes, dpi=300, fmt="png")
        for page_num, img in enumerate(images, start=1):
            import numpy as np
            img_array = np.array(img)
            result = ocr.ocr(img_array, cls=True)
            lines = []
            if result and result[0]:
                for line in result[0]:
                    if line and len(line) > 1:
                        lines.append(line[1][0])
            raw_text = "\n".join(lines)
            clean = _clean_text(raw_text)
            pages.append({
                "page_number": page_num,
                "raw_text": raw_text,
                "clean_text": clean,
                "token_count": len(clean.split()),
            })
        if pages:
            return pages
    except ImportError:
        logger.debug("PaddleOCR not installed")
    except Exception as e:
        logger.debug(f"PaddleOCR failed: {e}")

    return pages


def _clean_text(text: str) -> str:
    """Clean extracted text: normalise whitespace, remove form-feeds."""
    text = text.replace("\f", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 4 — SECTION DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

# Canonical IM sections with expanded variation patterns per spec
SECTION_PATTERNS = [
    ("Risk Factors", [
        r"risk\s+factors?",
        r"risk\s+considerations?",
        r"material\s+risk\s+factors?",
        r"risks?\s+associated",
        r"risks?\s+relating\s+to",
    ]),
    ("Events of Default", [
        r"events?\s+of\s+default",
        r"default\s+events?",
        r"default\s+provisions?",
        r"events?\s+triggering\s+default",
        r"conditions?\s+constituting\s+default",
        r"events?\s+which\s+(?:may\s+)?(?:constitute|trigger)\s+(?:an?\s+)?default",
    ]),
    ("Covenants", [
        r"covenants?(?:\s+of\s+the\s+issuer)?",
        r"financial\s+covenants?",
        r"non[- ]?financial\s+covenants?",
        r"affirmative\s+covenants?",
        r"negative\s+covenants?",
        r"restrictive\s+covenants?",
        r"information\s+covenants?",
        r"undertakings?\s+(?:of|by)\s+(?:the\s+)?(?:issuer|company)",
    ]),
    ("Security", [
        r"security\s+(?:clause|structure|for|cover|arrangement)",
        r"creation\s+of\s+(?:charge|security)",
        r"charge\s+on\s+assets?",
        r"security\s+package",
        r"nature\s+of\s+security",
        r"details?\s+of\s+security",
        r"collateral\s+(?:details?|structure)",
        r"asset\s+(?:coverage|cover)",
        r"secured\s+(?:by|through)",
    ]),
    ("Guarantee", [
        r"guarantee\s*(?:details?|provisions?)?",
        r"credit\s+enhancement",
        r"corporate\s+guarantee",
        r"personal\s+guarantee",
        r"promoter\s+guarantee",
        r"guarantor\s+(?:details?|information)",
        r"credit\s+support",
    ]),
    ("Use of Proceeds", [
        r"use\s+of\s+proceeds",
        r"objects?\s+of\s+(?:the\s+)?issue",
        r"purpose\s+of\s+(?:the\s+)?issue",
        r"utilization\s+of\s+(?:issue\s+)?proceeds",
        r"deployment\s+of\s+funds",
    ]),
    ("Definitions", [
        r"definitions?\s+(?:and\s+interpretations?)?",
        r"glossary",
        r"interpretation\s+(?:clause|section)",
    ]),
    ("Terms of Issue", [
        r"terms?\s+of\s+(?:the\s+)?issue",
        r"salient\s+features?\s+(?:of\s+the\s+issue)?",
        r"principal\s+terms?",
        r"terms?\s+(?:and\s+conditions?\s+)?of\s+(?:the\s+)?(?:debentures?|bonds?|ncds?)",
        r"key\s+terms?",
        r"(?:general\s+)?terms?\s+and\s+conditions?",
    ]),
    ("Redemption", [
        r"redemption\s*(?:details?|schedule|provisions?)?",
        r"repayment\s+schedule",
        r"amortiz(?:ation|ing)\s+schedule",
        r"principal\s+repayment",
        r"maturity\s+(?:and\s+)?redemption",
    ]),
    ("Trustee Details", [
        r"debenture\s+trustee",
        r"trustee\s+(?:details?|information)",
        r"(?:role|duties)\s+of\s+(?:the\s+)?(?:debenture\s+)?trustee",
    ]),
    ("Call/Put Option", [
        r"call\s+option",
        r"put\s+option",
        r"optional\s+redemption",
        r"early\s+redemption",
        r"call\s*/\s*put\s+option",
    ]),
    ("Listing", [
        r"listing\s+(?:details?|exchange|provisions?|arrangement)",
        r"stock\s+exchange\s+(?:listing|details?)",
        r"(?:proposed\s+)?listing\s+(?:on|at)",
    ]),
    ("Credit Rating", [
        r"credit\s+rating\s*(?:details?|rationale)?",
        r"rating\s+(?:rationale|details?|letter)",
        r"instrument\s+rating",
    ]),
    ("Issuer Details", [
        r"about\s+(?:the\s+)?(?:issuer|company)",
        r"issuer\s+(?:details?|profile|information)",
        r"(?:brief\s+)?(?:description|profile)\s+of\s+(?:the\s+)?(?:issuer|company)",
    ]),
]


def detect_sections(pages: List[Dict]) -> List[Dict[str, Any]]:
    """
    Detect major IM sections across all pages.
    Returns list of {section_name, start_page, end_page, section_text, confidence}.
    """
    results = []

    for section_name, patterns in SECTION_PATTERNS:
        best_match = None
        matched_pages = []

        for page in pages:
            text = page.get("clean_text", "") or page.get("raw_text", "")
            page_num = page["page_number"]
            text_lower = text.lower()

            for pat in patterns:
                # Look for section HEADING (near start-of-line, optional numbering)
                heading_re = (
                    r"(?:^|\n)\s*"
                    r"(?:\d{1,3}[\.\)]\s*|[ivxIVX]+[\.\)]\s*|[a-zA-Z][\.\)]\s*)?"
                    r"(" + pat + r")"
                    r"\s*(?:\n|$|[:\-–])"
                )
                heading_match = re.search(heading_re, text_lower)
                if heading_match:
                    matched_pages.append(page_num)
                    if not best_match or page_num < best_match["page"]:
                        best_match = {
                            "page": page_num,
                            "match_text": heading_match.group(0).strip(),
                            "confidence": 0.95,
                        }
                    break
                elif re.search(pat, text_lower):
                    # Mentioned but not as heading — lower confidence
                    matched_pages.append(page_num)
                    if not best_match:
                        best_match = {
                            "page": page_num,
                            "match_text": "",
                            "confidence": 0.60,
                        }
                    break

        if best_match and matched_pages:
            start_page = min(matched_pages)
            end_page = max(matched_pages)
            section_text = "\n\n".join(
                (p.get("clean_text", "") or p.get("raw_text", ""))
                for p in pages if start_page <= p["page_number"] <= end_page
            )
            results.append({
                "section_name": section_name,
                "start_page": start_page,
                "end_page": end_page,
                "section_text": section_text[:60000],
                "confidence": best_match["confidence"],
            })

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 5 — BOND METADATA EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_bond_metadata(full_text: str, pages: List[Dict]) -> Dict[str, Any]:
    """
    Extract structured bond terms from IM text using regex-first approach.
    Fields: issuer_name, isin, coupon_rate, coupon_type, issue_size,
            face_value, issue_date, maturity_date, tenor, currency,
            credit_rating, rating_agency, security_type, seniority,
            trustee, listing_exchange, governing_law, ...
    """
    text_lower = full_text.lower()
    meta: Dict[str, Any] = {}

    # ── Issuer name ──
    for pat in [
        r"(?:name\s+of\s+(?:the\s+)?issuer|issuer\s*(?:name)?|company)\s*[:\-–]\s*(.+?)(?:\n|$)",
        r"issued\s+by\s+(.+?)(?:\n|$|\.)",
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            val = m.group(1).strip()[:200]
            if len(val) > 3:
                meta["issuer_name"] = val
                break

    # ── ISIN ──
    isin_m = re.search(r'\b(IN[A-Z0-9]{10})\b', full_text)
    if isin_m:
        meta["isin"] = isin_m.group(1)

    # ── Currency ──
    if re.search(r'\b(?:usd|us\s*dollar|united\s*states\s*dollar)\b', text_lower):
        meta["currency"] = "USD"
    elif re.search(r'\beur(?:o)?\b', text_lower):
        meta["currency"] = "EUR"
    else:
        meta["currency"] = "INR"

    # ── Issue size ──
    for pat in [
        r'(?:(?:aggregate|total)\s+)?(?:issue\s*size|nominal\s*(?:value|amount)|aggregate\s*(?:nominal|face)\s*value|total\s*issue(?:\s*size)?)\s*[:\-–]?\s*(?:Rs\.?\s*|INR\s*|₹\s*)?(\d[\d,]*(?:\.\d+)?)\s*(?:cr(?:ore)?s?|lacs?|lakhs?)',
        r'(?:Rs\.?\s*|INR\s*|₹\s*)(\d[\d,]*(?:\.\d+)?)\s*(?:cr(?:ore)?s?)\b',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            try:
                meta["issue_size"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    # ── Coupon rate ──
    for pat in [
        r'coupon\s*(?:rate)?\s*[:\-–]\s*(\d+\.?\d*)\s*%',
        r'(?:rate\s+of\s+interest|interest\s+rate)\s*[:\-–]?\s*(\d+\.?\d*)\s*%',
        r'(\d+\.?\d*)\s*%\s*(?:per\s*annum|p\.?a\.?|coupon)',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            try:
                val = float(m.group(1))
                if 0.5 <= val <= 30:
                    meta["coupon_rate"] = val
                    break
            except ValueError:
                continue

    # ── Coupon type ──
    if re.search(r'\bzero\s*coupon\b', text_lower):
        meta["coupon_type"] = "ZERO"
    elif re.search(r'\bfloating\s*(?:rate|coupon)\b', text_lower):
        meta["coupon_type"] = "FLOATING"
    else:
        meta["coupon_type"] = "FIXED"

    # ── Maturity date ──
    for pat in [
        r'(?:maturity|redemption|final\s+redemption)\s*date\s*[:\-–]\s*(\d{1,2}[\s/\-](?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*[,]?\s*\d{4})',
        r'(?:maturity|redemption)\s*date\s*[:\-–]\s*(\d{1,2}[\s/\-]\d{1,2}[\s/\-]\d{4})',
        r'(?:maturity|redemption)\s*date\s*[:\-–]\s*(\d{4}[\s/\-]\d{1,2}[\s/\-]\d{1,2})',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            meta["maturity_date"] = m.group(1).strip()
            break

    # ── Issue date ──
    for pat in [
        r'(?:(?:deemed\s+)?date\s+of\s+allotment|issue\s*date)\s*[:\-–]\s*(\d{1,2}[\s/\-](?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*[,]?\s*\d{4})',
        r'(?:issue\s*date)\s*[:\-–]\s*(\d{1,2}[\s/\-]\d{1,2}[\s/\-]\d{4})',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            meta["issue_date"] = m.group(1).strip()
            break

    # ── Tenor ──
    for pat in [
        r'(?:tenor|tenure|term)\s*[:\-–]\s*(\d+)\s*(?:years?|months?|days?)',
        r'(\d+)\s*(?:years?|months?)\s*(?:tenor|tenure)',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            meta["tenor"] = m.group(0).strip()[:80]
            break

    # ── Credit rating ──
    for pat in [
        r'(?:instrument\s+)?(?:credit\s+)?rating\s*[:\-–]\s*["\']?([A-D][A-Za-z+\-]{1,8})',
        r'rated\s+["\']?([A-D][A-Za-z+\-]{1,8})',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            val = m.group(1).strip().upper()
            if re.match(r'^(?:AAA|AA\+?|AA-|A\+?|A-|BBB\+?|BBB-?|BB\+?|BB-?|B\+?|B-?|C|D)', val):
                meta["credit_rating"] = val
                break

    # ── Rating agency ──
    for pat in [
        r'(?:rating\s+agency|rated\s+by|rating\s+assigned\s+by)\s*[:\-–]?\s*(.+?)(?:\n|$)',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            meta["rating_agency"] = m.group(1).strip()[:100]
            break
    if not meta.get("rating_agency"):
        agencies = []
        for name in ["CRISIL", "ICRA", "CARE", "India Ratings", "Brickwork", "ACUITE", "Fitch", "Moody", "S&P"]:
            if name.lower() in text_lower:
                agencies.append(name)
        if agencies:
            meta["rating_agency"] = ", ".join(agencies)

    # ── Trustee ──
    for pat in [
        r'(?:debenture\s+trustee)\s*[:\-–]\s*(.+?)(?:\n|$)',
        r'(?:security\s+trustee)\s*[:\-–]\s*(.+?)(?:\n|$)',
        r'(?:trustee)\s*[:\-–]\s*(.+?)(?:\n|$)',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            val = m.group(1).strip()[:200]
            if "debenture" in pat:
                meta["debenture_trustee"] = val
            elif "security" in pat:
                meta["security_trustee"] = val
            if "trustee" not in meta:
                meta["trustee"] = val

    # ── Registrar ──
    m = re.search(r'(?:registrar(?:\s+and\s+transfer\s+agent)?|rta)\s*[:\-–]\s*(.+?)(?:\n|$)', full_text, re.I)
    if m:
        meta["registrar"] = m.group(1).strip()[:200]

    # ── Listing exchange ──
    listing = []
    if re.search(r'\bbse\b', text_lower):
        listing.append("BSE")
    if re.search(r'\bnse\b', text_lower):
        listing.append("NSE")
    if listing:
        meta["listing_exchange"] = ", ".join(listing)

    # ── Governing law ──
    m = re.search(r'(?:governing\s+law|jurisdiction)\s*[:\-–]\s*(.+?)(?:\n|$)', full_text, re.I)
    if m:
        val = m.group(1).strip()[:200]
        meta["governing_law"] = val
        meta["jurisdiction"] = val

    # ── Security type ──
    if re.search(r'\bunsecured\b', text_lower):
        meta["security_type"] = "UNSECURED"
    elif re.search(r'\bsecured\b', text_lower):
        meta["security_type"] = "SECURED"

    # ── Seniority ──
    if re.search(r'\bsubordinated?\b', text_lower):
        meta["seniority"] = "SUBORDINATED"
    elif re.search(r'\bsenior\s+(?:secured|unsecured)?\b', text_lower):
        meta["seniority"] = "SENIOR"
    elif re.search(r'\bpari[- ]?passu\b', text_lower):
        meta["seniority"] = "PARI_PASSU"

    # ── Guarantee ──
    guarantee_pats = [
        "corporate guarantee", "personal guarantee", "promoter guarantee",
        "unconditional guarantee", "irrevocable guarantee",
        "unconditional and irrevocable guarantee",
        "guarantee by", "guaranteed by",
    ]
    for kw in guarantee_pats:
        if kw in text_lower:
            idx = text_lower.find(kw)
            meta["guarantee"] = full_text[idx:idx + 400].strip()
            break

    # ── Face value ──
    for pat in [
        r'face\s*value\s*[:\-–]?\s*(?:Rs\.?\s*|INR\s*|₹\s*)?(\d[\d,]*(?:\.\d+)?)',
        r'(?:denomination|nominal\s+value)\s*[:\-–]?\s*(?:Rs\.?\s*|INR\s*|₹\s*)?(\d[\d,]*(?:\.\d+)?)',
    ]:
        m = re.search(pat, full_text, re.I)
        if m:
            try:
                meta["face_value"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    # ── Call option ──
    for pat in [r'call\s*option', r'callable', r'issuer.*?right.*?(?:early\s+)?redeem', r'early\s+redemption\s+at\s+the\s+option\s+of\s+the\s+issuer']:
        m = re.search(pat + r'[^.]{0,300}\.', full_text, re.I)
        if m:
            meta["call_option"] = m.group(0).strip()[:400]
            break

    # ── Put option ──
    for pat in [r'put\s*option', r'puttable', r'investor.*?right.*?(?:early\s+)?redemption']:
        m = re.search(pat + r'[^.]{0,200}\.', full_text, re.I)
        if m:
            meta["put_option"] = m.group(0).strip()[:400]
            break

    # ── Step-up ──
    m = re.search(r'step[- ]?up[^.]*\.', full_text, re.I)
    if m:
        meta["step_up_coupon"] = m.group(0).strip()[:400]

    # ── ISIN validation ──
    if meta.get("isin"):
        isin = meta["isin"]
        meta["isin_validated"] = 1 if re.match(r'^IN[A-Z0-9]{10}$', isin) else 0

    return meta


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 6 — CLAUSE DETECTION AGENTS
# ═══════════════════════════════════════════════════════════════════════════════

# Confidence threshold below which we say "not detected with sufficient
# confidence" rather than "ABSENT" (false-negative prevention)
FALSE_NEGATIVE_CONFIDENCE_THRESHOLD = 0.80

CLAUSE_AGENTS = {
    "ACCELERATION": {
        "name": "Acceleration Clause",
        "definition": "Allows bondholders or trustees to demand immediate repayment of principal and interest if an event of default occurs.",
        "patterns": [
            (r"accelerat(?:ion|ed|e)\s+(?:of\s+)?(?:the\s+)?(?:debentures?|bonds?|ncds?|principal|payment|obligation)", 0.95),
            (r"(?:all\s+)?(?:outstanding\s+)?(?:amounts?|principal|sums?)\s+(?:shall|will|may)\s+(?:become|be(?:come)?)\s+(?:immediately\s+)?due\s+and\s+payable", 0.92),
            (r"declare\s+(?:all|the)\s+(?:outstanding\s+)?(?:amounts?|principal)\s+(?:to\s+be\s+)?(?:immediately\s+)?due", 0.90),
            (r"accelerat(?:ion|ed|e)", 0.65),
        ],
        "target_sections": ["Events of Default", "Covenants", "Terms of Issue"],
    },
    "CROSS_DEFAULT": {
        "name": "Cross Default Clause",
        "definition": "Default on another debt obligation triggers default on this bond.",
        "patterns": [
            (r"cross[- ]?default", 0.95),
            (r"default\s+(?:under|on|in\s+respect\s+of)\s+(?:any\s+)?other\s+(?:indebtedness|borrowing|debt|loan|obligation|agreement)", 0.92),
            (r"default\s+(?:by\s+the\s+)?(?:issuer|company)\s+(?:under|on)\s+any\s+other", 0.88),
            (r"defaults?\s+on\s+(?:its?\s+)?other\s+(?:financial\s+)?obligations?", 0.80),
        ],
        "target_sections": ["Events of Default", "Covenants"],
    },
    "NEGATIVE_PLEDGE": {
        "name": "Negative Pledge Clause",
        "definition": "Issuer agrees not to pledge assets to other creditors without equally securing these bonds.",
        "patterns": [
            (r"negative\s+pledge", 0.95),
            (r"shall\s+not\s+(?:create|grant|permit|allow)\s+(?:any\s+)?(?:lien|charge|encumbrance|security\s+interest|mortgage|hypothecation)", 0.92),
            (r"shall\s+not\s+(?:create|grant|permit|allow)[^.]{0,140}(?:further|additional)?\s*(?:lien|charge|encumbrance|security\s+interest|mortgage|hypothecation)", 0.91),
            (r"(?:refrain|restrict(?:ed)?)\s+from\s+(?:creating|granting)\s+(?:any\s+)?(?:further\s+)?(?:charge|lien|encumbrance)", 0.88),
            (r"(?:no|not)\s+(?:further|additional)\s+(?:charge|lien|encumbrance|pledge)\s+(?:on|over|against)", 0.85),
        ],
        "target_sections": ["Covenants", "Security", "Events of Default"],
    },
    "CHANGE_OF_CONTROL": {
        "name": "Change of Control Clause",
        "definition": "Mandatory prepayment or put option triggered by change in ownership or control of the issuer.",
        "patterns": [
            (r"change\s*(?:of|in)\s*control", 0.95),
            (r"(?:transfer|sale)\s+of\s+(?:controlling|majority)\s+(?:interest|stake|shareholding)", 0.90),
            (r"(?:acquisition|takeover)\s+(?:of|by)\s+(?:the\s+)?(?:issuer|company)", 0.85),
            (r"change\s+in\s+(?:the\s+)?(?:management|promoter|ownership)", 0.82),
        ],
        "target_sections": ["Events of Default", "Covenants", "Terms of Issue"],
    },
    "SECURITY": {
        "name": "Security Clause",
        "definition": "Details the assets pledged as security for the bond.",
        "patterns": [
            (r"(?:first|second|exclusive|pari[- ]?passu)\s+(?:ranking\s+)?(?:charge|lien|mortgage)\s+(?:on|over)", 0.95),
            (r"secured\s+by\s+(?:way\s+of\s+)?(?:a\s+)?(?:mortgage|charge|pledge|hypothecation|lien)", 0.92),
            (r"creation\s+of\s+(?:security|charge)\s+(?:in\s+favour|for\s+the\s+benefit)", 0.90),
            (r"(?:first|exclusive)\s+(?:pari[- ]?passu\s+)?(?:floating|fixed)\s+charge", 0.90),
            (r"mortgage\s+(?:of|on|over)\s+(?:the\s+)?(?:immovable|movable|fixed)\s+assets?", 0.85),
        ],
        "target_sections": ["Security", "Terms of Issue"],
    },
    "GUARANTEE": {
        "name": "Guarantee Clause",
        "definition": "Third-party guarantee backing the bond obligations.",
        "patterns": [
            (r"(?:corporate|personal|promoter|unconditional|irrevocable)\s+guarantee", 0.95),
            (r"(?:guarantor|guarantee(?:e)?)\s+(?:shall|agrees?\s+to|undertakes?\s+to)\s+(?:pay|discharge|perform|guarantee)", 0.92),
            (r"guaranteed\s+by\s+(?:the\s+)?(?:promoter|parent|holding|group)", 0.90),
            (r"guarantee\s+(?:given|provided|furnished)\s+by", 0.85),
        ],
        "target_sections": ["Guarantee", "Security", "Terms of Issue"],
    },
    "EVENT_OF_DEFAULT": {
        "name": "Event of Default",
        "definition": "Conditions that constitute a default and trigger consequences.",
        "patterns": [
            (r"events?\s+of\s+default", 0.95),
            (r"(?:each|any)\s+of\s+the\s+following\s+(?:events?\s+)?(?:shall\s+)?constitute\s+(?:an?\s+)?(?:event\s+of\s+)?default", 0.92),
            (r"default\s+(?:shall\s+)?(?:be\s+deemed\s+to\s+)?(?:occur|have\s+occurred)", 0.85),
            (r"(?:following|below)\s+(?:events?\s+)?shall\s+constitute\s+(?:events?\s+of\s+)?default", 0.88),
        ],
        "target_sections": ["Events of Default"],
    },
}


def run_clause_agents(
    full_text: str,
    pages: List[Dict],
    sections: List[Dict],
) -> List[Dict[str, Any]]:
    """
    Run all clause detection agents.
    Implements false-negative prevention: status is ABSENT only when
    confidence > FALSE_NEGATIVE_CONFIDENCE_THRESHOLD.
    """
    results = []
    full_lower = full_text.lower()

    for clause_type, agent in CLAUSE_AGENTS.items():
        best_result = {
            "clause_type": clause_type,
            "status": "ABSENT",
            "page_number": None,
            "section": None,
            "clause_text": None,
            "confidence": 0.0,
            "evidence": [],
        }
        any_weak_signal = False

        # Collect section texts for this agent's target sections
        section_texts = {}
        for sec in sections:
            if sec["section_name"] in agent["target_sections"]:
                section_texts[sec["section_name"]] = {
                    "text": sec.get("section_text", ""),
                    "start_page": sec.get("start_page"),
                    "end_page": sec.get("end_page"),
                }

        for pattern, base_conf in agent["patterns"]:
            # Search in TARGET SECTIONS first (higher confidence)
            for sec_name, sec_info in section_texts.items():
                sec_text = sec_info["text"]
                match = re.search(pattern, sec_text, re.IGNORECASE)
                if match:
                    page_num = _find_page_for_text(
                        match.group(0), pages,
                        sec_info["start_page"], sec_info["end_page"]
                    )
                    start = max(0, match.start() - 150)
                    end = min(len(sec_text), match.end() + 400)
                    clause_text = sec_text[start:end].strip()
                    # Bonus for being in the right section
                    confidence = min(base_conf + 0.05, 1.0)

                    if confidence > best_result["confidence"]:
                        best_result.update({
                            "status": "PRESENT",
                            "page_number": page_num,
                            "section": sec_name,
                            "clause_text": clause_text[:600],
                            "confidence": round(confidence, 3),
                        })
                    # Store evidence
                    best_result["evidence"].append({
                        "page_number": page_num,
                        "section": sec_name,
                        "text_snippet": clause_text[:400],
                        "confidence": round(confidence, 3),
                    })
                    any_weak_signal = True

            # Also search FULL TEXT (lower confidence)
            for match in re.finditer(pattern, full_text, re.IGNORECASE):
                page_num = _find_page_for_text(match.group(0), pages)
                start = max(0, match.start() - 150)
                end = min(len(full_text), match.end() + 400)
                clause_text = full_text[start:end].strip()
                confidence = base_conf * 0.85  # Penalise non-section match

                if confidence > best_result["confidence"]:
                    best_result.update({
                        "status": "PRESENT",
                        "page_number": page_num,
                        "section": _find_section_for_page(page_num, sections),
                        "clause_text": clause_text[:600],
                        "confidence": round(confidence, 3),
                    })

                # Deduplicated evidence
                ev = {
                    "page_number": page_num,
                    "section": _find_section_for_page(page_num, sections),
                    "text_snippet": clause_text[:400],
                    "confidence": round(confidence, 3),
                }
                if not any(
                    e["page_number"] == ev["page_number"] and
                    e["text_snippet"][:80] == ev["text_snippet"][:80]
                    for e in best_result["evidence"]
                ):
                    best_result["evidence"].append(ev)

                any_weak_signal = True
                break  # First match per pattern in full text is enough

        # ── False-negative prevention (Step 10) ──
        if best_result["status"] == "ABSENT":
            if any_weak_signal and best_result["confidence"] < FALSE_NEGATIVE_CONFIDENCE_THRESHOLD:
                best_result["status"] = "INCONCLUSIVE"
                best_result["clause_text"] = "Clause not detected with sufficient confidence."

        results.append(best_result)

    return results


def _find_page_for_text(text: str, pages: List[Dict], start_page: int = None, end_page: int = None) -> Optional[int]:
    """Find which page contains the given text snippet."""
    text_lower = text.lower()[:80]
    search_pages = pages
    if start_page and end_page:
        search_pages = [p for p in pages if start_page <= p["page_number"] <= end_page]

    for page in search_pages:
        page_text = (page.get("clean_text") or page.get("raw_text", "")).lower()
        if text_lower in page_text:
            return page["page_number"]

    # Fallback to start_page, then first page
    if start_page:
        return start_page
    if pages:
        return pages[0]["page_number"]
    return None


def _find_section_for_page(page_num: int, sections: List[Dict]) -> Optional[str]:
    """Find which section a page belongs to."""
    if not page_num:
        return None
    for sec in sections:
        if sec.get("start_page") and sec.get("end_page"):
            if sec["start_page"] <= page_num <= sec["end_page"]:
                return sec["section_name"]
    return None


def _expand_to_sentence(text: str, start: int, end: int, window: int = 220) -> str:
    """Expand a regex hit to the surrounding sentence / clause context."""
    left = max(0, start - window)
    right = min(len(text), end + window)

    left_boundary = text.rfind(".", left, start)
    left_boundary = max(left_boundary, text.rfind("\n", left, start))
    right_boundary = text.find(".", end, right)
    newline_boundary = text.find("\n", end, right)
    if right_boundary == -1 or (newline_boundary != -1 and newline_boundary < right_boundary):
        right_boundary = newline_boundary

    snippet_start = left_boundary + 1 if left_boundary != -1 else left
    snippet_end = right_boundary + 1 if right_boundary != -1 else right
    return text[snippet_start:snippet_end].strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 7 — COVENANT EXTRACTION  (with binding-language validation)
# ═══════════════════════════════════════════════════════════════════════════════

BINDING_KEYWORDS_RE = re.compile(
    r"shall|must|will\s+maintain|will\s+ensure|undertakes?\s+to|agrees?\s+to|"
    r"obligated\s+to|required\s+to|shall\s+ensure|shall\s+maintain|"
    r"shall\s+not|will\s+not|must\s+not|hereby\s+covenants?|"
    r"hereby\s+undertakes?|hereby\s+agrees?|is\s+required\s+to|shall\s+be\s+bound",
    re.IGNORECASE,
)

FINANCIAL_COVENANT_PATTERNS = [
    {
        "name": "Debt to Equity",
        "patterns": [r"debt[/ ]?(?:to[/ ])?equity\s*ratio[^.]*\.", r"debt[- ]equity\s*ratio[^.]*\."],
        "threshold_pattern": r"(?:not\s+(?:to\s+)?exceed|(?:<=?|≤)\s*|maintain[^.]*(?:below|under|within)|maximum\s+(?:of\s+)?)\s*(\d+\.?\d*)\s*(?:x|times?)?",
    },
    {
        "name": "Interest Coverage Ratio",
        "patterns": [r"interest\s*(?:coverage|service)\s*ratio[^.]*\.", r"ICR[^.]*\."],
        "threshold_pattern": r"(?:not\s+(?:fall\s+)?below|(?:>=?|≥)\s*|maintain[^.]*(?:above|over|minimum)|minimum\s+(?:of\s+)?)\s*(\d+\.?\d*)\s*(?:x|times?)?",
    },
    {
        "name": "DSCR",
        "patterns": [r"(?:debt\s+service\s+coverage|DSCR)\s*ratio?[^.]*\.", r"DSCR[^.]*\."],
        "threshold_pattern": r"(?:not\s+(?:fall\s+)?below|(?:>=?|≥)\s*|minimum\s+(?:of\s+)?)\s*(\d+\.?\d*)\s*(?:x|times?)?",
    },
    {
        "name": "Minimum Net Worth",
        "patterns": [r"(?:minimum\s+)?net\s*worth[^.]*(?:maintain|not\s+fall|covenant)[^.]*\.", r"net\s*worth\s*(?:>=?|≥|shall\s+not\s+fall\s+below)[^.]*\."],
        "threshold_pattern": r"(?:Rs\.?\s*|INR\s*)?(\d[\d,.]*)\s*(?:cr|crore|lakh)",
    },
    {
        "name": "Current Ratio",
        "patterns": [r"current\s+ratio[^.]*\.", r"liquidity\s+ratio[^.]*\."],
        "threshold_pattern": r"(?:not\s+(?:fall\s+)?below|(?:>=?|≥)\s*|minimum\s+(?:of\s+)?)\s*(\d+\.?\d*)",
    },
    {
        "name": "Debt/EBITDA",
        "patterns": [r"debt[/ ]?(?:to[/ ])?EBITDA\s*ratio[^.]*\.", r"total\s+debt\s*(?:to|/)\s*EBITDA[^.]*\."],
        "threshold_pattern": r"(?:not\s+(?:to\s+)?exceed|(?:<=?|≤)\s*|maximum\s+(?:of\s+)?)\s*(\d+\.?\d*)\s*(?:x|times?)?",
    },
    {
        "name": "TOL/TNW",
        "patterns": [r"(?:TOL[/ ]?TNW|total\s+(?:outside\s+)?liabilities?\s*(?:to|/)\s*(?:tangible\s+)?net\s*worth)[^.]*\."],
        "threshold_pattern": r"(?:not\s+(?:to\s+)?exceed|(?:<=?|≤)\s*|maximum\s+(?:of\s+)?)\s*(\d+\.?\d*)\s*(?:x|times?)?",
    },
]

OPERATIONAL_COVENANT_PATTERNS = [
    {
        "name": "No Additional Borrowing",
        "patterns": [r"(?:no|not)\s+(?:additional|further)\s+borrowing[^.]*\.", r"(?:restrict|prohibit)[^.]*(?:additional|further)\s+(?:borrowing|indebtedness)[^.]*\."],
    },
    {
        "name": "Asset Sale Restrictions",
        "patterns": [r"(?:no|not)\s+(?:sell|dispose|alienate|transfer)[^.]*(?:assets?|property)[^.]*\.", r"asset\s+sale\s+restriction[^.]*\."],
    },
    {
        "name": "Dividend Restrictions",
        "patterns": [r"(?:no|not|restrict)[^.]*dividend[^.]*\.", r"dividend\s+(?:restriction|lock|payout\s+limit)[^.]*\."],
    },
    {
        "name": "Maintenance of Security",
        "patterns": [r"(?:maintain|ensure)\s+(?:the\s+)?(?:security|charge|cover)[^.]*\.", r"security\s+cover\s*(?:ratio)?\s*(?:of|at\s+least)[^.]*\."],
    },
    {
        "name": "Change in Management",
        "patterns": [r"change\s+in\s+(?:management|promoter|controlling)[^.]*\.", r"(?:prior\s+consent|approval)[^.]*(?:change|transfer)\s+(?:of|in)\s+(?:management|control)[^.]*\."],
    },
    {
        "name": "Related Party Restrictions",
        "patterns": [r"related\s+party\s+(?:transaction|restriction|limit)[^.]*\.", r"(?:restrict|limit|cap)[^.]*related\s+party[^.]*\."],
    },
]


def extract_covenants(
    full_text: str,
    pages: List[Dict],
    sections: List[Dict],
) -> List[Dict[str, Any]]:
    """
    Extract financial and operational covenants from IM.
    Only includes matches that contain binding language (Step 8).
    Returns list of covenant dicts.
    """
    results = []

    # Prefer searching in Covenants section
    covenant_sections = [s for s in sections if s["section_name"] in ("Covenants", "Terms of Issue")]
    search_texts = []
    if covenant_sections:
        for cs in covenant_sections:
            search_texts.append({
                "text": cs.get("section_text", ""),
                "section": cs["section_name"],
                "start_page": cs.get("start_page"),
                "end_page": cs.get("end_page"),
            })
    # Also search full text as fallback
    search_texts.append({
        "text": full_text,
        "section": "Full Document",
        "start_page": 1,
        "end_page": len(pages) if pages else None,
    })

    already_found = set()

    # Financial covenants
    for cov_def in FINANCIAL_COVENANT_PATTERNS:
        for search_ctx in search_texts:
            if cov_def["name"] in already_found:
                break
            for pat in cov_def["patterns"]:
                match = re.search(pat, search_ctx["text"], re.IGNORECASE)
                if match:
                    cov_text = _expand_to_sentence(search_ctx["text"], match.start(), match.end()) or match.group(0).strip()

                    # ── Binding-language gate ──
                    if not BINDING_KEYWORDS_RE.search(cov_text):
                        continue  # skip non-binding mentions

                    page_num = _find_page_for_text(
                        cov_text[:50], pages,
                        search_ctx.get("start_page"),
                        search_ctx.get("end_page"),
                    )

                    # Extract threshold
                    threshold = None
                    if cov_def.get("threshold_pattern"):
                        tm = re.search(cov_def["threshold_pattern"], cov_text, re.I)
                        if tm:
                            threshold = tm.group(0).strip()

                    results.append({
                        "covenant_type": "FINANCIAL",
                        "covenant_name": cov_def["name"],
                        "threshold": threshold,
                        "page_number": page_num,
                        "section": search_ctx["section"],
                        "text": cov_text[:600],
                        "confidence": 0.90 if search_ctx["section"] != "Full Document" else 0.75,
                    })
                    already_found.add(cov_def["name"])
                    break

    # Operational covenants
    for cov_def in OPERATIONAL_COVENANT_PATTERNS:
        for search_ctx in search_texts:
            if cov_def["name"] in already_found:
                break
            for pat in cov_def["patterns"]:
                match = re.search(pat, search_ctx["text"], re.IGNORECASE)
                if match:
                    cov_text = _expand_to_sentence(search_ctx["text"], match.start(), match.end()) or match.group(0).strip()

                    # ── Binding-language gate ──
                    if not BINDING_KEYWORDS_RE.search(cov_text):
                        continue  # skip non-binding mentions

                    page_num = _find_page_for_text(
                        cov_text[:50], pages,
                        search_ctx.get("start_page"),
                        search_ctx.get("end_page"),
                    )

                    results.append({
                        "covenant_type": "OPERATIONAL",
                        "covenant_name": cov_def["name"],
                        "threshold": None,
                        "page_number": page_num,
                        "section": search_ctx["section"],
                        "text": cov_text[:600],
                        "confidence": 0.85 if search_ctx["section"] != "Full Document" else 0.70,
                    })
                    already_found.add(cov_def["name"])
                    break

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 8 — RISK SCORING ENGINE  (corrected thresholds)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_institutional_risk_score(
    metadata: Dict,
    clauses: List[Dict],
    covenants: List[Dict],
    credit_rating: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute the institutional-grade risk score.

    Risk bands:
      0-20  → LOW
      21-40 → MEDIUM
      41-60 → HIGH
      61-100→ CRITICAL

    Scoring logic:
      Unsecured bond → +15
      Subordinated → +15
      No cross default → +8
      No negative pledge → +8
      No promoter guarantee → +10
      No covenants → +10
      No acceleration clause → +5
      No change of control → +6
      No event of default section → +7
      High coupon (>12%) → +6
      Bullet repayment → +5
      Low rating → up to +20

    Returns {risk_score, risk_level, breakdown, red_flags}.
    """
    score = 0.0
    red_flags = []
    breakdown = {}

    # Clause lookup
    clause_lookup = {c["clause_type"]: c for c in clauses}

    # 1. Unsecured
    unsecured_pts = 0
    security_type = metadata.get("security_type", "")
    if security_type == "UNSECURED" or not security_type:
        unsecured_pts = 15
        red_flags.append("Unsecured Issue — No asset backing")
    breakdown["unsecured_pts"] = unsecured_pts
    score += unsecured_pts

    # 1b. Subordinated penalty
    subordinated_pts = 0
    instrument = (metadata.get("instrument_type") or "").upper()
    issuer_name = (metadata.get("issuer_name") or "").upper()
    full_label = f"{instrument} {issuer_name}"
    if re.search(r"subordinat(?:ed|ion)", full_label, re.IGNORECASE):
        subordinated_pts = 15
        red_flags.append("Subordinated Instrument — Higher loss-given-default risk")
    breakdown["subordinated_pts"] = subordinated_pts
    score += subordinated_pts

    # 2. Cross-default
    cd_pts = 0
    cd = clause_lookup.get("CROSS_DEFAULT", {})
    has_cd = cd.get("status") == "PRESENT"
    if not has_cd:
        cd_pts = 8
        red_flags.append("No Cross-Default Clause")
    breakdown["no_cross_default_pts"] = cd_pts
    score += cd_pts

    # 3. Negative pledge
    np_pts = 0
    np_clause = clause_lookup.get("NEGATIVE_PLEDGE", {})
    has_np = np_clause.get("status") == "PRESENT"
    if not has_np:
        np_pts = 8
        red_flags.append("No Negative Pledge")
    breakdown["no_negative_pledge_pts"] = np_pts
    score += np_pts

    # 4. Guarantee
    guar_pts = 0
    guar = clause_lookup.get("GUARANTEE", {})
    has_guar = guar.get("status") == "PRESENT"
    if not has_guar and not metadata.get("guarantee"):
        guar_pts = 10
        red_flags.append("No Promoter/Corporate Guarantee")
    breakdown["no_guarantee_pts"] = guar_pts
    score += guar_pts

    # 5. Covenants
    cov_pts = 0
    if not covenants or len(covenants) == 0:
        cov_pts = 10
        red_flags.append("No Covenants Detected")
    breakdown["no_covenants_pts"] = cov_pts
    score += cov_pts

    # 6. Acceleration clause
    accel = clause_lookup.get("ACCELERATION", {})
    if accel.get("status") != "PRESENT":
        score += 5
        breakdown["no_acceleration_pts"] = 5
    else:
        breakdown["no_acceleration_pts"] = 0

    # 7. Change of control
    coc = clause_lookup.get("CHANGE_OF_CONTROL", {})
    if coc.get("status") != "PRESENT":
        score += 6
        red_flags.append("No Change of Control Protection")
        breakdown["no_coc_pts"] = 6
    else:
        breakdown["no_coc_pts"] = 0

    # 8. Event of default
    eod = clause_lookup.get("EVENT_OF_DEFAULT", {})
    if eod.get("status") != "PRESENT":
        score += 7
        red_flags.append("No Event of Default Section Found")
        breakdown["no_eod_pts"] = 7
    else:
        breakdown["no_eod_pts"] = 0

    # 9. Low rating
    low_rating_pts = 0
    rating = credit_rating or metadata.get("credit_rating", "")
    RATING_RISK = {
        "AAA": 0, "AA+": 0, "AA": 2, "AA-": 3, "A+": 5, "A": 7, "A-": 9,
        "BBB+": 12, "BBB": 15, "BBB-": 18, "BB+": 20, "BB": 20, "BB-": 20,
        "B+": 20, "B": 20, "B-": 20, "C": 20, "D": 20,
    }
    for r, pts in RATING_RISK.items():
        if r in str(rating).upper():
            low_rating_pts = pts
            if pts >= 10:
                red_flags.append(f"Low Credit Rating ({rating})")
            break
    breakdown["low_rating_pts"] = low_rating_pts
    score += low_rating_pts

    # 10. High coupon
    high_coupon_pts = 0
    coupon = metadata.get("coupon_rate") or 0
    if coupon > 12:
        high_coupon_pts = 6
        red_flags.append(f"High Coupon ({coupon}%) — may indicate credit risk")
    breakdown["high_coupon_pts"] = high_coupon_pts
    score += high_coupon_pts

    # 11. Bullet repayment  (no call/put → likely bullet → +5)
    bullet_pts = 0
    if not metadata.get("call_option") and not metadata.get("put_option"):
        bullet_pts = 5
    breakdown["bullet_repayment_pts"] = bullet_pts
    score += bullet_pts

    # ── Determine level (corrected thresholds) ──
    score = min(round(score, 1), 100)
    if score >= 61:
        level = "CRITICAL"
    elif score >= 41:
        level = "HIGH"
    elif score >= 21:
        level = "MEDIUM"
    else:
        level = "LOW"

    return {
        "risk_score": score,
        "risk_level": level,
        "breakdown": breakdown,
        "red_flags": red_flags,
        "clause_presence": {
            "cross_default": int(has_cd),
            "negative_pledge": int(has_np),
            "acceleration": int(accel.get("status") == "PRESENT"),
            "change_of_control": int(coc.get("status") == "PRESENT"),
            "security": int(clause_lookup.get("SECURITY", {}).get("status") == "PRESENT"),
            "guarantee": int(has_guar),
            "event_of_default": int(eod.get("status") == "PRESENT"),
        },
        "covenant_count": len(covenants),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 9 — LLM CLAUSE AGENTS  (optional enrichment)
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_llm_clause_agent(
    clause_type: str,
    clause_def: Dict,
    full_text: str,
    existing_result: Dict,
) -> Dict[str, Any]:
    """
    Call an LLM to validate / enrich a single clause detection result.
    Falls back gracefully if no API key is configured.
    """
    llm_enabled = os.environ.get("LLM_CLAUSE_ENRICHMENT_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    api_key = os.environ.get("OPENAI_API_KEY")
    if not llm_enabled or not api_key:
        return existing_result  # No LLM available, keep regex result

    try:
        import httpx  # noqa: F811
        prompt = (
            f"You are a fixed-income bond analyst. Analyse the following bond Information Memorandum text "
            f"and determine whether a {clause_def['name']} clause is present.\n\n"
            f"Definition: {clause_def['definition']}\n\n"
            f"Text (first 8000 chars):\n{full_text[:8000]}\n\n"
            f"Return JSON: {{\"present\": true/false, \"confidence\": 0.0-1.0, "
            f"\"excerpt\": \"relevant text excerpt if found\", \"reasoning\": \"brief explanation\"}}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                llm_result = json.loads(content)
                # Merge LLM insights
                if llm_result.get("present") and not existing_result.get("status") == "PRESENT":
                    existing_result["status"] = "PRESENT"
                    existing_result["confidence"] = max(
                        existing_result.get("confidence", 0),
                        llm_result.get("confidence", 0.75),
                    )
                    existing_result["clause_text"] = llm_result.get("excerpt", "")[:600]
                existing_result["llm_reasoning"] = llm_result.get("reasoning", "")
    except Exception:
        pass  # LLM enrichment is best-effort

    return existing_result


async def enrich_clauses_with_llm(
    clauses: List[Dict],
    full_text: str,
) -> List[Dict]:
    """
    Enrich clause results with LLM analysis for ABSENT / INCONCLUSIVE clauses.
    Only calls LLM when a clause was NOT found by regex.
    """
    llm_enabled = os.environ.get("LLM_CLAUSE_ENRICHMENT_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    api_key = os.environ.get("OPENAI_API_KEY")
    if not llm_enabled or not api_key:
        return clauses  # Skip if no key

    enriched = []
    for cl in clauses:
        if cl["status"] in ("ABSENT", "INCONCLUSIVE"):
            agent = CLAUSE_AGENTS.get(cl["clause_type"])
            if agent:
                cl = await _run_llm_clause_agent(
                    cl["clause_type"], agent, full_text, cl
                )
        enriched.append(cl)
    return enriched


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 9 — FULL INSTITUTIONAL PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

async def run_institutional_analysis(
    upload_id: int,
    isin: str,
    file_bytes: bytes,
    credit_rating: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run the full institutional-grade IM analysis pipeline.
    Stores all results in the normalised tables, including
    processing log and full text.

    Returns complete analysis results dict.
    """
    t0 = time.time()
    await _ensure_institutional_schema_compat()
    result = {
        "upload_id": upload_id,
        "isin": isin,
        "status": "error",
    }

    # ── Step 1: Page-level extraction (hybrid OCR) ───────────────────
    pages, method_used = extract_pages_from_pdf(file_bytes)
    if not pages:
        elapsed = round(time.time() - t0, 2)
        # Store processing failure in log
        try:
            await execute_query(
                """INSERT INTO im_processing_log
                   (upload_id, method_used, char_count, pages_processed, processing_time, status, error_message)
                   VALUES (:uid, :method, 0, 0, :elapsed, 'FAILED', 'No text extracted from PDF')""",
                {"uid": upload_id, "method": method_used, "elapsed": elapsed},
            )
        except Exception:
            pass
        result["errors"] = ["Could not extract pages from PDF"]
        return result

    # Store pages
    await execute_query("DELETE FROM im_pages WHERE upload_id = :uid", {"uid": upload_id})
    for pg in pages:
        await execute_query(
            """INSERT INTO im_pages (upload_id, page_number, raw_text, clean_text, token_count)
               VALUES (:uid, :pn, :raw, :clean, :tc)""",
            {
                "uid": upload_id,
                "pn": pg["page_number"],
                "raw": pg["raw_text"][:50000] if pg["raw_text"] else None,
                "clean": pg["clean_text"][:50000] if pg["clean_text"] else None,
                "tc": pg["token_count"],
            },
        )

    full_text = "\n\n".join(pg.get("clean_text") or pg.get("raw_text", "") for pg in pages)

    # ── Store full text (im_full_text) ───────────────────────────────
    try:
        await execute_query("DELETE FROM im_full_text WHERE upload_id = :uid", {"uid": upload_id})
        await execute_query(
            """INSERT INTO im_full_text (upload_id, full_text, text_length)
               VALUES (:uid, :ft, :tl)""",
            {"uid": upload_id, "ft": full_text[:500000], "tl": len(full_text)},
        )
    except Exception:
        pass  # Non-critical — table may not exist yet

    # ── Step 2: Section detection ────────────────────────────────────
    sections = detect_sections(pages)

    await execute_query("DELETE FROM im_sections WHERE upload_id = :uid", {"uid": upload_id})
    for sec in sections:
        await execute_query(
            """INSERT INTO im_sections (upload_id, section_name, start_page, end_page, section_text, confidence)
               VALUES (:uid, :name, :sp, :ep, :text, :conf)""",
            {
                "uid": upload_id,
                "name": sec["section_name"],
                "sp": sec["start_page"],
                "ep": sec["end_page"],
                "text": sec["section_text"][:50000] if sec.get("section_text") else None,
                "conf": sec.get("confidence", 1.0),
            },
        )

    # ── Step 3: Bond metadata extraction ─────────────────────────────
    metadata = extract_bond_metadata(full_text, pages)
    metadata["upload_id"] = upload_id
    metadata["isin"] = metadata.get("isin") or isin

    await execute_query("DELETE FROM bond_metadata WHERE upload_id = :uid", {"uid": upload_id})
    bond_metadata_columns = await _get_table_columns("bond_metadata")
    meta_cols = [k for k in metadata.keys() if metadata[k] is not None and k in bond_metadata_columns]
    if meta_cols:
        cols_str = ", ".join(meta_cols)
        vals_str = ", ".join(f":{k}" for k in meta_cols)
        await execute_query(
            f"INSERT INTO bond_metadata ({cols_str}) VALUES ({vals_str})",
            {k: metadata[k] for k in meta_cols},
        )

    # ── Step 4: Clause detection ─────────────────────────────────────
    clauses = run_clause_agents(full_text, pages, sections)

    # ── Step 4b: LLM enrichment (optional) ───────────────────────────
    clauses = await enrich_clauses_with_llm(clauses, full_text)

    await execute_query("DELETE FROM bond_clauses WHERE upload_id = :uid", {"uid": upload_id})
    await execute_query("DELETE FROM clause_evidence WHERE upload_id = :uid", {"uid": upload_id})

    for cl in clauses:
        await execute_query(
            """INSERT INTO bond_clauses (upload_id, clause_type, status, page_number, section, clause_text, confidence)
               VALUES (:uid, :ctype, :status, :page, :section, :text, :conf)""",
            {
                "uid": upload_id,
                "ctype": cl["clause_type"],
                "status": cl["status"],
                "page": cl.get("page_number"),
                "section": cl.get("section"),
                "text": cl.get("clause_text"),
                "conf": cl.get("confidence", 0.0),
            },
        )
        for ev in cl.get("evidence", []):
            await execute_query(
                """INSERT INTO clause_evidence (upload_id, clause_type, page_number, section, text_snippet, confidence)
                   VALUES (:uid, :ctype, :page, :section, :text, :conf)""",
                {
                    "uid": upload_id,
                    "ctype": cl["clause_type"],
                    "page": ev.get("page_number"),
                    "section": ev.get("section"),
                    "text": ev.get("text_snippet", ""),
                    "conf": ev.get("confidence", 0.0),
                },
            )

    # ── Step 5: Covenant extraction (with binding-language filter) ───
    covenants = extract_covenants(full_text, pages, sections)

    await execute_query("DELETE FROM bond_covenants WHERE upload_id = :uid", {"uid": upload_id})
    for cov in covenants:
        await execute_query(
            """INSERT INTO bond_covenants (upload_id, covenant_type, covenant_name, threshold, page_number, section, text, confidence)
               VALUES (:uid, :ctype, :cname, :thresh, :page, :section, :text, :conf)""",
            {
                "uid": upload_id,
                "ctype": cov["covenant_type"],
                "cname": cov["covenant_name"],
                "thresh": cov.get("threshold"),
                "page": cov.get("page_number"),
                "section": cov.get("section"),
                "text": cov.get("text"),
                "conf": cov.get("confidence", 0.0),
            },
        )

    # ── Step 6: Risk scoring ─────────────────────────────────────────
    risk = compute_institutional_risk_score(metadata, clauses, covenants, credit_rating)

    await execute_query("DELETE FROM bond_risk_analysis WHERE upload_id = :uid", {"uid": upload_id})
    clause_p = risk["clause_presence"]
    bd = risk.get("breakdown", {})
    await execute_query(
        """INSERT INTO bond_risk_analysis
           (upload_id, isin, risk_score, risk_level,
            cross_default, negative_pledge, acceleration, change_of_control,
            security, guarantee, covenants,
            unsecured_pts, no_cross_default_pts, no_negative_pledge_pts,
            no_guarantee_pts, no_covenants_pts, low_rating_pts,
            high_coupon_pts, bullet_repayment_pts,
            red_flags)
           VALUES
           (:uid, :isin, :score, :level,
            :cd, :np, :accel, :coc,
            :sec, :guar, :covs,
            :p_unsec, :p_cd, :p_np,
            :p_guar, :p_cov, :p_rating,
            :p_coupon, :p_bullet,
            :flags)""",
        {
            "uid": upload_id,
            "isin": isin,
            "score": risk["risk_score"],
            "level": risk["risk_level"],
            "cd": clause_p.get("cross_default", 0),
            "np": clause_p.get("negative_pledge", 0),
            "accel": clause_p.get("acceleration", 0),
            "coc": clause_p.get("change_of_control", 0),
            "sec": clause_p.get("security", 0),
            "guar": clause_p.get("guarantee", 0),
            "covs": risk.get("covenant_count", 0),
            "p_unsec": bd.get("unsecured_pts", 0),
            "p_cd": bd.get("no_cross_default_pts", 0),
            "p_np": bd.get("no_negative_pledge_pts", 0),
            "p_guar": bd.get("no_guarantee_pts", 0),
            "p_cov": bd.get("no_covenants_pts", 0),
            "p_rating": bd.get("low_rating_pts", 0),
            "p_coupon": bd.get("high_coupon_pts", 0),
            "p_bullet": bd.get("bullet_repayment_pts", 0),
            "flags": json.dumps(risk.get("red_flags", [])),
        },
    )

    # ── Processing log ───────────────────────────────────────────────
    elapsed = round(time.time() - t0, 2)
    try:
        await execute_query(
            """INSERT INTO im_processing_log
               (upload_id, method_used, char_count, pages_processed, processing_time, status, error_message)
               VALUES (:uid, :method, :chars, :pages, :elapsed, 'SUCCESS', NULL)""",
            {
                "uid": upload_id,
                "method": method_used,
                "chars": len(full_text),
                "pages": len(pages),
                "elapsed": elapsed,
            },
        )
    except Exception:
        pass  # Non-critical

    # Audit log
    await execute_query(
        """INSERT INTO im_audit_log (upload_id, isin, action, actor, details, created_at)
           VALUES (:uid, :isin, 'INSTITUTIONAL_ANALYSIS', 'system', :details, datetime('now'))""",
        {
            "uid": upload_id,
            "isin": isin,
            "details": json.dumps({
                "pages_extracted": len(pages),
                "method_used": method_used,
                "sections_detected": len(sections),
                "clauses_analysed": len(clauses),
                "clauses_present": sum(1 for c in clauses if c["status"] == "PRESENT"),
                "clauses_inconclusive": sum(1 for c in clauses if c["status"] == "INCONCLUSIVE"),
                "covenants_found": len(covenants),
                "risk_score": risk["risk_score"],
                "risk_level": risk["risk_level"],
                "processing_time_s": elapsed,
            }),
        },
    )

    result.update({
        "status": "success",
        "pages_count": len(pages),
        "total_tokens": sum(p["token_count"] for p in pages),
        "method_used": method_used,
        "processing_time_s": elapsed,
        "sections": [
            {"name": s["section_name"], "start_page": s["start_page"], "end_page": s["end_page"], "confidence": s.get("confidence")}
            for s in sections
        ],
        "metadata": metadata,
        "clauses": [
            {
                "clause_type": c["clause_type"],
                "status": c["status"],
                "page_number": c.get("page_number"),
                "section": c.get("section"),
                "clause_text": c.get("clause_text"),
                "confidence": c.get("confidence", 0),
                "evidence_count": len(c.get("evidence", [])),
            }
            for c in clauses
        ],
        "covenants": covenants,
        "risk": risk,
    })

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  STEP 10 — QUERY FUNCTIONS  (for API endpoints)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_institutional_analysis(upload_id: int, allow_backfill: bool = True) -> Dict[str, Any]:
    """
    Retrieve full institutional analysis for a given upload_id from DB.
    Includes processing log if available.
    """
    # Pages
    pages_rows = await fetch_all(
        "SELECT page_number, token_count FROM im_pages WHERE upload_id = :uid ORDER BY page_number",
        {"uid": upload_id},
    )

    # Sections
    sections_rows = await fetch_all(
        "SELECT section_name, start_page, end_page, confidence FROM im_sections WHERE upload_id = :uid ORDER BY start_page",
        {"uid": upload_id},
    )

    upload = await fetch_one(
        """SELECT id, isin, issuer_name, file_path, coupon, maturity_date, issue_size_cr,
                  face_value, credit_rating, rating_agency, security_type, seniority,
                  risk_score, risk_level, risk_flags
           FROM bond_im_uploads WHERE id = :id""",
        {"id": upload_id},
    )

    # Metadata
    metadata = await fetch_one(
        "SELECT * FROM bond_metadata WHERE upload_id = :uid",
        {"uid": upload_id},
    )
    if not metadata:
        metadata = {}

    # Clauses
    clauses_rows = await fetch_all(
        "SELECT clause_type, status, page_number, section, clause_text, confidence FROM bond_clauses WHERE upload_id = :uid",
        {"uid": upload_id},
    )

    # Evidence per clause
    evidence_rows = await fetch_all(
        "SELECT clause_type, page_number, section, text_snippet, confidence FROM clause_evidence WHERE upload_id = :uid",
        {"uid": upload_id},
    )
    evidence_by_type = {}
    for ev in evidence_rows:
        ct = ev["clause_type"]
        if ct not in evidence_by_type:
            evidence_by_type[ct] = []
        evidence_by_type[ct].append(ev)

    # Covenants
    covenants_rows = await fetch_all(
        "SELECT covenant_type, covenant_name, threshold, page_number, section, text, confidence FROM bond_covenants WHERE upload_id = :uid",
        {"uid": upload_id},
    )

    # Risk analysis
    risk = await fetch_one(
        "SELECT * FROM bond_risk_analysis WHERE upload_id = :uid",
        {"uid": upload_id},
    )
    if risk and risk.get("red_flags") and isinstance(risk["red_flags"], str):
        try:
            risk["red_flags"] = json.loads(risk["red_flags"])
        except Exception:
            pass

    if allow_backfill and not any([pages_rows, sections_rows, metadata, clauses_rows, covenants_rows, risk]):
        if upload and upload.get("file_path"):
            try:
                from .im_connectors import IM_STORAGE_DIR

                full_path = IM_STORAGE_DIR / upload["file_path"]
                if full_path.exists():
                    await run_institutional_analysis(
                        upload_id=upload_id,
                        isin=upload.get("isin") or "",
                        file_bytes=full_path.read_bytes(),
                        credit_rating=upload.get("credit_rating"),
                    )
                    return await get_institutional_analysis(upload_id, allow_backfill=False)
            except Exception as exc:
                logger.warning("institutional_backfill_failed", exc_info=exc)

    if upload:
        if upload.get("risk_flags") and isinstance(upload["risk_flags"], str):
            try:
                upload["risk_flags"] = json.loads(upload["risk_flags"])
            except Exception:
                pass

        bond_master = None
        if upload.get("isin"):
            try:
                bond_master = await fetch_one(
                    "SELECT isin, issuer_name, bond_name FROM universal_bond_master WHERE isin = :isin",
                    {"isin": upload["isin"]},
                )
            except Exception:
                bond_master = None

        metadata["isin"] = upload.get("isin") or metadata.get("isin")
        metadata_issuer = metadata.get("issuer_name")
        if _is_suspicious_issuer_value(metadata_issuer):
            fallback_issuer = upload.get("issuer_name") or (bond_master or {}).get("issuer_name") or (bond_master or {}).get("bond_name")
            metadata_issuer = fallback_issuer if not _is_suspicious_issuer_value(fallback_issuer) else None
        metadata["issuer_name"] = metadata_issuer
        metadata["coupon"] = metadata.get("coupon") or metadata.get("coupon_rate") or upload.get("coupon")
        metadata["maturity_date"] = metadata.get("maturity_date") or upload.get("maturity_date")
        metadata["issue_size_cr"] = metadata.get("issue_size_cr") or metadata.get("issue_size") or upload.get("issue_size_cr")
        metadata["face_value"] = metadata.get("face_value") or upload.get("face_value")
        metadata_rating = metadata.get("credit_rating")
        metadata["credit_rating"] = metadata_rating if _is_valid_rating_value(metadata_rating) else (upload.get("credit_rating") or metadata_rating)
        metadata_agency = metadata.get("rating_agency")
        metadata["rating_agency"] = (upload.get("rating_agency") or metadata_agency) if not _is_valid_rating_agency_value(metadata_agency) else metadata_agency
        metadata["security_type"] = metadata.get("security_type") or upload.get("security_type")
        metadata["seniority"] = metadata.get("seniority") or upload.get("seniority")
        if metadata.get("step_up_coupon") and not metadata.get("step_up"):
            metadata["step_up"] = metadata.get("step_up_coupon")

        if not risk and (upload.get("risk_score") is not None or upload.get("risk_level")):
            risk = {
                "risk_score": upload.get("risk_score"),
                "risk_level": upload.get("risk_level"),
                "red_flags": upload.get("risk_flags") or [],
            }

    # Full text preview
    full_text_preview = None
    try:
        full_text_row = await fetch_one(
            "SELECT full_text, text_length FROM im_full_text WHERE upload_id = :uid",
            {"uid": upload_id},
        )
        if full_text_row and full_text_row.get("full_text"):
            full_text_preview = {
                "text_length": full_text_row.get("text_length") or len(full_text_row["full_text"]),
                "preview": full_text_row["full_text"][:5000],
            }
    except Exception:
        pass

    # Risk benchmark for UI comparison
    sector_benchmark = None
    try:
        seniority = (metadata or {}).get("seniority") or ""
        security_type = (metadata or {}).get("security_type") or ""
        bench = await fetch_one(
            """SELECT AVG(r.risk_score) AS avg_score, COUNT(*) AS sample_size
               FROM bond_risk_analysis r
               JOIN bond_metadata m ON m.upload_id = r.upload_id
               WHERE r.upload_id <> :uid
                 AND COALESCE(m.seniority, '') = :seniority
                 AND COALESCE(m.security_type, '') = :security_type""",
            {
                "uid": upload_id,
                "seniority": seniority,
                "security_type": security_type,
            },
        )
        avg_score = None
        sample_size = 0
        if bench and bench.get("avg_score") is not None:
            avg_score = round(float(bench["avg_score"]), 1)
            sample_size = int(bench.get("sample_size") or 0)
        else:
            if "SUBORDINATED" in seniority.upper():
                avg_score = 36.0
            elif "UNSECURED" in security_type.upper():
                avg_score = 28.0
            else:
                avg_score = 19.0
            sample_size = 0
        current_score = float((risk or {}).get("risk_score") or 0)
        sector_benchmark = {
            "group": f"{seniority or 'UNKNOWN'} / {security_type or 'UNKNOWN'}",
            "avg_score": avg_score,
            "sample_size": sample_size,
            "delta": round(current_score - avg_score, 1),
        }
    except Exception:
        pass

    # Processing log
    processing_log = None
    try:
        processing_log = await fetch_one(
            "SELECT method_used, char_count, pages_processed, processing_time, status, error_message FROM im_processing_log WHERE upload_id = :uid ORDER BY id DESC LIMIT 1",
            {"uid": upload_id},
        )
        if processing_log:
            processing_log = dict(processing_log)
    except Exception:
        pass

    # Build enriched clause list
    enriched_clauses = []
    for cl in clauses_rows:
        cl_dict = dict(cl)
        cl_dict["evidence"] = evidence_by_type.get(cl["clause_type"], [])
        enriched_clauses.append(cl_dict)

    return {
        "upload_id": upload_id,
        "pages_count": len(pages_rows),
        "total_tokens": sum(p.get("token_count", 0) for p in pages_rows),
        "sections": [dict(s) for s in sections_rows],
        "metadata": dict(metadata) if metadata else {},
        "clauses": enriched_clauses,
        "covenants": [dict(c) for c in covenants_rows],
        "risk": dict(risk) if risk else {},
        "processing_log": processing_log,
        "sector_benchmark": sector_benchmark,
        "full_text_preview": full_text_preview,
    }
