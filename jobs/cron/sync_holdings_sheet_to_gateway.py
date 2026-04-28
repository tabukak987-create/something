from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

import openpyxl

_BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services.portfolio_dataset_service import PortfolioDatasetService
from app.services.portfolio_sync_validation import validate_manual_sync


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


async def sync_holdings_sheet_to_gateway(workbook_path: Path) -> dict[str, int]:
    parsed = await PortfolioDatasetService.parse_upload(
        workbook_path.read_bytes(),
        filename=workbook_path.name,
        content_type="application/vnd.ms-excel",
    )
    sync_result = await PortfolioDatasetService.sync_upload(
        parsed,
        uploaded_by="workbook_sync",
        source_context={"trust_id": "default", "portfolio_id": "default", "source_reference": workbook_path.name},
    )
    validation_result = await validate_manual_sync(workbook_path)

    return {
        "source_rows": int(parsed.get("summary", {}).get("total_rows") or 0),
        "accepted_rows": int(parsed.get("summary", {}).get("accepted_rows") or 0),
        "rejected_rows": int(parsed.get("summary", {}).get("rejected_rows") or 0),
        "synced_rows": int(sync_result.get("synced_rows") or 0),
        "validation_pass": bool(validation_result.get("pass")),
        "validation_checked_rows": int(validation_result.get("checked_rows") or 0),
        "validation_mismatch_count": len(validation_result.get("mismatches") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync the holdings sheet into the gateway portfolio tables")
    parser.add_argument("--workbook", required=True, help="Path to the holdings workbook (.xlsx)")
    args = parser.parse_args()

    result = asyncio.run(sync_holdings_sheet_to_gateway(Path(args.workbook)))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
