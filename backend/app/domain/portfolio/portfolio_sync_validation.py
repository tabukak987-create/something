from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domain.portfolio.dataset import PortfolioDatasetService
from app.domain.portfolio.analytics import normalize_rating
from app.shared.database import fetch_all


def _normalize_value(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def _normalize_rating_value(value: Any) -> str | None:
    normalized = normalize_rating(value)
    if normalized in {"NR", "UNKNOWN", ""}:
        return None
    return normalized


async def validate_manual_sync(workbook_path: Path, *, trust_id: str = "default") -> dict[str, Any]:
    parsed = await PortfolioDatasetService.parse_upload(
        workbook_path.read_bytes(),
        filename=workbook_path.name,
        content_type="application/vnd.ms-excel",
    )
    accepted = list(parsed.get("accepted") or [])

    expected_by_row: dict[int, dict[str, str | None]] = {}
    for row in accepted:
        row_number = int(row.get("_row_number") or 0)
        expected_by_row[row_number] = {
            "credit_rating": _normalize_rating_value(row.get("credit_rating_1") or row.get("credit_rating") or row.get("rating")),
            "credit_rating_2": _normalize_rating_value(row.get("credit_rating_2")),
            "rating_agency": _normalize_value(row.get("rating_agency_1") or row.get("rating_agency")),
            "rating_agency_2": _normalize_value(row.get("rating_agency_2")),
        }

    row_numbers = sorted(expected_by_row.keys())
    if not row_numbers:
        return {"checked_rows": 0, "mismatches": [], "pass": True}

    placeholders = ", ".join(f":r{i}" for i in range(len(row_numbers)))
    rows = await fetch_all(
        f"""SELECT source_row_number, credit_rating, credit_rating_2, rating_agency, rating_agency_2
            FROM portfolio_positions
            WHERE trust_id = :trust_id
              AND source_row_number IN ({placeholders})
            ORDER BY source_row_number""",
        {"trust_id": trust_id, **{f"r{i}": row_number for i, row_number in enumerate(row_numbers)}},
    )
    found_by_row = {int(row.get("source_row_number") or 0): row for row in rows}

    mismatches: list[dict[str, Any]] = []
    for row_number in row_numbers:
        expected = expected_by_row[row_number]
        actual = found_by_row.get(row_number)
        if not actual:
            mismatches.append({"row_number": row_number, "issue": "missing_in_portfolio_positions"})
            continue

        for field in ("credit_rating", "credit_rating_2", "rating_agency", "rating_agency_2"):
            if field.startswith("credit_rating"):
                actual_value = _normalize_rating_value(actual.get(field))
            else:
                actual_value = _normalize_value(actual.get(field))
            expected_value = expected.get(field)
            if actual_value != expected_value:
                mismatches.append(
                    {
                        "row_number": row_number,
                        "field": field,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    return {
        "checked_rows": len(row_numbers),
        "mismatches": mismatches,
        "pass": not mismatches,
    }
