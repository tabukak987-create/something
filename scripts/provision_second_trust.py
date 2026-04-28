from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.shared.database import execute_query, fetch_all
from app.domain.portfolio.engine import ensure_portfolio_core_schema


SECOND_TRUST_ID = "trust_b"


async def clone_trust_dataset(source_trust_id: str = "default", target_trust_id: str = SECOND_TRUST_ID) -> dict:
    await ensure_portfolio_core_schema()
    source_rows = await fetch_all(
        """
        SELECT
            isin, category, instrument_name, coupon, maturity,
            quantity, face_value, book_value, market_price, purchase_price, purchase_date,
            issuer_name, credit_rating, credit_rating_2, rating_agency, rating_agency_2, sector,
            source_name, source_type, ingestion_method, source_reference,
            source_sheet_name, source_row_number, timestamp_ingested, timestamp_effective, provenance_json
        FROM portfolio_positions
        WHERE trust_id = :source_trust_id
        ORDER BY source_row_number, id
        """,
        {"source_trust_id": source_trust_id},
    )
    if not source_rows:
        raise RuntimeError(f"No portfolio_positions rows found for trust_id={source_trust_id}")

    await execute_query(
        "DELETE FROM portfolio_positions WHERE trust_id = :trust_id",
        {"trust_id": target_trust_id},
    )

    inserted = 0
    for row in source_rows:
        payload = dict(row)
        payload["trust_id"] = target_trust_id
        payload["portfolio_id"] = target_trust_id
        await execute_query(
            """
            INSERT INTO portfolio_positions (
                trust_id, portfolio_id, isin, category, instrument_name, coupon, maturity,
                quantity, face_value, book_value, market_price, purchase_price, purchase_date,
                issuer_name, credit_rating, credit_rating_2, rating_agency, rating_agency_2, sector,
                source_name, source_type, ingestion_method, source_reference,
                source_sheet_name, source_row_number,
                timestamp_ingested, timestamp_effective, provenance_json
            ) VALUES (
                :trust_id, :portfolio_id, :isin, :category, :instrument_name, :coupon, :maturity,
                :quantity, :face_value, :book_value, :market_price, :purchase_price, :purchase_date,
                :issuer_name, :credit_rating, :credit_rating_2, :rating_agency, :rating_agency_2, :sector,
                :source_name, :source_type, :ingestion_method, :source_reference,
                :source_sheet_name, :source_row_number,
                :timestamp_ingested, :timestamp_effective, :provenance_json
            )
            """,
            payload,
        )
        inserted += 1

    return {
        "source_trust_id": source_trust_id,
        "target_trust_id": target_trust_id,
        "inserted_rows": inserted,
    }


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Provision a second trust dataset from the default trust")
    parser.add_argument("--source-trust", default="default")
    parser.add_argument("--target-trust", default=SECOND_TRUST_ID)
    args = parser.parse_args()
    result = await clone_trust_dataset(args.source_trust, args.target_trust)
    print(result)


if __name__ == "__main__":
    asyncio.run(_main())
