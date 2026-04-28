"""
Alert Engine — Unified Gateway
Detects and persists alerts for:
  1. PRICE_DEVIATION   — Broker price > market by threshold
  2. RATING_DOWNGRADE  — Rating below threshold or watch negative
  3. YIELD_SPREAD      — Abnormal yield spread vs peers
  4. EXPOSURE_BREACH   — Concentration limit exceeded
  5. LIQUIDITY_LOW     — Liquidity score below minimum
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Dict, Any
from ..shared.database import fetch_all, fetch_one, execute_query
from ..shared.logger import get_logger
from ..services.instrument_classification import classify_instrument

log = get_logger("enhanced.alerts")

# ─── Thresholds ───────────────────────────────────────────────────────────────
PRICE_DEVIATION_PCT = 1.0        # Broker price > market by 1.0%
PRICE_DEVIATION_CRITICAL_PCT = 2.5  # Escalate when deviation is materially wider
YIELD_SPREAD_ABNORMAL_BPS = 75   # Spread anomaly threshold
SPREAD_ALERT_MAX_PRICE_AGE_DAYS = 3
LIQUIDITY_MIN_SCORE = 20.0       # Minimum liquidity score
ISSUER_EXPOSURE_CAP_PCT = 5.0    # Single issuer concentration cap
SEVERITY_RANK = {"INFO": 1, "WARNING": 2, "CRITICAL": 3}
GOVERNMENT_SPREAD_ALERT_TYPE = "GOVERNMENT_SPREAD"

GOVERNMENT_ISSUER_TYPES = {
    "G-SEC", "GSEC", "T-BILL", "TBILL", "SDL",
    "CENTRAL-GOVT", "STATE-GOVT", "CENTRAL_GOVT", "STATE_GOVT", "SOVEREIGN",
}

GOVERNMENT_ISSUER_HINTS = (
    "STATE DEVELOPMENT LOAN",
    "UDAY BOND",
    "GOVERNMENT OF INDIA",
    "GOVERNMENT OF ",
    "GOVT OF ",
    "GOI",
    "SOVEREIGN",
    "G-SEC",
    "GSEC",
    "T-BILL",
    "TBILL",
    "SDL",
    "STATE GUARANTEED",
    "STATE ELECTRICITY",
    "ELECTRICITY BOARD",
    "POWER CORPORATION",
    "POWER TRANSMISSION",
    "DISCOM",
    "STATE POWER",
)

RATING_ORDER = {
    # Sovereign / Government — highest safety, above AAA
    "SOVEREIGN": 0, "SOV": 0, "G-SEC": 0, "GSEC": 0,
    "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7, "BBB+": 8, "BBB": 9,
    "BBB-": 10, "BB+": 11, "BB": 12, "B": 13, "C": 14, "D": 15
}

TYPICAL_SPREAD_BPS = {
    # Sovereign = 0 bps (benchmark / risk-free rate)
    "SOVEREIGN": 0, "SOV": 0, "G-SEC": 0, "GSEC": 0,
    "AAA": 45, "AA+": 75, "AA": 110, "AA-": 140,
    "A+": 180, "A": 220, "A-": 270, "BBB+": 340, "BBB": 420,
}


def _normalize_rating(raw: str | None, issuer_type: str | None = None) -> str:
    """Normalize raw credit_rating to canonical form used in RATING_ORDER."""
    if not raw:
        # Government issuer types are implicitly Sovereign
        if issuer_type and issuer_type.upper() in ("G-SEC", "GSEC", "T-BILL", "TBILL", "SDL", "CENTRAL-GOVT", "STATE-GOVT"):
            return "SOVEREIGN"
        return "UNRATED"
    r = raw.upper().strip()
    if r in ("SOV", "G-SEC", "GSEC"):
        return "SOVEREIGN"
    return r


def _price_date_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


def _is_market_date_fresh(price_date: object, *, as_of: date | None = None) -> bool:
    text = _price_date_text(price_date)
    if not text:
        return False
    try:
        parsed = date.fromisoformat(text)
    except Exception:
        return False
    current = as_of or date.today()
    return (current - parsed) <= timedelta(days=SPREAD_ALERT_MAX_PRICE_AGE_DAYS)


def _is_government_backed_spread(issuer_name: object, issuer_type: object, credit_rating: object | None = None) -> bool:
    issuer_name_up = str(issuer_name or "").upper()
    issuer_type_up = str(issuer_type or "").upper()
    rating_up = str(credit_rating or "").upper().strip()

    if issuer_type_up in GOVERNMENT_ISSUER_TYPES:
        return True

    if any(hint in issuer_name_up for hint in GOVERNMENT_ISSUER_HINTS):
        return True

    if rating_up == "SOVEREIGN" and issuer_name_up:
        return any(hint in issuer_name_up for hint in GOVERNMENT_ISSUER_HINTS)

    return False


def _alert_instrument_category(row: dict[str, Any]) -> str:
    derived = classify_instrument(row)
    return str(row.get("instrument_category") or derived.get("instrument_category") or "PRIVATE").upper()


class AlertEngine:
    """Generates and persists alerts based on market data and compliance analysis."""

    def __init__(self):
        self.today = date.today().isoformat()

    async def run_all_checks(self, limit: int = 1000) -> Dict[str, Any]:
        """Run all alert checks and return summary."""
        log.info("alert_engine_started")

        alerts_generated = 0
        alerts_generated += await self._check_price_deviations(limit)
        alerts_generated += await self._check_rating_issues(limit)
        alerts_generated += await self._check_yield_spread_anomalies(limit)
        alerts_generated += await self._check_liquidity_issues(limit)
        alerts_generated += await self._check_exposure_breaches()

        log.info("alert_engine_complete", alerts=alerts_generated)
        return {
            "alert_date": self.today,
            "alerts_generated": alerts_generated,
        }

    async def _allowed_isins(self, trust_id: str) -> set[str]:
        rows = await fetch_all(
            """
            SELECT DISTINCT isin
            FROM portfolio_positions
            WHERE trust_id = :pid
            UNION
            SELECT DISTINCT isin
            FROM portfolio_holdings
            WHERE portfolio_id = :pid
            """,
            {"pid": trust_id},
        )
        return {str(row.get("isin") or "").upper() for row in rows or [] if row.get("isin")}

    async def get_alerts_for_date(self, alert_date: str = None, trust_id: str = "default") -> List[Dict[str, Any]]:
        """Get alerts for a specific date scoped to the current trust portfolio."""
        dt = alert_date or self.today
        allowed_isins = await self._allowed_isins(trust_id)
        rows = await fetch_all(
            """SELECT * FROM unified_alerts
               WHERE alert_date = :dt
               ORDER BY severity DESC, created_at DESC""",
            {"dt": dt},
        )
        scoped: list[dict] = []
        for row in (rows or []):
            isin = str(row.get("isin") or "").upper()
            # Keep portfolio-level alerts (isin is null/empty), and bond alerts only when in trust scope.
            if not isin or isin in allowed_isins:
                scoped.append(row)
        return scoped

    async def get_alerts_for_bond(self, isin: str, trust_id: str = "default") -> List[Dict[str, Any]]:
        """Get all active alerts for a bond if it belongs to the current trust portfolio."""
        allowed_isins = await self._allowed_isins(trust_id)
        if str(isin or "").upper() not in allowed_isins:
            return []
        rows = await fetch_all(
            """SELECT * FROM unified_alerts
               WHERE isin = :isin AND is_resolved = 0
               ORDER BY severity DESC, created_at DESC""",
            {"isin": isin},
        )
        return rows or []

    # ─── Check 1: Price Deviations ────────────────────────────────────────────

    async def _check_price_deviations(self, limit: int) -> int:
        """Alert if broker quoted price > market price by threshold."""
        count = 0
        allowed_isins = await self._allowed_isins("default")
        if not allowed_isins:
            return 0
        rows = await fetch_all(
            """
            SELECT q.isin, q.broker_name, q.ask_price, q.bid_price,
                     m.clean_price, b.issuer_name, b.issuer_type, b.credit_rating, b.instrument_category
            FROM   unified_broker_quotes q
            JOIN   unified_market_data m ON q.isin = m.isin
            JOIN   universal_bond_master b ON q.isin = b.isin
            WHERE  m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = q.isin)
              AND  q.quote_date >= :today
              AND  m.clean_price > 0
            LIMIT :lim
            """,
            {"today": self.today, "lim": limit},
        )

        for row in (rows or []):
            isin = str(row.get("isin") or "").upper()
            if isin not in allowed_isins:
                continue
            broker_price = row.get("ask_price") or row.get("bid_price") or 0
            market_price = row.get("clean_price") or 0
            if market_price <= 0 or broker_price <= 0:
                continue

            deviation_pct = ((broker_price - market_price) / market_price) * 100
            if deviation_pct > PRICE_DEVIATION_PCT:
                severity = "CRITICAL" if deviation_pct >= PRICE_DEVIATION_CRITICAL_PCT else "WARNING"
                await self._persist_alert(
                    isin=isin,
                    alert_type="PRICE_DEVIATION",
                    severity=severity,
                    title=f"Broker price {deviation_pct:.2f}% above market for {row['issuer_name']}",
                    detail=(
                        f"Broker {row.get('broker_name', 'Unknown')} quotes ₹{broker_price:.2f} "
                        f"vs market ₹{market_price:.2f} — deviation {deviation_pct:.2f}%"
                    ),
                    metric_value=deviation_pct,
                    threshold_value=PRICE_DEVIATION_PCT,
                    issuer_name=row.get("issuer_name"),
                    issuer_type=row.get("issuer_type"),
                    credit_rating=row.get("credit_rating"),
                    instrument_category=_alert_instrument_category(row),
                )
                count += 1

        return count

    # ─── Check 2: Rating Issues ───────────────────────────────────────────────

    async def _check_rating_issues(self, limit: int) -> int:
        """Alert for bonds with ratings below AA- or negative outlook."""
        count = 0
        allowed_isins = await self._allowed_isins("default")
        if not allowed_isins:
            return 0
        rows = await fetch_all(
            """
                        SELECT isin, issuer_name, credit_rating, issuer_type, instrument_category
            FROM   universal_bond_master
            WHERE  is_active = 1
              AND  issuer_type NOT IN ('G-SEC', 'T-BILL', 'SDL')
            LIMIT :lim
            """,
            {"lim": limit},
        )

        for row in (rows or []):
            isin = str(row.get("isin") or "").upper()
            if isin not in allowed_isins:
                continue
            rating = _normalize_rating(row.get("credit_rating"), row.get("issuer_type"))
            rank = RATING_ORDER.get(rating, 99)

            # Below AA- threshold
            if rank > 4 and rank < 99:
                severity = "CRITICAL" if rank >= 8 else "WARNING"
                await self._persist_alert(
                    isin=isin,
                    alert_type="RATING_DOWNGRADE",
                    severity=severity,
                    title=f"Rating {rating} below AA- threshold for {row['issuer_name']}",
                    detail=f"Current rating {rating} is below minimum PF investment threshold (AA-)",
                    metric_value=float(rank),
                    threshold_value=4.0,
                    issuer_name=row.get("issuer_name"),
                    issuer_type=row.get("issuer_type"),
                    credit_rating=row.get("credit_rating"),
                    instrument_category=_alert_instrument_category(row),
                )
                count += 1

        return count

    # ─── Check 3: Yield Spread Anomalies ──────────────────────────────────────

    async def _check_yield_spread_anomalies(self, limit: int) -> int:
        """Alert if yield spread deviates significantly from rating peers."""
        count = 0
        as_of = date.today()
        allowed_isins = await self._allowed_isins("default")
        if not allowed_isins:
            return 0
        rows = await fetch_all(
            """
            SELECT b.isin, b.issuer_name, b.credit_rating, b.issuer_type,
                     b.instrument_category, m.spread_over_gsec, m.ytm, m.price_date
            FROM   universal_bond_master b
            JOIN   unified_market_data m ON b.isin = m.isin
            WHERE  m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = b.isin)
              AND  b.is_active = 1
              AND  b.issuer_type NOT IN ('G-SEC', 'T-BILL')
            LIMIT :lim
            """,
            {"lim": limit},
        )

        for row in (rows or []):
            isin = str(row.get("isin") or "").upper()
            if isin not in allowed_isins:
                continue
            price_date = _price_date_text(row.get("price_date"))
            if not _is_market_date_fresh(price_date, as_of=as_of):
                continue
            rating = _normalize_rating(row.get("credit_rating"), row.get("issuer_type"))
            spread = row.get("spread_over_gsec") or 0
            typical = TYPICAL_SPREAD_BPS.get(rating, 100)
            actual_bps = spread * 100 if spread < 10 else spread
            deviation = abs(actual_bps - typical)

            if deviation > YIELD_SPREAD_ABNORMAL_BPS:
                direction = "wider" if actual_bps > typical else "tighter"
                government_backed = _is_government_backed_spread(row.get("issuer_name"), row.get("issuer_type"), row.get("credit_rating"))
                rating_label = "Government-backed" if government_backed else ("Sovereign" if rating == "SOVEREIGN" else (rating if rating != "UNRATED" else "Unrated"))
                alert_type = GOVERNMENT_SPREAD_ALERT_TYPE if government_backed else "YIELD_SPREAD"
                severity = "INFO" if government_backed else "WARNING"
                await self._persist_alert(
                    isin=isin,
                    alert_type=alert_type,
                    severity=severity,
                    title=f"Abnormal spread for {row['issuer_name']} ({rating_label})",
                    detail=(
                        f"Spread {actual_bps:.0f}bps on {price_date} is {deviation:.0f}bps {direction} "
                        f"than typical {typical}bps for {rating_label} bonds"
                        + (". Tracked separately from corporate spread warnings." if government_backed else "")
                    ),
                    metric_value=actual_bps,
                    threshold_value=float(typical),
                    source_price_date=price_date,
                    issuer_name=row.get("issuer_name"),
                    issuer_type=row.get("issuer_type"),
                    credit_rating=row.get("credit_rating"),
                    instrument_category=_alert_instrument_category(row),
                )
                count += 1

        return count

    # ─── Check 4: Liquidity Issues ────────────────────────────────────────────

    async def _check_liquidity_issues(self, limit: int) -> int:
        """Alert for bonds with very low liquidity scores."""
        count = 0
        allowed_isins = await self._allowed_isins("default")
        if not allowed_isins:
            return 0
        rows = await fetch_all(
            """
            SELECT b.isin, b.issuer_name, b.issuer_type, m.liquidity_score
                  , b.credit_rating, b.instrument_category
            FROM   universal_bond_master b
            JOIN   unified_market_data m ON b.isin = m.isin
            WHERE  m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = b.isin)
              AND  m.liquidity_score IS NOT NULL
              AND  m.liquidity_score < :threshold
              AND  b.is_active = 1
              AND  b.issuer_type NOT IN ('G-SEC', 'T-BILL')
            LIMIT :lim
            """,
            {"threshold": LIQUIDITY_MIN_SCORE, "lim": limit},
        )

        for row in (rows or []):
            isin = str(row.get("isin") or "").upper()
            if isin not in allowed_isins:
                continue
            liq = row.get("liquidity_score") or 0
            await self._persist_alert(
                isin=isin,
                alert_type="LIQUIDITY_LOW",
                severity="WARNING",
                title=f"Low liquidity for {row['issuer_name']}",
                detail=f"Liquidity score {liq:.0f}/100 below minimum threshold {LIQUIDITY_MIN_SCORE}",
                metric_value=liq,
                threshold_value=LIQUIDITY_MIN_SCORE,
                issuer_name=row.get("issuer_name"),
                issuer_type=row.get("issuer_type"),
                credit_rating=row.get("credit_rating"),
                instrument_category=_alert_instrument_category(row),
            )
            count += 1

        return count

    # ─── Check 5: Exposure Breaches ───────────────────────────────────────────

    async def _check_exposure_breaches(self) -> int:
        """Alert for issuer concentration exceeding cap inside active portfolio."""
        count = 0
        total_row = await fetch_one(
            """
            WITH active_holdings AS (
                SELECT
                    COALESCE(NULLIF(TRIM(im.issuer), ''), NULLIF(TRIM(ubm.issuer_name), ''), h.isin) AS issuer_name,
                    COALESCE(NULLIF(TRIM(ubm.issuer_type), ''), NULLIF(TRIM(im.category), ''), 'PRIVATE') AS issuer_type,
                    COALESCE(NULLIF(TRIM(im.rating_latest), ''), NULLIF(TRIM(ubm.credit_rating), '')) AS credit_rating,
                    COALESCE(NULLIF(TRIM(ubm.instrument_category), ''), NULLIF(TRIM(im.category), ''), 'PRIVATE') AS instrument_category,
                    COALESCE(h.quantity, 0) * COALESCE(h.face_value, ubm.face_value, 1000) *
                        COALESCE(m.clean_price, 100) / 100.0 AS exposure_value
                FROM portfolio_holdings h
                LEFT JOIN instrument_master im ON im.isin = h.isin
                LEFT JOIN universal_bond_master ubm ON ubm.isin = h.isin
                LEFT JOIN unified_market_data m ON m.isin = h.isin
                    AND m.price_date = (
                        SELECT MAX(price_date)
                        FROM unified_market_data
                        WHERE isin = h.isin
                    )
                WHERE COALESCE(h.portfolio_id, 'default') = 'default'
                  AND COALESCE(h.quantity, 0) > 0
            )
            SELECT SUM(COALESCE(exposure_value, 0)) AS total_mv
            FROM active_holdings
            """
        )
        total = float((total_row or {}).get("total_mv") or 0.0)
        if total <= 0:
            return 0

        rows = await fetch_all(
            """
            WITH active_holdings AS (
                SELECT
                    COALESCE(NULLIF(TRIM(im.issuer), ''), NULLIF(TRIM(ubm.issuer_name), ''), h.isin) AS issuer_name,
                    COALESCE(NULLIF(TRIM(ubm.issuer_type), ''), NULLIF(TRIM(im.category), ''), 'PRIVATE') AS issuer_type,
                    COALESCE(NULLIF(TRIM(im.rating_latest), ''), NULLIF(TRIM(ubm.credit_rating), '')) AS credit_rating,
                    COALESCE(NULLIF(TRIM(ubm.instrument_category), ''), NULLIF(TRIM(im.category), ''), 'PRIVATE') AS instrument_category,
                    COALESCE(h.quantity, 0) * COALESCE(h.face_value, ubm.face_value, 1000) *
                        COALESCE(m.clean_price, 100) / 100.0 AS exposure_value
                FROM portfolio_holdings h
                LEFT JOIN instrument_master im ON im.isin = h.isin
                LEFT JOIN universal_bond_master ubm ON ubm.isin = h.isin
                LEFT JOIN unified_market_data m ON m.isin = h.isin
                    AND m.price_date = (
                        SELECT MAX(price_date)
                        FROM unified_market_data
                        WHERE isin = h.isin
                    )
                WHERE COALESCE(h.portfolio_id, 'default') = 'default'
                  AND COALESCE(h.quantity, 0) > 0
            )
            SELECT issuer_name,
                     issuer_type,
                     credit_rating,
                     instrument_category,
                   SUM(COALESCE(exposure_value, 0)) AS issuer_mv,
                   COUNT(*) AS lot_count
            FROM active_holdings
                 GROUP BY issuer_name, issuer_type, credit_rating, instrument_category
            """,
        )

        for row in (rows or []):
            issuer_mv = float(row.get("issuer_mv") or 0.0)
            if issuer_mv <= 0:
                continue
            pct = (issuer_mv / total) * 100
            if pct <= ISSUER_EXPOSURE_CAP_PCT:
                continue
            await self._persist_alert(
                isin=None,
                alert_type="EXPOSURE_BREACH",
                severity="CRITICAL" if pct >= (ISSUER_EXPOSURE_CAP_PCT + 5.0) else "WARNING",
                title=f"Issuer concentration breach: {row['issuer_name']}",
                detail=(
                    f"Issuer contributes {pct:.1f}% of active portfolio exposure "
                    f"(₹{issuer_mv:,.0f}) across {int(row.get('lot_count') or 0)} lots; "
                    f"cap is {ISSUER_EXPOSURE_CAP_PCT:.1f}%"
                ),
                metric_value=pct,
                threshold_value=ISSUER_EXPOSURE_CAP_PCT,
                issuer_name=row.get("issuer_name"),
                issuer_type=row.get("issuer_type"),
                credit_rating=row.get("credit_rating"),
                instrument_category=_alert_instrument_category(row),
            )
            count += 1

        return count

    # ─── Persistence ──────────────────────────────────────────────────────────

    async def _persist_alert(
        self, isin: str | None, alert_type: str, severity: str,
        title: str, detail: str, metric_value: float, threshold_value: float,
        source_price_date: str | None = None,
        issuer_name: str | None = None,
        issuer_type: str | None = None,
        credit_rating: str | None = None,
        instrument_category: str | None = None,
    ) -> None:
        """Insert alert for the day, or upgrade existing row when signal is stronger."""
        if isin:
            existing = await fetch_one(
                """SELECT id, severity, metric_value, threshold_value,
                          issuer_name, issuer_type, credit_rating, instrument_category
                   FROM unified_alerts
                   WHERE alert_date = :dt AND alert_type = :at AND isin = :isin
                   LIMIT 1""",
                {"dt": self.today, "at": alert_type, "isin": isin},
            )
        else:
            existing = await fetch_one(
                """SELECT id, severity, metric_value, threshold_value,
                          issuer_name, issuer_type, credit_rating, instrument_category
                   FROM unified_alerts
                   WHERE alert_date = :dt
                     AND alert_type = :at
                     AND COALESCE(isin, '') = ''
                     AND UPPER(COALESCE(title, '')) = :title_u
                   LIMIT 1""",
                {"dt": self.today, "at": alert_type, "title_u": str(title or "").upper()},
            )
        if existing:
            try:
                prev_metric = float(existing.get("metric_value") or 0.0)
            except Exception:
                prev_metric = 0.0
            try:
                prev_threshold = float(existing.get("threshold_value") or threshold_value or 0.0)
            except Exception:
                prev_threshold = float(threshold_value or 0.0)

            prev_rank = SEVERITY_RANK.get(str(existing.get("severity") or "").upper(), 0)
            new_rank = SEVERITY_RANK.get(str(severity or "").upper(), 0)
            prev_gap = abs(prev_metric - prev_threshold)
            new_gap = abs(float(metric_value or 0.0) - float(threshold_value or 0.0))
            existing_issuer_name = str(existing.get("issuer_name") or "").strip().upper()
            existing_issuer_type = str(existing.get("issuer_type") or "").strip().upper()
            existing_credit_rating = str(existing.get("credit_rating") or "").strip().upper()
            existing_instrument_category = str(existing.get("instrument_category") or "").strip().upper()
            incoming_issuer_name = str(issuer_name or "").strip().upper()
            incoming_issuer_type = str(issuer_type or "").strip().upper()
            incoming_credit_rating = str(credit_rating or "").strip().upper()
            incoming_instrument_category = str(instrument_category or "").strip().upper()
            metadata_needs_update = (
                (incoming_issuer_name and incoming_issuer_name != existing_issuer_name)
                or (incoming_issuer_type and incoming_issuer_type != existing_issuer_type)
                or (incoming_credit_rating and incoming_credit_rating != existing_credit_rating)
                or (incoming_instrument_category and incoming_instrument_category != existing_instrument_category)
            )

            if new_rank > prev_rank or new_gap > prev_gap:
                await execute_query(
                    """UPDATE unified_alerts
                       SET severity = :sev,
                           issuer_name = COALESCE(:issuer_name, issuer_name),
                           issuer_type = COALESCE(:issuer_type, issuer_type),
                           credit_rating = COALESCE(:credit_rating, credit_rating),
                           instrument_category = COALESCE(:instrument_category, instrument_category),
                           title = :title,
                           detail = :detail,
                           metric_value = :mv,
                           threshold_value = :tv,
                           source_price_date = COALESCE(:spd, source_price_date),
                           created_at = datetime('now'),
                           is_resolved = 0,
                           resolved_at = NULL,
                           resolved_by = NULL
                       WHERE id = :id""",
                    {
                        "id": existing.get("id"),
                        "sev": severity,
                        "title": title,
                        "detail": detail,
                        "mv": metric_value,
                        "tv": threshold_value,
                        "spd": source_price_date,
                        "issuer_name": issuer_name,
                        "issuer_type": issuer_type,
                        "credit_rating": credit_rating,
                        "instrument_category": instrument_category,
                    },
                )
            elif metadata_needs_update:
                await execute_query(
                    """UPDATE unified_alerts
                       SET issuer_name = COALESCE(:issuer_name, issuer_name),
                           issuer_type = COALESCE(:issuer_type, issuer_type),
                           credit_rating = COALESCE(:credit_rating, credit_rating),
                           instrument_category = COALESCE(:instrument_category, instrument_category)
                       WHERE id = :id""",
                    {
                        "id": existing.get("id"),
                        "issuer_name": issuer_name,
                        "issuer_type": issuer_type,
                        "credit_rating": credit_rating,
                        "instrument_category": instrument_category,
                    },
                )
            return

        await execute_query(
            """INSERT INTO unified_alerts
               (alert_date, source_price_date, isin, issuer_name, issuer_type, credit_rating, instrument_category,
                alert_type, severity, title, detail, metric_value, threshold_value)
               VALUES (:dt, :spd, :isin, :issuer_name, :issuer_type, :credit_rating, :instrument_category,
                       :at, :sev, :title, :detail, :mv, :tv)""",
            {
                "dt": self.today, "spd": source_price_date, "isin": isin, "at": alert_type, "sev": severity,
                "title": title, "detail": detail, "mv": metric_value, "tv": threshold_value,
                "issuer_name": issuer_name,
                "issuer_type": issuer_type,
                "credit_rating": credit_rating,
                "instrument_category": instrument_category,
            },
        )
