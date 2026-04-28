"""Canonical instrument category and subtype classification helpers."""

from __future__ import annotations

from typing import Any, Mapping

INSTRUMENT_TYPE_SDS = "SDS"
INSTRUMENT_TYPE_BOND = "BOND"
INSTRUMENT_TYPE_EQUITY = "EQUITY"

INSTRUMENT_SUBTYPE_STATIC = "STATIC"
INSTRUMENT_SUBTYPE_BOND_CENTRAL = "CENTRAL"
INSTRUMENT_SUBTYPE_BOND_STATE = "STATE"
INSTRUMENT_SUBTYPE_BOND_PSU = "PSU"
INSTRUMENT_SUBTYPE_BOND_PRIVATE = "PRIVATE"
INSTRUMENT_SUBTYPE_EQUITY_MARKET = "MARKET"
INSTRUMENT_SUBTYPE_EQUITY_NAV = "NAV"

INSTRUMENT_CATEGORY_STATE_CENTRAL = "STATE_CENTRAL"
INSTRUMENT_CATEGORY_PSU = "PSU"
INSTRUMENT_CATEGORY_PRIVATE = "PRIVATE"
INSTRUMENT_CATEGORY_EQUITY = "EQUITY"

PORTFOLIO_CATEGORY_SDS = "SDS"
PORTFOLIO_CATEGORY_CENTRAL_STATE = "CENTRAL_STATE"
PORTFOLIO_CATEGORY_PSU = "PSU"
PORTFOLIO_CATEGORY_PRIVATE = "PRIVATE"
PORTFOLIO_CATEGORY_EQUITY = "EQUITY"

PORTFOLIO_CATEGORY_ORDER = {
    PORTFOLIO_CATEGORY_SDS: 0,
    PORTFOLIO_CATEGORY_CENTRAL_STATE: 1,
    PORTFOLIO_CATEGORY_PSU: 2,
    PORTFOLIO_CATEGORY_PRIVATE: 3,
    PORTFOLIO_CATEGORY_EQUITY: 4,
}

PORTFOLIO_CATEGORY_DISPLAY_NAME = {
    PORTFOLIO_CATEGORY_SDS: "SDS",
    PORTFOLIO_CATEGORY_CENTRAL_STATE: "Central & State",
    PORTFOLIO_CATEGORY_PSU: "PSU",
    PORTFOLIO_CATEGORY_PRIVATE: "Private",
    PORTFOLIO_CATEGORY_EQUITY: "Equity",
}

PORTFOLIO_CATEGORY_TAG = {
    PORTFOLIO_CATEGORY_SDS: "Government-backed / Non-tradable",
    PORTFOLIO_CATEGORY_CENTRAL_STATE: "Sovereign / State Backed",
    PORTFOLIO_CATEGORY_PSU: "PSU",
    PORTFOLIO_CATEGORY_PRIVATE: "Corporate",
    PORTFOLIO_CATEGORY_EQUITY: "Equity / Market-based",
}

PORTFOLIO_CATEGORY_RISK_LABEL = {
    PORTFOLIO_CATEGORY_SDS: "Locked / Static",
    PORTFOLIO_CATEGORY_CENTRAL_STATE: "Very Low Risk",
    PORTFOLIO_CATEGORY_PSU: "Low to Moderate Risk",
    PORTFOLIO_CATEGORY_PRIVATE: "Moderate to High Risk",
    PORTFOLIO_CATEGORY_EQUITY: "Market Risk",
}

PORTFOLIO_CATEGORY_SECTION_TITLE = {
    PORTFOLIO_CATEGORY_SDS: "Static Reserve / SDS",
    PORTFOLIO_CATEGORY_CENTRAL_STATE: "Safe Core Allocation",
    PORTFOLIO_CATEGORY_PSU: "PSU Opportunities",
    PORTFOLIO_CATEGORY_PRIVATE: "Corporate Credit Opportunities",
    PORTFOLIO_CATEGORY_EQUITY: "Equity Opportunities",
}

INSTRUMENT_SUBTYPE_GSEC = "GSEC"
INSTRUMENT_SUBTYPE_SDL = "SDL"
INSTRUMENT_SUBTYPE_PSU_BOND = "PSU_BOND"
INSTRUMENT_SUBTYPE_NBFC = "NBFC"
INSTRUMENT_SUBTYPE_CORPORATE = "CORPORATE"
INSTRUMENT_SUBTYPE_PERPETUAL = "PERPETUAL"
INSTRUMENT_SUBTYPE_EQUITY_SHARE = "EQUITY_SHARE"
INSTRUMENT_SUBTYPE_CONVERTIBLE = "CONVERTIBLE"
INSTRUMENT_SUBTYPE_SDS = "SDS"

CATEGORY_ORDER = {
    INSTRUMENT_TYPE_SDS: 0,
    INSTRUMENT_CATEGORY_STATE_CENTRAL: 0,
    INSTRUMENT_CATEGORY_PSU: 1,
    INSTRUMENT_CATEGORY_PRIVATE: 2,
    INSTRUMENT_CATEGORY_EQUITY: 3,
    INSTRUMENT_TYPE_BOND: 1,
    INSTRUMENT_TYPE_EQUITY: 3,
}

CATEGORY_DISPLAY_NAME = {
    INSTRUMENT_TYPE_SDS: "SDS",
    INSTRUMENT_TYPE_BOND: "Bond",
    INSTRUMENT_TYPE_EQUITY: "Equity",
    INSTRUMENT_CATEGORY_STATE_CENTRAL: "Central & State",
    INSTRUMENT_CATEGORY_PSU: "PSU",
    INSTRUMENT_CATEGORY_PRIVATE: "Private",
    INSTRUMENT_CATEGORY_EQUITY: "Equity",
}

CATEGORY_TAG = {
    INSTRUMENT_TYPE_SDS: "Government-backed / Non-tradable",
    INSTRUMENT_TYPE_BOND: "Market-linked debt",
    INSTRUMENT_TYPE_EQUITY: "NAV / Market-based",
    INSTRUMENT_CATEGORY_STATE_CENTRAL: "Sovereign / State Backed",
    INSTRUMENT_CATEGORY_PSU: "PSU",
    INSTRUMENT_CATEGORY_PRIVATE: "Corporate",
    INSTRUMENT_CATEGORY_EQUITY: "Equity",
}

CATEGORY_RISK_LABEL = {
    INSTRUMENT_TYPE_SDS: "Locked / Static",
    INSTRUMENT_TYPE_BOND: "Interest-rate and credit risk",
    INSTRUMENT_TYPE_EQUITY: "Market risk",
    INSTRUMENT_CATEGORY_STATE_CENTRAL: "Very Low Risk",
    INSTRUMENT_CATEGORY_PSU: "Low to Moderate Risk",
    INSTRUMENT_CATEGORY_PRIVATE: "Moderate to High Risk",
    INSTRUMENT_CATEGORY_EQUITY: "Market Risk",
}

CATEGORY_SECTION_TITLE = {
    INSTRUMENT_TYPE_SDS: "Static Reserve / SDS",
    INSTRUMENT_TYPE_BOND: "Bond Universe",
    INSTRUMENT_TYPE_EQUITY: "Equity Book",
    INSTRUMENT_CATEGORY_STATE_CENTRAL: "Safe Core Allocation",
    INSTRUMENT_CATEGORY_PSU: "PSU Opportunities",
    INSTRUMENT_CATEGORY_PRIVATE: "Corporate Credit Opportunities",
    INSTRUMENT_CATEGORY_EQUITY: "Equity Opportunities",
}

_SDS_HINTS = (
    "SDS",
    "STATE DEVELOPMENT SAVINGS",
    "SAVINGS BOND",
    "SAVINGS BONDS",
    "FLOATING RATE SAVINGS",
    "NON-TRADABLE",
    "LOCKED",
    "STATIC",
    "FRSB",
)

_BOND_STATE_HINTS = (
    "SDL",
    "STATE DEVELOPMENT LOAN",
    "STATE GOVT",
    "STATE GOVERNMENT",
    "STATE GOVERNMENT SECURITY",
)

_BOND_CENTRAL_HINTS = (
    "G-SEC",
    "GSEC",
    "GOVERNMENT OF INDIA",
    "GOVT OF INDIA",
    "GOI",
    "CENTRAL GOV",
    "CENTRAL GOVERNMENT",
    "TREASURY BILL",
    "T-BILL",
    "SOVEREIGN",
)

_GOV_ISSUER_TYPES = {
    "G-SEC",
    "GSEC",
    "SDL",
    "SOVEREIGN",
    "CENTRAL_GOVT",
    "STATE_GOVT",
    "CENTRAL-GOVT",
    "STATE-GOVT",
    "T-BILL",
    "TBILL",
    "TREASURY",
}

_PSU_ISSUER_TYPES = {
    "PSU",
    "PSE",
    "PUBLIC_SECTOR",
}

_PSU_HINTS = (
    "PUBLIC SECTOR",
    "PSU",
    "NTPC",
    "NHAI",
    "NABARD",
    "PFC",
    "REC",
    "IRFC",
    "HUDCO",
    "POWER FINANCE",
    "RURAL ELECTRIFICATION",
    "POWER GRID",
    "GAIL",
    "ONGC",
    "SJVN",
)

_GOV_HINTS = (
    "GOVERNMENT OF INDIA",
    "GOVT OF INDIA",
    "GOI",
    "STATE GOVERNMENT",
    "STATE DEVELOPMENT LOAN",
    "SDL",
    "G-SEC",
    "GSEC",
    "TREASURY BILL",
    "T-BILL",
    "SOVEREIGN",
)

_EQUITY_HINTS = (
    "EQUITY",
    "EQUITY RELATED",
    "EQUITY-LINKED",
    "SHARE",
    "STOCK",
    "ETF",
    "INDEX FUND",
    "MUTUAL FUND",
    "FUND",
)

_NBFC_HINTS = (
    "NBFC",
    "HOUSING FINANCE",
    "FINANCIAL SERVICES",
    "MICROFINANCE",
)

_CONVERTIBLE_HINTS = (
    "CONVERTIBLE",
    "EQUITY LINKED",
    "MANDATORY CONVERTIBLE",
)

_PERPETUAL_HINTS = (
    "PERPETUAL",
    "AT1",
    "ADDITIONAL TIER",
)


def _upper_text(value: Any) -> str:
    return str(value or "").strip().upper()


def _joined_text(record: Mapping[str, Any]) -> str:
    fields = (
        record.get("type"),
        record.get("sub_type"),
        record.get("instrument_name"),
        record.get("bond_name"),
        record.get("issuer_name"),
        record.get("issuer"),
        record.get("category"),
        record.get("workbook_category"),
        record.get("sector"),
        record.get("workbook_sector"),
        record.get("issuer_type"),
        record.get("instrument_type"),
        record.get("security_type"),
        record.get("instrument_subtype"),
        record.get("portfolio_category"),
        record.get("portfolio_category_label"),
        record.get("portfolio_bucket"),
        record.get("portfolio_category_key"),
    )
    return " ".join(_upper_text(v) for v in fields if v not in (None, ""))


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(h in text for h in hints)


def _result_payload(
    *,
    asset_type: str,
    sub_type: str,
    legacy_category: str,
    legacy_subtype: str,
    portfolio_category: str,
    is_static: bool = False,
) -> dict[str, str | bool]:
    return {
        "type": asset_type,
        "sub_type": sub_type,
        "instrument_type": asset_type,
        "instrument_subtype_canonical": sub_type,
        "instrument_category": legacy_category,
        "instrument_subtype": legacy_subtype,
        "legacy_category": legacy_category,
        "legacy_subtype": legacy_subtype,
        "portfolio_category": portfolio_category,
        "portfolio_category_label": PORTFOLIO_CATEGORY_DISPLAY_NAME.get(portfolio_category, portfolio_category),
        "is_static_instrument": is_static,
    }


def _is_sds_text(joined: str) -> bool:
    return _contains_any(joined, _SDS_HINTS)


def _bond_subtype(joined: str, issuer_type: str) -> tuple[str, str]:
    if issuer_type in _GOV_ISSUER_TYPES or _contains_any(joined, _BOND_CENTRAL_HINTS):
        if issuer_type in {"SDL", "STATE_GOVT", "STATE-GOVT", "STATE_GOVERNMENT"} or _contains_any(joined, _BOND_STATE_HINTS):
            return INSTRUMENT_SUBTYPE_BOND_STATE, INSTRUMENT_SUBTYPE_SDL
        return INSTRUMENT_SUBTYPE_BOND_CENTRAL, INSTRUMENT_SUBTYPE_GSEC

    if issuer_type in _PSU_ISSUER_TYPES or _contains_any(joined, _PSU_HINTS):
        return INSTRUMENT_SUBTYPE_BOND_PSU, INSTRUMENT_SUBTYPE_PSU_BOND

    if _contains_any(joined, _PERPETUAL_HINTS):
        return INSTRUMENT_SUBTYPE_BOND_PRIVATE, INSTRUMENT_SUBTYPE_PERPETUAL

    if _contains_any(joined, _NBFC_HINTS):
        return INSTRUMENT_SUBTYPE_BOND_PRIVATE, INSTRUMENT_SUBTYPE_NBFC

    return INSTRUMENT_SUBTYPE_BOND_PRIVATE, INSTRUMENT_SUBTYPE_CORPORATE


def _equity_subtype(joined: str) -> tuple[str, str]:
    if _contains_any(joined, _CONVERTIBLE_HINTS):
        return INSTRUMENT_SUBTYPE_EQUITY_MARKET, INSTRUMENT_SUBTYPE_CONVERTIBLE
    if any(token in joined for token in ("NAV", "MUTUAL FUND", "INDEX FUND", "ETF")):
        return INSTRUMENT_SUBTYPE_EQUITY_NAV, INSTRUMENT_SUBTYPE_EQUITY_SHARE
    return INSTRUMENT_SUBTYPE_EQUITY_MARKET, INSTRUMENT_SUBTYPE_EQUITY_SHARE


def classify_instrument(record: Mapping[str, Any]) -> dict[str, str]:
    issuer_type = _upper_text(record.get("issuer_type") or record.get("instrument_type"))
    joined = _joined_text(record)

    if _is_sds_text(joined):
        return _result_payload(
            asset_type=INSTRUMENT_TYPE_SDS,
            sub_type=INSTRUMENT_SUBTYPE_STATIC,
            legacy_category=INSTRUMENT_CATEGORY_STATE_CENTRAL,
            legacy_subtype=INSTRUMENT_SUBTYPE_SDS,
            portfolio_category=PORTFOLIO_CATEGORY_SDS,
            is_static=True,
        )

    if _contains_any(joined, _CONVERTIBLE_HINTS):
        return _result_payload(
            asset_type=INSTRUMENT_TYPE_EQUITY,
            sub_type=INSTRUMENT_SUBTYPE_EQUITY_MARKET,
            legacy_category=INSTRUMENT_CATEGORY_EQUITY,
            legacy_subtype=INSTRUMENT_SUBTYPE_CONVERTIBLE,
            portfolio_category=PORTFOLIO_CATEGORY_EQUITY,
        )

    if _contains_any(joined, _EQUITY_HINTS):
        canonical_subtype, legacy_subtype = _equity_subtype(joined)
        return _result_payload(
            asset_type=INSTRUMENT_TYPE_EQUITY,
            sub_type=canonical_subtype,
            legacy_category=INSTRUMENT_CATEGORY_EQUITY,
            legacy_subtype=legacy_subtype,
            portfolio_category=PORTFOLIO_CATEGORY_EQUITY,
        )

    if issuer_type in _GOV_ISSUER_TYPES or _contains_any(joined, _GOV_HINTS):
        canonical_subtype, legacy_subtype = _bond_subtype(joined, issuer_type)
        return _result_payload(
            asset_type=INSTRUMENT_TYPE_BOND,
            sub_type=canonical_subtype,
            legacy_category=INSTRUMENT_CATEGORY_STATE_CENTRAL,
            legacy_subtype=legacy_subtype,
            portfolio_category=PORTFOLIO_CATEGORY_CENTRAL_STATE,
        )

    if issuer_type in _PSU_ISSUER_TYPES or _contains_any(joined, _PSU_HINTS):
        return _result_payload(
            asset_type=INSTRUMENT_TYPE_BOND,
            sub_type=INSTRUMENT_SUBTYPE_BOND_PSU,
            legacy_category=INSTRUMENT_CATEGORY_PSU,
            legacy_subtype=INSTRUMENT_SUBTYPE_PSU_BOND,
            portfolio_category=PORTFOLIO_CATEGORY_PSU,
        )

    if _contains_any(joined, _PERPETUAL_HINTS):
        return _result_payload(
            asset_type=INSTRUMENT_TYPE_BOND,
            sub_type=INSTRUMENT_SUBTYPE_BOND_PRIVATE,
            legacy_category=INSTRUMENT_CATEGORY_PRIVATE,
            legacy_subtype=INSTRUMENT_SUBTYPE_PERPETUAL,
            portfolio_category=PORTFOLIO_CATEGORY_PRIVATE,
        )

    if _contains_any(joined, _NBFC_HINTS):
        return _result_payload(
            asset_type=INSTRUMENT_TYPE_BOND,
            sub_type=INSTRUMENT_SUBTYPE_BOND_PRIVATE,
            legacy_category=INSTRUMENT_CATEGORY_PRIVATE,
            legacy_subtype=INSTRUMENT_SUBTYPE_NBFC,
            portfolio_category=PORTFOLIO_CATEGORY_PRIVATE,
        )

    return _result_payload(
        asset_type=INSTRUMENT_TYPE_BOND,
        sub_type=INSTRUMENT_SUBTYPE_BOND_PRIVATE,
        legacy_category=INSTRUMENT_CATEGORY_PRIVATE,
        legacy_subtype=INSTRUMENT_SUBTYPE_CORPORATE,
        portfolio_category=PORTFOLIO_CATEGORY_PRIVATE,
    )


def portfolio_category_from_record(record: Mapping[str, Any], classification: Mapping[str, Any] | None = None) -> str:
    explicit = _upper_text(
        record.get("portfolio_category")
        or record.get("portfolio_bucket")
        or record.get("portfolio_category_key")
    )
    if explicit in PORTFOLIO_CATEGORY_ORDER:
        return explicit
    if explicit in {INSTRUMENT_CATEGORY_STATE_CENTRAL, "GOVT", "CENTRAL_STATE", "STATE_CENTRAL"}:
        return PORTFOLIO_CATEGORY_CENTRAL_STATE
    if explicit in {INSTRUMENT_CATEGORY_PSU, "PSU_PRIVATE"}:
        return PORTFOLIO_CATEGORY_PSU
    if explicit in {INSTRUMENT_CATEGORY_PRIVATE, "PSU_PRIVATE_DEBT"}:
        return PORTFOLIO_CATEGORY_PRIVATE
    if explicit in {INSTRUMENT_CATEGORY_EQUITY, "EQUITY_RELATED"}:
        return PORTFOLIO_CATEGORY_EQUITY
    if explicit in {INSTRUMENT_TYPE_SDS, "STATIC", "LOCKED", "NON_TRADABLE", "NON-TRADABLE"}:
        return PORTFOLIO_CATEGORY_SDS

    classified = dict(classification or classify_instrument(record))
    legacy_category = _upper_text(classified.get("instrument_category"))
    if classified.get("type") == INSTRUMENT_TYPE_SDS:
        return PORTFOLIO_CATEGORY_SDS
    if classified.get("type") == INSTRUMENT_TYPE_EQUITY or legacy_category == INSTRUMENT_CATEGORY_EQUITY:
        return PORTFOLIO_CATEGORY_EQUITY
    if legacy_category == INSTRUMENT_CATEGORY_STATE_CENTRAL:
        return PORTFOLIO_CATEGORY_CENTRAL_STATE
    if legacy_category == INSTRUMENT_CATEGORY_PSU:
        return PORTFOLIO_CATEGORY_PSU
    if legacy_category == INSTRUMENT_CATEGORY_PRIVATE:
        return PORTFOLIO_CATEGORY_PRIVATE
    return PORTFOLIO_CATEGORY_PRIVATE


def category_to_allocation_bucket(category: str | None) -> str:
    cat = _upper_text(category)
    if cat in {INSTRUMENT_TYPE_SDS, PORTFOLIO_CATEGORY_SDS}:
        return "SDS"
    if cat == INSTRUMENT_TYPE_BOND:
        return "BOND"
    if cat in {INSTRUMENT_TYPE_EQUITY, PORTFOLIO_CATEGORY_EQUITY}:
        return "EQUITY"
    if cat in {INSTRUMENT_CATEGORY_STATE_CENTRAL, PORTFOLIO_CATEGORY_CENTRAL_STATE, "GOVT", "CENTRAL_STATE", "STATE_CENTRAL"}:
        return "GOVT"
    if cat == INSTRUMENT_CATEGORY_EQUITY:
        return "EQUITY"
    if cat in {INSTRUMENT_CATEGORY_PSU, PORTFOLIO_CATEGORY_PSU, INSTRUMENT_CATEGORY_PRIVATE, PORTFOLIO_CATEGORY_PRIVATE}:
        return "PSU_PRIVATE_DEBT"
    return "PSU_PRIVATE_DEBT"


def category_sort_key(category: str | None) -> int:
    cat = _upper_text(category)
    return CATEGORY_ORDER.get(cat, PORTFOLIO_CATEGORY_ORDER.get(cat, 99))


def category_metadata(category: str | None) -> dict[str, str]:
    cat = _upper_text(category)
    if cat in PORTFOLIO_CATEGORY_DISPLAY_NAME:
        return {
            "category_display": PORTFOLIO_CATEGORY_DISPLAY_NAME.get(cat, "Unknown"),
            "category_tag": PORTFOLIO_CATEGORY_TAG.get(cat, "Unknown"),
            "risk_label": PORTFOLIO_CATEGORY_RISK_LABEL.get(cat, "Unknown"),
            "section_title": PORTFOLIO_CATEGORY_SECTION_TITLE.get(cat, "Other Instruments"),
        }
    return {
        "category_display": CATEGORY_DISPLAY_NAME.get(cat, "Unknown"),
        "category_tag": CATEGORY_TAG.get(cat, "Unknown"),
        "risk_label": CATEGORY_RISK_LABEL.get(cat, "Unknown"),
        "section_title": CATEGORY_SECTION_TITLE.get(cat, "Other Instruments"),
    }


def portfolio_category_metadata(category: str | None) -> dict[str, str]:
    return category_metadata(category)


def category_explainability_lines(category: str | None, subtype: str | None = None) -> list[str]:
    cat = _upper_text(category)
    sub = _upper_text(subtype)
    if cat in {INSTRUMENT_TYPE_SDS, PORTFOLIO_CATEGORY_SDS} or sub == INSTRUMENT_SUBTYPE_SDS:
        return [
            "Static government-backed instrument",
            "Locked / non-tradable",
            "No YTM, spread, or market-price trend calculations",
        ]
    if cat == INSTRUMENT_TYPE_BOND:
        if sub in (INSTRUMENT_SUBTYPE_BOND_CENTRAL, INSTRUMENT_SUBTYPE_GSEC) or cat in {PORTFOLIO_CATEGORY_CENTRAL_STATE, INSTRUMENT_CATEGORY_STATE_CENTRAL}:
            return [
                "Market-linked central and state government paper",
                "Priced off the sovereign curve",
                "Evaluated using YTM, duration, and price trend",
            ]
        if sub in (INSTRUMENT_SUBTYPE_BOND_STATE, INSTRUMENT_SUBTYPE_SDL):
            return [
                "Market-linked state government paper",
                "Yield premium over central government curve",
                "Evaluated using YTM, duration, and spread to sovereign curve",
            ]
        if sub in (INSTRUMENT_SUBTYPE_BOND_PSU, INSTRUMENT_SUBTYPE_PSU_BOND):
            return [
                "Issued by a government-linked entity",
                "Credit spread and liquidity matter",
                "Evaluated using YTM and duration",
            ]
        return [
            "Corporate or private credit exposure",
            "Return driven by coupon plus spread",
            "Evaluated using YTM, duration, and credit quality",
        ]
    if cat in {INSTRUMENT_TYPE_EQUITY, PORTFOLIO_CATEGORY_EQUITY}:
        return [
            "NAV / market-based investment",
            "Return measured via absolute return and CAGR",
            "No bond yield or duration framework applies",
        ]
    if cat in {INSTRUMENT_CATEGORY_STATE_CENTRAL, PORTFOLIO_CATEGORY_CENTRAL_STATE}:
        lines = [
            "Backed by Central and State Government",
            "Negligible default risk",
            "Selected based on yield advantage and duration fit",
        ]
        if sub == INSTRUMENT_SUBTYPE_SDL:
            lines.append("State-issued paper compared against sovereign curve")
        return lines
    if cat == INSTRUMENT_CATEGORY_PSU:
        return [
            "Issued by government-linked entity",
            "Not sovereign guaranteed",
            "Selected for spread advantage with controlled risk",
        ]
    if cat == INSTRUMENT_CATEGORY_PRIVATE:
        return [
            "Private issuer",
            "Risk driven by credit profile",
            "Higher yield compensates for risk",
        ]
    if cat == INSTRUMENT_CATEGORY_EQUITY:
        return [
            "Growth-oriented investment",
            "Returns driven by market performance",
            "Evaluated on performance, volatility, and valuation context",
        ]
    return ["Category-specific rationale unavailable"]
