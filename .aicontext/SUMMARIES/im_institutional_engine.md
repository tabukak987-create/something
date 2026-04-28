# Summary: backend/app/domain/portfolio/im_institutional_engine.py (81KB)

## Overview
High-performance pipeline for extracting structured data and detecting legal clauses in institutional Investment Memorandum (IM) PDF documents.

## Core Pipeline Steps

### 1. Hybrid PDF Extraction
- Uses a tiered approach: **PyMuPDF (Fitz)** → **pdfminer** → **PyPDF2** → **OCR Fallback**.
- OCR supports both **Tesseract** (pytesseract) and **PaddleOCR**.
- Normalizes text by cleaning whitespace and form-feeds.

### 2. Section Detection
- Identifies canonical IM sections using robust regex variation patterns:
    - *Risk Factors*, *Events of Default*, *Covenants*, *Security*, *Redemption*, *Terms of Issue*, etc.

### 3. Bond Metadata Extraction
- Regex-based extraction for:
    - **ISIN**, **Issuer Name**, **Issue Size**, **Currency**.
    - **Coupon Rate** (Fixed/Floating/Zero), **Maturity Date**, **Issue Date**.
    - **Credit Rating**, **Rating Agency**, **Trustee**, **Listing Exchange**.
    - **Governing Law**, **Security Type** (Secured/Unsecured), **Seniority**.

### 4. Clause Detection Agents
- Specialized logic to detect critical legal clauses with confidence scores:
    - **ACCELERATION**: Immediate repayment triggers.
    - **CROSS_DEFAULT**: Default on other debt triggers.
    - **NEGATIVE_PLEDGE**: Restriction on pledging assets to others.
    - **CHANGE_OF_CONTROL**: Ownership change consequences.
    - **SECURITY/GUARANTEE**: Collateral and third-party backing details.

## Internal Utilities
- `_ensure_institutional_schema_compat()`: Automates DB schema migrations for `bond_metadata`.
- `_is_valid_rating_value()`: Validates rating strings (e.g., 'AAA', 'AA+').
- `detect_sections()`: Maps extracted text to page ranges for high-signal retrieval.
