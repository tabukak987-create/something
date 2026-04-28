"""
Universal ISIN Registry — Single Source of Truth for Bond Metadata
All four services (bond_agent, bond_intelligence, compliance_engine, bond_quote_system)
resolve bond attributes via this registry before doing pricing or compliance work.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Optional

from .database import execute_query, fetch_all, fetch_one
from .logger import get_logger
from .time_utils import utcnow_iso
from ..services.instrument_classification import classify_instrument
from ..services.data_governance import normalize_source_context, validate_source_context

log = get_logger("isin_registry")


class ISINRegistry:
    """
    Universal bond metadata registry.

    Usage
    -----
    registry = ISINRegistry()
    bond = await registry.get(isin)
    if not bond:
        await registry.upsert({...})
    """

    # ―― Reads ―――――――――――――――――――――――――――――――――――――――――――――――――――――――――

    async def get(self, isin: str) -> Optional[dict]:
        """Fetch a single bond by ISIN (None if not found)."""
        return await fetch_one(
            "SELECT * FROM universal_bond_master WHERE isin = :isin",
            {"isin": isin.strip().upper()},
        )

    async def search(
        self,
        *,
        issuer_type: Optional[str] = None,
        credit_rating: Optional[str] = None,
        sector: Optional[str] = None,
        pf_eligible: Optional[bool] = None,
        invested_only: Optional[bool] = None,
        keyword: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """Flexible search across the universal bond master."""
        conditions: list[str] = ["b.is_active = 1"]
        params: dict = {"limit": limit, "offset": offset}

        if issuer_type:
            conditions.append("b.issuer_type = :issuer_type")
            params["issuer_type"] = issuer_type.upper()
        if credit_rating:
            conditions.append("b.credit_rating = :credit_rating")
            params["credit_rating"] = credit_rating.upper()
        if sector:
            conditions.append("b.sector = :sector")
            params["sector"] = sector.upper()
        if pf_eligible is not None:
            conditions.append("b.is_pf_eligible = :pf_eligible")
            params["pf_eligible"] = 1 if pf_eligible else 0
        if invested_only is True:
            conditions.append("p.isin IS NOT NULL")
        if keyword:
            conditions.append(
                "(b.issuer_name LIKE :kw OR b.bond_name LIKE :kw OR b.isin LIKE :kw)"
            )
            params["kw"] = f"%{keyword}%"

        where = " AND ".join(conditions)
        sql = f"""
            SELECT b.*, CASE WHEN p.isin IS NULL THEN 0 ELSE 1 END AS is_invested
            FROM universal_bond_master b
            LEFT JOIN (
                SELECT DISTINCT isin
                                FROM portfolio_holdings
                                WHERE COALESCE(portfolio_id, 'default') = 'default'
                                    AND COALESCE(quantity, 0) > 0
            ) p ON p.isin = b.isin
            WHERE {where}
            ORDER BY
                CASE WHEN p.isin IS NULL THEN 0 ELSE 1 END DESC,
                COALESCE(NULLIF(TRIM(b.updated_at), ''), '0000-00-00T00:00:00') DESC,
                COALESCE(NULLIF(TRIM(b.bond_name), ''), b.issuer_name) ASC,
                b.maturity_date ASC
            LIMIT :limit OFFSET :offset
        """
        return await fetch_all(sql, params)

    async def count(self) -> int:
        row = await fetch_one("SELECT COUNT(*) AS cnt FROM universal_bond_master WHERE is_active = 1")
        return int(row["cnt"]) if row else 0

    async def stats(self) -> dict:
        """Aggregate statistics useful for dashboards."""
        rows = await fetch_all(
            """
            SELECT issuer_type, credit_rating, COUNT(*) AS cnt,
                   SUM(is_pf_eligible) AS pf_count
            FROM universal_bond_master
            WHERE is_active = 1
            GROUP BY issuer_type, credit_rating
            ORDER BY issuer_type, credit_rating
            """
        )
        total = sum(r["cnt"] for r in rows)
        by_type: dict[str, int] = {}
        for r in rows:
            by_type[r["issuer_type"]] = by_type.get(r["issuer_type"], 0) + r["cnt"]
        return {
            "total_bonds": total,
            "by_type": by_type,
            "detail": rows,
        }

    # ―― Writes ――――――――――――――――――――――――――――――――――――――――――――――――――――――――

    async def upsert(self, bond: dict, source_context: Optional[dict] = None) -> str:
        """Insert or update a bond record. Returns the ISIN."""
        isin = bond["isin"].strip().upper()
        now = utcnow_iso()
        provenance = normalize_source_context(
            source_context,
            default_source_name=str(bond.get("source_name") or "Bond Registry"),
            default_source_type=str(bond.get("source_type") or "client_provided"),
            default_ingestion_method=str(bond.get("ingestion_method") or "manual_upload"),
            default_source_reference=str(bond.get("source_reference") or bond.get("source_file") or isin),
            actor=str(bond.get("actor") or "system"),
        )
        provenance_errors = validate_source_context(provenance)
        if provenance_errors:
            raise ValueError("; ".join(provenance_errors))

        instrument_category = str(
            classify_instrument(
                {
                    "issuer_type": bond.get("issuer_type"),
                    "issuer_name": bond.get("issuer_name"),
                    "sector": bond.get("sector"),
                    "category": bond.get("category") or bond.get("sub_sector"),
                    "bond_name": bond.get("bond_name") or bond.get("issuer_name") or isin,
                }
            ).get("instrument_category")
            or "PRIVATE"
        ).upper()

        await execute_query(
            """
            INSERT INTO universal_bond_master (
                isin, bond_name, issuer_name, issuer_type, coupon_rate,
                coupon_type, coupon_frequency, maturity_date, issue_date,
                face_value, credit_rating, rating_agency, sector, sub_sector, instrument_category,
                listing_exchange, is_pf_eligible, is_active,
                source_name, source_type, ingestion_method, source_reference,
                timestamp_ingested, timestamp_effective, provenance_json, updated_at
            ) VALUES (
                :isin, :bond_name, :issuer_name, :issuer_type, :coupon_rate,
                :coupon_type, :coupon_frequency, :maturity_date, :issue_date,
                :face_value, :credit_rating, :rating_agency, :sector, :sub_sector, :instrument_category,
                :listing_exchange, :is_pf_eligible, 1,
                :source_name, :source_type, :ingestion_method, :source_reference,
                :timestamp_ingested, :timestamp_effective, :provenance_json, :updated_at
            )
            ON CONFLICT(isin) DO UPDATE SET
                bond_name        = excluded.bond_name,
                issuer_name      = excluded.issuer_name,
                issuer_type      = excluded.issuer_type,
                coupon_rate      = excluded.coupon_rate,
                coupon_type      = excluded.coupon_type,
                coupon_frequency = excluded.coupon_frequency,
                maturity_date    = excluded.maturity_date,
                face_value       = excluded.face_value,
                credit_rating    = excluded.credit_rating,
                sector           = excluded.sector,
                listing_exchange = excluded.listing_exchange,
                instrument_category = excluded.instrument_category,
                is_pf_eligible   = excluded.is_pf_eligible,
                source_name      = excluded.source_name,
                source_type      = excluded.source_type,
                ingestion_method  = excluded.ingestion_method,
                source_reference  = excluded.source_reference,
                timestamp_ingested = excluded.timestamp_ingested,
                timestamp_effective = excluded.timestamp_effective,
                provenance_json   = excluded.provenance_json,
                updated_at       = excluded.updated_at
            """,
            {
                "isin": isin,
                "bond_name": bond.get("bond_name", ""),
                "issuer_name": bond.get("issuer_name", ""),
                "issuer_type": bond.get("issuer_type", "CORPORATE").upper(),
                "coupon_rate": float(bond.get("coupon_rate", 0)),
                "coupon_type": bond.get("coupon_type", "FIXED").upper(),
                "coupon_frequency": int(bond.get("coupon_frequency", 2)),
                "maturity_date": str(bond.get("maturity_date", "")),
                "issue_date": str(bond.get("issue_date", "")),
                "face_value": float(bond.get("face_value", 1000)),
                "credit_rating": bond.get("credit_rating", ""),
                "rating_agency": bond.get("rating_agency", ""),
                "sector": bond.get("sector", ""),
                "sub_sector": bond.get("sub_sector", ""),
                "instrument_category": instrument_category,
                "listing_exchange": bond.get("listing_exchange", "NSE"),
                "is_pf_eligible": 1 if bond.get("is_pf_eligible") else 0,
                "source_name": provenance["source_name"],
                "source_type": provenance["source_type"],
                "ingestion_method": provenance["ingestion_method"],
                "source_reference": provenance["source_reference"],
                "timestamp_ingested": provenance["timestamp_ingested"],
                "timestamp_effective": provenance["timestamp_effective"],
                "provenance_json": json.dumps(provenance, default=str),
                "updated_at": now,
            },
        )
        log.debug("upserted isin", isin=isin)
        return isin

    async def bulk_upsert(self, bonds: list[dict]) -> int:
        """Upsert a list of bonds. Returns count of processed records."""
        count = 0
        for b in bonds:
            try:
                await self.upsert(b)
                count += 1
            except Exception as exc:
                log.warning("bulk_upsert row failed", isin=b.get("isin"), error=str(exc))
        log.info("bulk_upsert complete", total=count)
        return count

    async def deactivate(self, isin: str) -> None:
        await execute_query(
            "UPDATE universal_bond_master SET is_active = 0 WHERE isin = :isin",
            {"isin": isin.strip().upper()},
        )
