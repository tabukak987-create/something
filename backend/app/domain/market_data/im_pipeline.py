"""
IM Processing Pipeline
======================
End-to-end pipeline for uploaded IM PDFs:
  1. Validate PDF (format, size, magic bytes)
  2. Compute SHA-256 checksum (duplicate detection)
  3. Store file to im_storage/{ISIN}/
  4. Extract text (pdfminer → PyPDF2 fallback)
  5. NLP parse: extract coupon, maturity, call/put, covenants, clauses
  6. IM Risk Engine: detect hidden risk clauses
  7. Store structured data in DB (bond_im_uploads + im_extracted_data)
  8. Link to universal_bond_master if ISIN exists
  9. Audit log every action

Usage:
    pipeline = IMPipeline()
    result = await pipeline.process_upload(isin, file_bytes, filename, uploaded_by="admin")
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.time_utils import utcnow_iso
from app.domain.portfolio.institutional_financial_core import validate_bond_record
from app.domain.portfolio.im_connectors import ManualUploadConnector, get_connector
from app.domain.portfolio.im_institutional_engine import run_institutional_analysis

logger = logging.getLogger("im_pipeline")

# ─── ISIN pattern ─────────────────────────────────────────────────────────────
ISIN_RE = re.compile(r'\b(IN[A-Z0-9]{10})\b')


# ═══════════════════════════════════════════════════════════════════════════════
# Text Extraction
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_text_from_pdf(data: bytes) -> Tuple[str, str]:
    """
    Extract text from PDF bytes.
    Returns (text, method) where method is 'pdfminer' | 'pypdf2' | 'failed'.
    """
    text = ""
    method = "failed"

    # Try pdfminer first (better accuracy)
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
        text = pdfminer_extract(io.BytesIO(data))
        if text and text.strip():
            method = "pdfminer"
            return text, method
    except Exception as e:
        logger.debug(f"pdfminer failed: {e}")

    # Fallback to PyPDF2
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(data))
        text = " ".join(p.extract_text() or "" for p in reader.pages)
        if text and text.strip():
            method = "pypdf2"
            return text, method
    except Exception as e:
        logger.debug(f"PyPDF2 failed: {e}")

    return text, method


# ═══════════════════════════════════════════════════════════════════════════════
# NLP Field Extraction from IM Text
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_coupon(text: str) -> Optional[float]:
    """Extract coupon rate from IM text."""
    patterns = [
        r'coupon\s*(?:rate)?\s*[:\-–]\s*(\d+\.?\d*)\s*%',
        r'(\d+\.?\d*)\s*%\s*(?:per\s*annum|p\.?a\.?|coupon)',
        r'interest\s*rate\s*[:\-–]\s*(\d+\.?\d*)\s*%',
        r'rate\s*of\s*interest\s*[:\-–]?\s*(\d+\.?\d*)\s*%',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 0.5 <= val <= 25:
                    return val
            except ValueError:
                continue
    return None


def _extract_maturity_date(text: str) -> Optional[str]:
    """Extract maturity date from IM text."""
    patterns = [
        r'maturity\s*date\s*[:\-–]\s*(\d{1,2}[\s/\-](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s/\-]\d{4})',
        r'maturity\s*date\s*[:\-–]\s*(\d{1,2}[\s/\-]\d{1,2}[\s/\-]\d{4})',
        r'redemption\s*date\s*[:\-–]\s*(\d{1,2}[\s/\-](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s/\-]\d{4})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _extract_issue_size(text: str) -> Optional[float]:
    """Extract issue size in crores."""
    patterns = [
        r'(?:issue\s*size|aggregate\s*(?:nominal|face)\s*value|total\s*issue)\s*[:\-–]?\s*(?:Rs\.?\s*|INR\s*)?(\d[\d,]*(?:\.\d+)?)\s*(?:cr|crore)',
        r'(?:Rs\.?\s*|INR\s*)(\d[\d,]*(?:\.\d+)?)\s*(?:cr|crore)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    return val
            except ValueError:
                continue
    return None


def _extract_face_value(text: str) -> Optional[float]:
    """Extract face value of bonds."""
    m = re.search(r'face\s*value\s*[:\-–]?\s*(?:Rs\.?\s*|INR\s*)(\d[\d,]*(?:\.\d+)?)', text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _extract_credit_rating(text: str) -> Optional[Tuple[str, str]]:
    """Extract credit rating and agency."""
    agencies = {
        r'CRISIL':  'CRISIL',
        r'ICRA':    'ICRA',
        r'CARE':    'CARE',
        r'India\s*Ratings|Ind[- ]Ra': 'India Ratings',
        r'Brickwork|BWR': 'Brickwork',
        r'Acuite':  'Acuite',
    }
    rating_pat = r'(AAA|AA\+|AA\-|AA|A\+|A\-|A|BBB\+|BBB\-|BBB|BB\+|BB\-|BB|B\+|B\-|B|C|D)'

    for agency_pat, agency_name in agencies.items():
        m = re.search(agency_pat + r'\s+' + rating_pat, text, re.IGNORECASE)
        if m:
            return m.group(len(m.groups())), agency_name  # rating, agency

    # Generic rating extraction
    m = re.search(r'(?:rated?|rating)\s*[:\-–]?\s*' + rating_pat, text, re.IGNORECASE)
    if m:
        return m.group(1), None
    return None


def _detect_option(text: str, option_type: str) -> bool:
    """Detect if call/put option exists in IM."""
    if option_type == "call":
        patterns = [r'call\s*option', r'callable', r'issuer.*right.*redeem', r'early\s*redemption']
    else:
        patterns = [r'put\s*option', r'puttable', r'investor.*right.*redemption']
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def _extract_security_type(text: str) -> Optional[str]:
    """Detect if secured or unsecured."""
    if re.search(r'\bunsecured\b', text, re.IGNORECASE):
        return "UNSECURED"
    if re.search(r'\bsecured\b', text, re.IGNORECASE):
        return "SECURED"
    return None


def _extract_seniority(text: str) -> Optional[str]:
    """Detect bond seniority."""
    if re.search(r'\bsubordinated?\b', text, re.IGNORECASE):
        return "SUBORDINATED"
    if re.search(r'\bsenior\b', text, re.IGNORECASE):
        return "SENIOR"
    if re.search(r'\bpari[- ]?passu\b', text, re.IGNORECASE):
        return "PARI_PASSU"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# IM Risk Clause Detection (IM-Layer Specific)
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_risk_clauses(text: str) -> Dict[str, Any]:
    """
    Detect hidden risk clauses in IM text.
    Returns structured risk data for storage.
    """
    text_lower = text.lower()
    result = {
        "cross_default_clause": 0,
        "change_of_control": 0,
        "acceleration_clause": 0,
        "negative_pledge": 0,
        "step_up_triggers": None,
        "event_of_default": None,
        "debt_covenants": None,
        "promoter_guarantee": None,
        "liquidity_risk": None,
        "security_charge_ranking": None,
        "covenants": [],
        "risk_factors": [],
    }

    # Cross-default clause
    if re.search(r'cross[- ]?default', text_lower):
        result["cross_default_clause"] = 1
        match = re.search(r'cross[- ]?default[^.]*\.', text, re.IGNORECASE)
        if match:
            result["risk_factors"].append({"type": "CROSS_DEFAULT", "text": match.group(0)[:300]})

    # Change of control
    if re.search(r'change\s*(of|in)\s*control', text_lower):
        result["change_of_control"] = 1
        match = re.search(r'change\s*(?:of|in)\s*control[^.]*\.', text, re.IGNORECASE)
        if match:
            result["risk_factors"].append({"type": "CHANGE_OF_CONTROL", "text": match.group(0)[:300]})

    # Acceleration clause
    if re.search(r'accelerat(?:ion|ed|e)', text_lower):
        result["acceleration_clause"] = 1
        match = re.search(r'accelerat[^\n.]*[.\n]', text, re.IGNORECASE)
        if match:
            result["risk_factors"].append({"type": "ACCELERATION", "text": match.group(0)[:300]})

    # Negative pledge
    if re.search(r'negative\s*pledge', text_lower):
        result["negative_pledge"] = 1
        match = re.search(r'negative\s*pledge[^.]*\.', text, re.IGNORECASE)
        if match:
            result["risk_factors"].append({"type": "NEGATIVE_PLEDGE", "text": match.group(0)[:300]})

    # Step-up triggers
    step_up_matches = re.findall(r'step[- ]?up[^.]*\.', text, re.IGNORECASE)
    if step_up_matches:
        result["step_up_triggers"] = json.dumps([m[:200] for m in step_up_matches[:3]])

    # Event of default
    eod_match = re.search(r'events?\s*of\s*default[:\s]*([^§]+?)(?:(?:\d+\.?\s*[A-Z])|$)', text, re.IGNORECASE)
    if eod_match:
        result["event_of_default"] = eod_match.group(0)[:1000]

    # Debt covenants
    cov_patterns = [
        (r'debt\s*(?:to\s*)?(?:equity|ebitda|service)\s*(?:coverage\s*)?ratio[^.]*\.', "DEBT_RATIO"),
        (r'interest\s*(?:coverage|service)\s*ratio[^.]*\.', "INTEREST_COVERAGE"),
        (r'dscr[^.]*\.', "DSCR"),
        (r'(?:current|liquidity)\s*ratio[^.]*\.', "LIQUIDITY"),
        (r'net\s*worth[^.]*(?:maintain|not\s*fall|covenant)[^.]*\.', "NET_WORTH"),
    ]
    covenants = []
    for pat, cov_type in cov_patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        for m in matches[:2]:
            covenants.append({"type": cov_type, "text": m[:300]})
    if covenants:
        result["covenants"] = covenants
        result["debt_covenants"] = json.dumps(covenants)

    # Promoter guarantee
    prom_match = re.search(r'(?:promoter|personal)\s*guarantee[^.]*\.', text, re.IGNORECASE)
    if prom_match:
        result["promoter_guarantee"] = prom_match.group(0)[:300]

    # Liquidity risk
    liq_match = re.search(r'liquidity\s*risk[^.]*\.', text, re.IGNORECASE)
    if liq_match:
        result["liquidity_risk"] = liq_match.group(0)[:300]

    # Security/charge ranking
    sec_match = re.search(r'(?:first|second|pari[- ]?passu)\s*(?:ranking\s*)?charge[^.]*\.', text, re.IGNORECASE)
    if sec_match:
        result["security_charge_ranking"] = sec_match.group(0)[:300]

    return result


def _compute_im_risk_score(extracted: Dict, risk_data: Dict) -> Tuple[float, str, List[str]]:
    """
    Compute IM-level risk score based on extracted fields & risk clauses.
    Returns (score 0-100, level, list of flag strings).
    """
    score = 0.0
    flags = []

    # Unsecured = higher risk
    if extracted.get("security_type") == "UNSECURED":
        score += 15
        flags.append("UNSECURED_ISSUE")

    # Subordinated = higher risk
    if extracted.get("seniority") == "SUBORDINATED":
        score += 12
        flags.append("SUBORDINATED_SENIORITY")

    # No cross-default = weakness
    if not risk_data.get("cross_default_clause"):
        score += 8
        flags.append("NO_CROSS_DEFAULT_CLAUSE")

    # No negative pledge = weakness
    if not risk_data.get("negative_pledge"):
        score += 5
        flags.append("NO_NEGATIVE_PLEDGE")

    # Acceleration clause present = some risk (can trigger cascade)
    if risk_data.get("acceleration_clause"):
        score += 4
        flags.append("ACCELERATION_CLAUSE_PRESENT")

    # No covenants found = weakness
    if not risk_data.get("covenants"):
        score += 10
        flags.append("NO_COVENANTS_DETECTED")

    # No event of default section found = weakness
    if not risk_data.get("event_of_default"):
        score += 8
        flags.append("NO_EVENT_OF_DEFAULT_SECTION")

    # High coupon might indicate risk
    coupon = extracted.get("coupon")
    if coupon and coupon > 12:
        score += 6
        flags.append("HIGH_COUPON_RISK")

    # No promoter guarantee
    if not risk_data.get("promoter_guarantee"):
        score += 3
        flags.append("NO_PROMOTER_GUARANTEE")

    # Call option without investor protection
    if extracted.get("call_option") and not extracted.get("put_option"):
        score += 5
        flags.append("CALL_WITHOUT_PUT_OPTION")

    # Determine risk level
    if score >= 50:
        level = "CRITICAL"
    elif score >= 35:
        level = "HIGH"
    elif score >= 20:
        level = "MEDIUM"
    else:
        level = "LOW"

    return round(score, 1), level, flags


# ═══════════════════════════════════════════════════════════════════════════════
# IM Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

class IMPipeline:
    """
    End-to-end IM document processing pipeline.
    Steps: Validate → Checksum → Store → Extract → NLP → Risk → DB → Link → Audit
    """

    def __init__(self, connector=None):
        self.connector = connector or ManualUploadConnector()

    @staticmethod
    def _validate_extracted_bond_fields(
        isin: str,
        issuer_name: str,
        extracted: Dict[str, Any],
        fallback_rating: Optional[str] = None,
        fallback_maturity: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any], List[str], List[str]]:
        """
        Apply institutional validation to IM-extracted financial fields before writes.
        Uses fallback values where IM parsing is partial.
        """
        record = {
            "isin": isin,
            "issuer_name": issuer_name,
            "credit_rating": extracted.get("credit_rating") or fallback_rating,
            "coupon_rate": extracted.get("coupon"),
            "maturity_date": extracted.get("maturity_date") or fallback_maturity,
            "sector": extracted.get("sector"),
        }
        validation = validate_bond_record(record)
        return validation.is_valid, validation.normalized, validation.errors, validation.warnings

    # ─── Audit Helper ─────────────────────────────────────────────────────

    async def _audit_log(self, upload_id: int, isin: str, action: str, actor: str = "system", details: Any = None):
        """Write to im_audit_log."""
        now = utcnow_iso()
        await execute_query(
            """INSERT INTO im_audit_log (upload_id, isin, action, actor, details, created_at)
               VALUES (:uid, :isin, :action, :actor, :details, :now)""",
            {
                "uid": upload_id or 0,
                "isin": isin,
                "action": action,
                "actor": actor,
                "details": json.dumps(details) if details else None,
                "now": now,
            },
        )

    # ─── Duplicate Check ──────────────────────────────────────────────────

    async def _check_duplicate(self, checksum: str) -> Optional[Dict]:
        """Check if a file with this checksum already exists."""
        row = await fetch_one(
            "SELECT id, isin, version, is_active FROM bond_im_uploads WHERE checksum = :cs",
            {"cs": checksum},
        )
        return row

    # ─── Version Control ──────────────────────────────────────────────────

    async def _get_next_version(self, isin: str) -> int:
        """Get next version number for this ISIN."""
        row = await fetch_one(
            "SELECT COALESCE(MAX(version), 0) AS max_ver FROM bond_im_uploads WHERE isin = :isin",
            {"isin": isin},
        )
        return (row.get("max_ver", 0) if row else 0) + 1

    async def _deactivate_old_versions(self, isin: str):
        """Mark all old versions as inactive."""
        await execute_query(
            "UPDATE bond_im_uploads SET is_active = 0, updated_at = :now WHERE isin = :isin AND is_active = 1",
            {"isin": isin, "now": utcnow_iso()},
        )

    # ─── Main Pipeline ────────────────────────────────────────────────────

    async def process_upload(
        self,
        isin: str,
        file_bytes: bytes,
        filename: str,
        uploaded_by: str = "admin",
        issuer_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Full processing pipeline for an uploaded IM PDF.

        Returns dict with:
          - upload_id, isin, version, checksum
          - extracted fields (coupon, maturity, etc.)
          - risk_score, risk_level, risk_flags
          - status: 'success' | 'duplicate' | 'error'
        """
        isin = isin.upper()
        result = {"isin": isin, "status": "error", "errors": []}
        now = utcnow_iso()

        # Step 1: Validate
        validation = await self.connector.validate_document(file_bytes, filename)
        if not validation["valid"]:
            result["errors"] = validation["errors"]
            await self._audit_log(0, isin, "UPLOAD_VALIDATION_FAILED", uploaded_by, {"errors": validation["errors"]})
            return result

        checksum = validation["metadata"]["checksum"]

        # Step 2: Duplicate check
        dup = await self._check_duplicate(checksum)
        if dup:
            result["status"] = "duplicate"
            result["errors"] = [f"Identical file already uploaded for {dup.get('isin')} (v{dup.get('version')})"]
            result["existing_id"] = dup.get("id")
            await self._audit_log(dup.get("id", 0), isin, "DUPLICATE_DETECTED", uploaded_by, {"checksum": checksum})
            return result

        # Step 3: Version control
        version = await self._get_next_version(isin)
        if version > 1:
            await self._deactivate_old_versions(isin)

        # Step 4: Store file
        store_result = await self.connector.store_document(isin, file_bytes, filename, version)
        file_path = store_result["file_path"]

        # Step 5: Extract text
        text, extract_method = _extract_text_from_pdf(file_bytes)
        extraction_status = "DONE" if text.strip() else "FAILED"

        # Step 6: NLP field extraction
        extracted = {}
        if text.strip():
            extracted["coupon"] = _extract_coupon(text)
            extracted["maturity_date"] = _extract_maturity_date(text)
            extracted["issue_size_cr"] = _extract_issue_size(text)
            extracted["face_value"] = _extract_face_value(text)
            extracted["call_option"] = 1 if _detect_option(text, "call") else 0
            extracted["put_option"] = 1 if _detect_option(text, "put") else 0
            extracted["security_type"] = _extract_security_type(text)
            extracted["seniority"] = _extract_seniority(text)

            rating_result = _extract_credit_rating(text)
            if rating_result:
                extracted["credit_rating"], extracted["rating_agency"] = rating_result

            # Auto-detect ISIN from text if different ISINs found
            isins_in_text = ISIN_RE.findall(text)
            if isins_in_text and isin not in isins_in_text:
                extracted["isins_detected"] = list(set(isins_in_text))

        # Resolve issuer name
        if not issuer_name:
            bond = await fetch_one("SELECT issuer_name FROM universal_bond_master WHERE isin = :isin", {"isin": isin})
            issuer_name = bond.get("issuer_name", "") if bond else ""

        bond_master_snapshot = await fetch_one(
            """SELECT maturity_date, credit_rating
               FROM universal_bond_master
               WHERE isin = :isin""",
            {"isin": isin},
        )

        is_valid, normalized, val_errors, _val_warnings = self._validate_extracted_bond_fields(
            isin=isin,
            issuer_name=issuer_name or "",
            extracted=extracted,
            fallback_rating=(bond_master_snapshot or {}).get("credit_rating"),
            fallback_maturity=(bond_master_snapshot or {}).get("maturity_date"),
        )
        if not is_valid:
            result["errors"] = [f"Institutional validation failed: {'; '.join(val_errors)}"]
            await self._audit_log(0, isin, "UPLOAD_VALIDATION_FAILED", uploaded_by, {"errors": val_errors})
            return result

        # Persist normalized fields from institutional validator.
        if normalized.get("coupon_rate") is not None:
            extracted["coupon"] = normalized.get("coupon_rate")
        if normalized.get("maturity_date"):
            extracted["maturity_date"] = normalized.get("maturity_date")
        if normalized.get("credit_rating"):
            extracted["credit_rating"] = normalized.get("credit_rating")
        if normalized.get("sector"):
            extracted["sector"] = normalized.get("sector")

        # Step 7: Risk analysis on extracted text
        risk_data = _detect_risk_clauses(text) if text.strip() else {}
        risk_score, risk_level, risk_flags = (0, "UNKNOWN", [])
        if text.strip():
            risk_score, risk_level, risk_flags = _compute_im_risk_score(extracted, risk_data)

        # Step 8: Insert into bond_im_uploads
        upload_params = {
            "isin": isin,
            "issuer_name": issuer_name or "",
            "file_path": file_path,
            "file_name": filename,
            "file_size": len(file_bytes),
            "checksum": checksum,
            "version": version,
            "is_active": 1,
            "upload_source": self.connector.source_name,
            "uploaded_by": uploaded_by,
            "coupon": extracted.get("coupon"),
            "maturity_date": extracted.get("maturity_date"),
            "issue_size_cr": extracted.get("issue_size_cr"),
            "face_value": extracted.get("face_value"),
            "credit_rating": extracted.get("credit_rating"),
            "rating_agency": extracted.get("rating_agency"),
            "call_option": extracted.get("call_option", 0),
            "put_option": extracted.get("put_option", 0),
            "security_type": extracted.get("security_type"),
            "seniority": extracted.get("seniority"),
            "extraction_status": extraction_status,
            "extracted_text": text[:50000] if text else None,
            "text_length": len(text) if text else 0,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "risk_flags": json.dumps(risk_flags),
            "risk_run_at": now,
            "created_at": now,
            "updated_at": now,
        }

        # Insert and get ID
        await execute_query(
            """INSERT INTO bond_im_uploads
               (isin, issuer_name, file_path, file_name, file_size, checksum,
                version, is_active, upload_source, uploaded_by,
                coupon, maturity_date, issue_size_cr, face_value,
                credit_rating, rating_agency, call_option, put_option,
                security_type, seniority,
                extraction_status, extracted_text, text_length,
                risk_score, risk_level, risk_flags, risk_run_at,
                created_at, updated_at)
               VALUES
               (:isin, :issuer_name, :file_path, :file_name, :file_size, :checksum,
                :version, :is_active, :upload_source, :uploaded_by,
                :coupon, :maturity_date, :issue_size_cr, :face_value,
                :credit_rating, :rating_agency, :call_option, :put_option,
                :security_type, :seniority,
                :extraction_status, :extracted_text, :text_length,
                :risk_score, :risk_level, :risk_flags, :risk_run_at,
                :created_at, :updated_at)""",
            upload_params,
        )

        # Get the inserted ID
        upload_row = await fetch_one(
            "SELECT id FROM bond_im_uploads WHERE isin = :isin AND checksum = :cs",
            {"isin": isin, "cs": checksum},
        )
        upload_id = upload_row["id"] if upload_row else 0

        # Step 9: Insert into im_extracted_data
        if text.strip():
            extract_params = {
                "upload_id": upload_id,
                "isin": isin,
                "coupon": extracted.get("coupon"),
                "maturity_date": extracted.get("maturity_date"),
                "call_option": extracted.get("call_option", 0),
                "put_option": extracted.get("put_option", 0),
                "seniority": extracted.get("seniority"),
                "security_type": extracted.get("security_type"),
                "covenants": json.dumps(risk_data.get("covenants", [])),
                "risk_factors": json.dumps(risk_data.get("risk_factors", [])),
                "cross_default_clause": risk_data.get("cross_default_clause", 0),
                "change_of_control": risk_data.get("change_of_control", 0),
                "acceleration_clause": risk_data.get("acceleration_clause", 0),
                "negative_pledge": risk_data.get("negative_pledge", 0),
                "step_up_triggers": risk_data.get("step_up_triggers"),
                "event_of_default": risk_data.get("event_of_default"),
                "debt_covenants": risk_data.get("debt_covenants"),
                "promoter_guarantee": risk_data.get("promoter_guarantee"),
                "liquidity_risk": risk_data.get("liquidity_risk"),
                "security_charge_ranking": risk_data.get("security_charge_ranking"),
                "extracted_text": text[:50000],
                    "created_at": now,
            }
            await execute_query(
                """INSERT INTO im_extracted_data
                   (upload_id, isin, coupon, maturity_date, call_option, put_option,
                    seniority, security_type, covenants, risk_factors,
                    cross_default_clause, change_of_control, acceleration_clause,
                    negative_pledge, step_up_triggers, event_of_default,
                    debt_covenants, promoter_guarantee, liquidity_risk,
                    security_charge_ranking, extracted_text, created_at)
                   VALUES
                   (:upload_id, :isin, :coupon, :maturity_date, :call_option, :put_option,
                    :seniority, :security_type, :covenants, :risk_factors,
                    :cross_default_clause, :change_of_control, :acceleration_clause,
                    :negative_pledge, :step_up_triggers, :event_of_default,
                    :debt_covenants, :promoter_guarantee, :liquidity_risk,
                       :security_charge_ranking, :extracted_text, :created_at)""",
                extract_params,
            )

        # Step 10: Audit log
        await self._audit_log(upload_id, isin, "UPLOAD", uploaded_by, {
            "filename": filename,
            "version": version,
            "checksum": checksum,
            "extraction_status": extraction_status,
            "risk_score": risk_score,
            "risk_level": risk_level,
        })

        # Step 11: Also push key data to bond_im_data for heuristics engine
        if text.strip():
            await self._sync_to_im_data(isin, text, extracted, risk_data)

        # Step 12: Run institutional-grade analysis (pages, sections, clauses, covenants, risk)
        institutional = {}
        if text.strip() and upload_id:
            try:
                institutional = await run_institutional_analysis(
                    upload_id=upload_id,
                    isin=isin,
                    file_bytes=file_bytes,
                    credit_rating=extracted.get("credit_rating"),
                )
                if institutional.get("status") == "success":
                    # Use institutional risk score as the primary score
                    inst_risk = institutional.get("risk", {})
                    risk_score = inst_risk.get("risk_score", risk_score)
                    risk_level = inst_risk.get("risk_level", risk_level)
                    inst_flags = inst_risk.get("red_flags", [])
                    # Update the uploads table with institutional score
                    await execute_query(
                        """UPDATE bond_im_uploads
                           SET risk_score = :score, risk_level = :level,
                               risk_flags = :flags, risk_run_at = :rtime,
                               updated_at = :now
                           WHERE id = :id""",
                        {
                            "id": upload_id,
                            "score": risk_score,
                            "level": risk_level,
                            "flags": json.dumps(inst_flags),
                            "rtime": now,
                            "now": now,
                        },
                    )
                    risk_flags = inst_flags
            except Exception as e:
                logger.warning(f"Institutional analysis failed for {isin}: {e}")
                institutional = {"status": "error", "errors": [str(e)]}

        result.update({
            "status": "success",
            "upload_id": upload_id,
            "version": version,
            "checksum": checksum,
            "file_path": file_path,
            "extraction_status": extraction_status,
            "extraction_method": extract_method,
            "text_length": len(text),
            "extracted": extracted,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "risk_flags": risk_flags,
            "risk_clauses": {
                "cross_default": bool(risk_data.get("cross_default_clause")),
                "change_of_control": bool(risk_data.get("change_of_control")),
                "acceleration": bool(risk_data.get("acceleration_clause")),
                "negative_pledge": bool(risk_data.get("negative_pledge")),
                "covenants_found": len(risk_data.get("covenants", [])),
            },
            "institutional": institutional if institutional.get("status") == "success" else None,
        })
        return result

    async def _sync_to_im_data(self, isin: str, text: str, extracted: Dict, risk_data: Dict):
        """
        Push extracted data to the existing bond_im_data table so the
        heuristics engine (H1-H24) can use it.
        """
        d = {"isin": isin, "im_full_text": text[:50000]}

        if extracted.get("security_type"):
            if extracted["security_type"] == "UNSECURED":
                d["charge_type"] = "UNSECURED"
            else:
                d["charge_type"] = "FIRST"

        if extracted.get("seniority"):
            d["security_structure"] = extracted["seniority"]

        if risk_data.get("event_of_default"):
            d["default_clauses"] = risk_data["event_of_default"][:500]

        if risk_data.get("cross_default_clause"):
            d["cross_default_triggers"] = "Cross-default clause detected in IM"

        if risk_data.get("covenants"):
            fin_covs = [c["text"] for c in risk_data["covenants"] if c["type"] in ("DEBT_RATIO", "INTEREST_COVERAGE", "DSCR")]
            if fin_covs:
                d["covenants_financial"] = json.dumps(fin_covs)

        d["updated_at"] = utcnow_iso()[:10]
        d["im_parsed_at"] = utcnow_iso()[:10]

        existing = await fetch_one("SELECT id FROM bond_im_data WHERE isin = :isin", {"isin": isin})
        if existing:
            sets = ", ".join(f"{k} = :{k}" for k in d if k != "isin")
            await execute_query(f"UPDATE bond_im_data SET {sets} WHERE isin = :isin", d)
        else:
            d["created_at"] = utcnow_iso()[:10]
            cols = ", ".join(d.keys())
            vals = ", ".join(f":{k}" for k in d.keys())
            await execute_query(f"INSERT INTO bond_im_data ({cols}) VALUES ({vals})", d)

    # ─── Reprocess Pipeline ───────────────────────────────────────────────

    async def reprocess_upload(self, upload_id: int, actor: str = "admin") -> Dict[str, Any]:
        """Re-run extraction & risk analysis on an existing upload."""
        row = await fetch_one("SELECT * FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
        if not row:
            return {"status": "error", "errors": ["Upload not found"]}

        isin = row["isin"]
        file_path_str = row["file_path"]
        from app.domain.portfolio.im_connectors import IM_STORAGE_DIR
        full_path = IM_STORAGE_DIR / file_path_str

        if not full_path.exists():
            return {"status": "error", "errors": ["File not found on disk"]}

        file_bytes = full_path.read_bytes()

        # Re-extract
        text, method = _extract_text_from_pdf(file_bytes)
        extraction_status = "DONE" if text.strip() else "FAILED"

        extracted = {}
        risk_data = {}
        risk_score, risk_level, risk_flags = 0, "UNKNOWN", []

        if text.strip():
            extracted["coupon"] = _extract_coupon(text)
            extracted["maturity_date"] = _extract_maturity_date(text)
            extracted["issue_size_cr"] = _extract_issue_size(text)
            extracted["face_value"] = _extract_face_value(text)
            extracted["call_option"] = 1 if _detect_option(text, "call") else 0
            extracted["put_option"] = 1 if _detect_option(text, "put") else 0
            extracted["security_type"] = _extract_security_type(text)
            extracted["seniority"] = _extract_seniority(text)
            rating_result = _extract_credit_rating(text)
            if rating_result:
                extracted["credit_rating"], extracted["rating_agency"] = rating_result

            bond_master_snapshot = await fetch_one(
                """SELECT issuer_name, maturity_date, credit_rating
                   FROM universal_bond_master
                   WHERE isin = :isin""",
                {"isin": isin},
            )
            is_valid, normalized, val_errors, _val_warnings = self._validate_extracted_bond_fields(
                isin=isin,
                issuer_name=(bond_master_snapshot or {}).get("issuer_name") or (row.get("issuer_name") or ""),
                extracted=extracted,
                fallback_rating=(bond_master_snapshot or {}).get("credit_rating"),
                fallback_maturity=(bond_master_snapshot or {}).get("maturity_date"),
            )
            if not is_valid:
                await self._audit_log(upload_id, isin, "REPROCESS_VALIDATION_FAILED", actor, {"errors": val_errors})
                return {"status": "error", "upload_id": upload_id, "errors": [f"Institutional validation failed: {'; '.join(val_errors)}"]}

            if normalized.get("coupon_rate") is not None:
                extracted["coupon"] = normalized.get("coupon_rate")
            if normalized.get("maturity_date"):
                extracted["maturity_date"] = normalized.get("maturity_date")
            if normalized.get("credit_rating"):
                extracted["credit_rating"] = normalized.get("credit_rating")

            risk_data = _detect_risk_clauses(text)
            risk_score, risk_level, risk_flags = _compute_im_risk_score(extracted, risk_data)

        # Update bond_im_uploads
        await execute_query(
            """UPDATE bond_im_uploads
               SET extraction_status = :status, extracted_text = :text, text_length = :tlen,
                   coupon = :coupon, maturity_date = :maturity, call_option = :call, put_option = :put,
                   security_type = :sec, seniority = :sen, credit_rating = :rating, rating_agency = :agency,
                   risk_score = :rscore, risk_level = :rlevel, risk_flags = :rflags, risk_run_at = :rtime,
                   updated_at = :now
               WHERE id = :id""",
            {
                "id": upload_id, "status": extraction_status,
                "text": text[:50000] if text else None, "tlen": len(text),
                "coupon": extracted.get("coupon"), "maturity": extracted.get("maturity_date"),
                "call": extracted.get("call_option", 0), "put": extracted.get("put_option", 0),
                "sec": extracted.get("security_type"), "sen": extracted.get("seniority"),
                "rating": extracted.get("credit_rating"), "agency": extracted.get("rating_agency"),
                "rscore": risk_score, "rlevel": risk_level,
                "rflags": json.dumps(risk_flags), "rtime": utcnow_iso(),
                "now": utcnow_iso(),
            },
        )

        # Update im_extracted_data
        if text.strip():
            await execute_query("DELETE FROM im_extracted_data WHERE upload_id = :uid", {"uid": upload_id})
            extract_params = {
                "upload_id": upload_id, "isin": isin,
                "coupon": extracted.get("coupon"), "maturity_date": extracted.get("maturity_date"),
                "call_option": extracted.get("call_option", 0), "put_option": extracted.get("put_option", 0),
                "seniority": extracted.get("seniority"), "security_type": extracted.get("security_type"),
                "covenants": json.dumps(risk_data.get("covenants", [])),
                "risk_factors": json.dumps(risk_data.get("risk_factors", [])),
                "cross_default_clause": risk_data.get("cross_default_clause", 0),
                "change_of_control": risk_data.get("change_of_control", 0),
                "acceleration_clause": risk_data.get("acceleration_clause", 0),
                "negative_pledge": risk_data.get("negative_pledge", 0),
                "step_up_triggers": risk_data.get("step_up_triggers"),
                "event_of_default": risk_data.get("event_of_default"),
                "debt_covenants": risk_data.get("debt_covenants"),
                "promoter_guarantee": risk_data.get("promoter_guarantee"),
                "liquidity_risk": risk_data.get("liquidity_risk"),
                "security_charge_ranking": risk_data.get("security_charge_ranking"),
                "extracted_text": text[:50000],
            }
            await execute_query(
                """INSERT INTO im_extracted_data
                   (upload_id, isin, coupon, maturity_date, call_option, put_option,
                    seniority, security_type, covenants, risk_factors,
                    cross_default_clause, change_of_control, acceleration_clause,
                    negative_pledge, step_up_triggers, event_of_default,
                    debt_covenants, promoter_guarantee, liquidity_risk,
                    security_charge_ranking, extracted_text, created_at)
                   VALUES
                   (:upload_id, :isin, :coupon, :maturity_date, :call_option, :put_option,
                    :seniority, :security_type, :covenants, :risk_factors,
                    :cross_default_clause, :change_of_control, :acceleration_clause,
                    :negative_pledge, :step_up_triggers, :event_of_default,
                    :debt_covenants, :promoter_guarantee, :liquidity_risk,
                        :security_charge_ranking, :extracted_text, :now)""",
                extract_params,
            )
            await self._sync_to_im_data(isin, text, extracted, risk_data)

        await self._audit_log(upload_id, isin, "REPROCESS", actor, {
            "extraction_status": extraction_status,
            "risk_score": risk_score,
            "risk_level": risk_level,
        })

        return {
            "status": "success",
            "upload_id": upload_id,
            "extraction_status": extraction_status,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "risk_flags": risk_flags,
            "extracted": extracted,
        }

    # ─── Integrity Check ──────────────────────────────────────────────────

    async def check_integrity(self, upload_id: int) -> Dict[str, Any]:
        """Cross-check uploaded IM data against universal_bond_master."""
        upload = await fetch_one("SELECT * FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
        if not upload:
            return {"status": "error", "errors": ["Upload not found"]}

        isin = upload["isin"]
        bond = await fetch_one("SELECT * FROM universal_bond_master WHERE isin = :isin", {"isin": isin})
        mismatches = []

        if not bond:
            mismatches.append({"field": "isin", "issue": "ISIN not found in bond master"})
        else:
            # Check coupon match
            if upload.get("coupon") and bond.get("coupon_rate"):
                if abs(upload["coupon"] - (bond["coupon_rate"] or 0)) > 0.1:
                    mismatches.append({
                        "field": "coupon",
                        "im_value": upload["coupon"],
                        "master_value": bond.get("coupon_rate"),
                        "issue": "Coupon mismatch between IM and bond master",
                    })

            # Check security type
            if upload.get("security_type") and bond.get("secured_unsecured"):
                sec_master = bond.get("secured_unsecured", "").upper()
                if upload["security_type"] not in sec_master:
                    mismatches.append({
                        "field": "security_type",
                        "im_value": upload["security_type"],
                        "master_value": sec_master,
                        "issue": "Security type mismatch",
                    })

        # Check file integrity (checksum)
        from app.domain.portfolio.im_connectors import IM_STORAGE_DIR
        full_path = IM_STORAGE_DIR / upload["file_path"]
        file_ok = full_path.exists()
        checksum_ok = False
        if file_ok:
            actual_cs = ManualUploadConnector.compute_checksum(full_path.read_bytes())
            checksum_ok = actual_cs == upload["checksum"]
            if not checksum_ok:
                mismatches.append({"field": "checksum", "issue": "File checksum mismatch — file may be corrupted"})

        await self._audit_log(upload_id, isin, "INTEGRITY_CHECK", "system", {
            "file_exists": file_ok, "checksum_ok": checksum_ok, "mismatches": len(mismatches),
        })

        return {
            "status": "ok",
            "upload_id": upload_id,
            "isin": isin,
            "file_exists": file_ok,
            "checksum_ok": checksum_ok,
            "mismatches": mismatches,
            "integrity_score": "PASS" if not mismatches else "WARN" if len(mismatches) <= 2 else "FAIL",
        }
