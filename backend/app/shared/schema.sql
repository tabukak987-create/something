-- ============================================================
-- Universal Bond Master Registry — Shared Schema
-- All gateway services read/write from this single source.
-- ============================================================

CREATE TABLE IF NOT EXISTS universal_bond_master (
    isin                TEXT PRIMARY KEY,
    bond_name           TEXT NOT NULL,
    issuer_name         TEXT NOT NULL,
    issuer_type         TEXT NOT NULL DEFAULT 'CORPORATE',  -- G-SEC, SDL, PSU, BANK, NBFC, CORPORATE
    coupon_rate         REAL NOT NULL DEFAULT 0,
    coupon_type         TEXT NOT NULL DEFAULT 'FIXED',      -- FIXED, FLOATING, ZERO
    coupon_frequency    INTEGER NOT NULL DEFAULT 2,
    maturity_date       TEXT NOT NULL,
    issue_date          TEXT,
    face_value          REAL NOT NULL DEFAULT 1000,
    credit_rating       TEXT,
    rating_agency       TEXT,
    sector              TEXT,
    instrument_category TEXT,
    sub_sector          TEXT,
    listing_exchange    TEXT DEFAULT 'NSE',
    is_pf_eligible      INTEGER NOT NULL DEFAULT 0,
    is_active           INTEGER NOT NULL DEFAULT 1,
    source_name         TEXT,
    source_type         TEXT,
    ingestion_method    TEXT,
    source_reference    TEXT,
    timestamp_ingested  TEXT,
    timestamp_effective TEXT,
    provenance_json     TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ubm_issuer_type  ON universal_bond_master(issuer_type);
CREATE INDEX IF NOT EXISTS idx_ubm_credit_rating ON universal_bond_master(credit_rating);
CREATE INDEX IF NOT EXISTS idx_ubm_pf_eligible  ON universal_bond_master(is_pf_eligible);
CREATE INDEX IF NOT EXISTS idx_ubm_maturity     ON universal_bond_master(maturity_date);

-- ──────────────────────────────────────────────────────────────
-- Unified Market Data (all price sources write here)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unified_market_data (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL REFERENCES universal_bond_master(isin) ON DELETE CASCADE,
    price_date      TEXT NOT NULL,
    clean_price     REAL,
    dirty_price     REAL,
    macaulay_duration REAL,
    ask_yield       REAL,
    modified_duration REAL,
    dv01            REAL,
    convexity       REAL,
    spread_over_gsec REAL,
    z_spread        REAL,
    source_reference    TEXT,
    source_sheet_name   TEXT,
    source_row_number   INTEGER,
    traded_volume   REAL,
    liquidity_score REAL,
    data_source     TEXT NOT NULL DEFAULT 'SYNTHETIC',
    source_name     TEXT,
    source_type     TEXT,
    ingestion_method TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json TEXT,
    last_updated    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, price_date)
);

CREATE INDEX IF NOT EXISTS idx_umd_isin         ON unified_market_data(isin);
CREATE INDEX IF NOT EXISTS idx_umd_price_date   ON unified_market_data(price_date DESC);
CREATE INDEX IF NOT EXISTS idx_umd_source       ON unified_market_data(data_source);

-- ──────────────────────────────────────────────────────────────
-- Service Health Log (audit trail for background tasks)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS service_health_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name    TEXT NOT NULL,
    check_type      TEXT NOT NULL,  -- STARTUP | SCHEDULED_REFRESH | HEALTHCHECK
    status          TEXT NOT NULL,  -- OK | WARN | ERROR
    message         TEXT,
    duration_ms     INTEGER,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_shl_service      ON service_health_log(service_name);
CREATE INDEX IF NOT EXISTS idx_shl_status       ON service_health_log(status);

-- ──────────────────────────────────────────────────────────────
-- Broker Quotes (sourced from bond_quote_system)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unified_broker_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    broker_name     TEXT NOT NULL,
    quote_date      TEXT NOT NULL,
    bid_price       REAL,
    ask_price       REAL,
    bid_yield       REAL,
    ask_yield       REAL,
    lot_size        INTEGER,
    settlement_days INTEGER DEFAULT 1,
    source_file     TEXT,
    source_name     TEXT,
    source_type     TEXT,
    ingestion_method TEXT,
    source_reference TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json TEXT,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ubq_isin         ON unified_broker_quotes(isin);
CREATE INDEX IF NOT EXISTS idx_ubq_broker       ON unified_broker_quotes(broker_name);
CREATE INDEX IF NOT EXISTS idx_ubq_date         ON unified_broker_quotes(quote_date DESC);

-- ──────────────────────────────────────────────────────────────
-- Compliance Audit Trail (sourced from compliance_engine)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unified_compliance_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    check_type      TEXT NOT NULL,  -- PRE_TRADE | POST_TRADE | BATCH
    overall_status  TEXT NOT NULL,  -- PASS | FAIL | WARN
    violations_json TEXT,           -- JSON array of violations
    checked_by      TEXT DEFAULT 'system',
    checked_at      TEXT NOT NULL DEFAULT (datetime('now')),
    source_name     TEXT,
    source_type     TEXT,
    ingestion_method TEXT,
    source_reference TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_uca_isin         ON unified_compliance_audit(isin);
CREATE INDEX IF NOT EXISTS idx_uca_status       ON unified_compliance_audit(overall_status);
CREATE INDEX IF NOT EXISTS idx_uca_date         ON unified_compliance_audit(checked_at DESC);

-- ──────────────────────────────────────────────────────────────
-- Users & IAM (JWT auth, RBAC)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'VIEWER',  -- SUPER_ADMIN|ADMIN|PORTFOLIO|TRUSTEE|COMPLIANCE|AUDITOR|VIEWER
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_login      TEXT,
    password_changed_at TEXT,
    mfa_secret          TEXT,     -- TOTP secret (base32) for MFA (M4 remediation)
    mfa_secret_pending  TEXT      -- Pending MFA secret awaiting confirmation
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email    ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role     ON users(role);

-- ──────────────────────────────────────────────────────────────
-- Intelligent Bond Scoring (Institutional Suitability Score)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unified_bond_scores (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    isin                TEXT NOT NULL REFERENCES universal_bond_master(isin) ON DELETE CASCADE,
    score_date          TEXT NOT NULL,
    -- Dimension scores (0-100)
    value_score         REAL,        -- Spread vs rating peers
    liquidity_score     REAL,        -- Volume percentile
    credit_score        REAL,        -- Rating outlook
    alm_fit_score       REAL,        -- Maturity match
    broker_score        REAL,        -- Broker discount
    -- Composite
    iss_score           REAL NOT NULL, -- Institutional Suitability Score
    iss_rank            INTEGER,
    -- Analysis
    recommendation      TEXT,          -- STRONG_BUY | BUY | HOLD | SELL | AVOID
    overall_explanation TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, score_date)
);

CREATE INDEX IF NOT EXISTS idx_ubs_isin ON unified_bond_scores(isin);
CREATE INDEX IF NOT EXISTS idx_ubs_date ON unified_bond_scores(score_date);
CREATE INDEX IF NOT EXISTS idx_ubs_score ON unified_bond_scores(iss_score DESC);

-- ──────────────────────────────────────────────────────────────
-- Alert System (price deviations, rating downgrades, exposure breaches)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unified_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_date      TEXT NOT NULL,
    source_price_date TEXT,
    isin            TEXT,
    issuer_name     TEXT,
    issuer_type     TEXT,
    credit_rating   TEXT,
    instrument_category TEXT,
    alert_type      TEXT NOT NULL,       -- PRICE_DEVIATION | RATING_DOWNGRADE | YIELD_SPREAD_ABNORMAL | EXPOSURE_BREACH | LIQUIDITY_LOW
    severity        TEXT NOT NULL DEFAULT 'WARNING',  -- INFO | WARNING | CRITICAL
    title           TEXT NOT NULL,
    detail          TEXT,
    metric_value    REAL,
    threshold_value REAL,
    is_resolved     INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT,
    resolved_by     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ua_date      ON unified_alerts(alert_date DESC);
CREATE INDEX IF NOT EXISTS idx_ua_source_price_date ON unified_alerts(source_price_date DESC);
CREATE INDEX IF NOT EXISTS idx_ua_isin      ON unified_alerts(isin);
CREATE INDEX IF NOT EXISTS idx_ua_type      ON unified_alerts(alert_type);
CREATE INDEX IF NOT EXISTS idx_ua_severity  ON unified_alerts(severity);

-- ──────────────────────────────────────────────────────────────
-- Decision Audit Trail (full decision log with overrides)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unified_decision_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    decision_date   TEXT NOT NULL,
    action          TEXT NOT NULL,       -- SCORE | COMPLIANCE_CHECK | ALERT | OVERRIDE | MANUAL_APPROVAL
    decision        TEXT,                -- APPROVE | REJECT | OVERRIDE | HOLD
    reason          TEXT,
    performed_by    TEXT DEFAULT 'system',
    metadata_json   TEXT,                -- JSON blob for extra context
    source_name     TEXT,
    source_type     TEXT,
    ingestion_method TEXT,
    source_reference TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_uda_isin  ON unified_decision_audit(isin);
CREATE INDEX IF NOT EXISTS idx_uda_date  ON unified_decision_audit(decision_date DESC);

-- ──────────────────────────────────────────────────────────────
-- Decision Orchestrator Core (registry + explainability + overrides)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS decision_registry (
    decision_id            TEXT PRIMARY KEY,
    decision_type          TEXT NOT NULL,
    decision_outcome       TEXT,
    module_name            TEXT NOT NULL,
    source_endpoint        TEXT,
    status                 TEXT NOT NULL DEFAULT 'RECORDED',
    actor                  TEXT NOT NULL DEFAULT 'system',
    portfolio_snapshot_ref TEXT,
    market_snapshot_ref    TEXT,
    policy_snapshot_ref    TEXT,
    policy_version_id      TEXT,
    data_snapshot_ids_json TEXT,
    calculation_version_id TEXT,
    input_hash             TEXT,
    output_hash            TEXT,
    trace_timestamp        TEXT,
    intent_id              TEXT,
    idempotency_key        TEXT,
    explanation_required   INTEGER NOT NULL DEFAULT 1,
    pipeline_stage         TEXT,
    metadata_json          TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dr_type ON decision_registry(decision_type);
CREATE INDEX IF NOT EXISTS idx_dr_status ON decision_registry(status);
CREATE INDEX IF NOT EXISTS idx_dr_created ON decision_registry(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dr_intent ON decision_registry(intent_id);
CREATE INDEX IF NOT EXISTS idx_dr_trace_timestamp ON decision_registry(trace_timestamp DESC);

CREATE TABLE IF NOT EXISTS decision_explanations (
    decision_id     TEXT PRIMARY KEY,
    schema_version  TEXT,
    explanation_json TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_de_updated ON decision_explanations(updated_at DESC);

CREATE TABLE IF NOT EXISTS decision_override_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id         TEXT NOT NULL,
    requested_by        TEXT NOT NULL,
    reason              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    required_approvals  INTEGER NOT NULL DEFAULT 2,
    created_at          TEXT NOT NULL,
    resolved_at         TEXT,
    resolved_by         TEXT,
    resolution_notes    TEXT
);

CREATE INDEX IF NOT EXISTS idx_dor_decision ON decision_override_requests(decision_id);
CREATE INDEX IF NOT EXISTS idx_dor_status ON decision_override_requests(status);

CREATE TABLE IF NOT EXISTS decision_override_approvals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    override_request_id INTEGER NOT NULL,
    decision_id         TEXT NOT NULL,
    approved_by         TEXT NOT NULL,
    approval_note       TEXT,
    approved_at         TEXT NOT NULL,
    UNIQUE(override_request_id, approved_by)
);

CREATE INDEX IF NOT EXISTS idx_doa_request ON decision_override_approvals(override_request_id);
CREATE INDEX IF NOT EXISTS idx_doa_decision ON decision_override_approvals(decision_id);

CREATE TABLE IF NOT EXISTS decision_intent (
    intent_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT UNIQUE,
    decision_type TEXT NOT NULL,
    module_name TEXT NOT NULL,
    source_endpoint TEXT,
    status TEXT NOT NULL DEFAULT 'INTENT_LOGGED',
    request_payload_json TEXT NOT NULL,
    execution_payload_json TEXT,
    context_json TEXT,
    error_text TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_di_status ON decision_intent(status);
CREATE INDEX IF NOT EXISTS idx_di_updated ON decision_intent(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_di_next_retry ON decision_intent(next_retry_at);

CREATE TABLE IF NOT EXISTS decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    snapshot_role TEXT NOT NULL,
    snapshot_ref TEXT,
    snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, snapshot_role)
);

CREATE INDEX IF NOT EXISTS idx_ds_decision ON decision_snapshots(decision_id);
CREATE INDEX IF NOT EXISTS idx_ds_role ON decision_snapshots(snapshot_role);

CREATE TABLE IF NOT EXISTS decision_alternatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    rank_order INTEGER NOT NULL,
    alternative_id TEXT NOT NULL,
    alternative_label TEXT,
    status TEXT NOT NULL,
    score REAL,
    rejection_reason TEXT,
    rule_code TEXT,
    metrics_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, rank_order)
);

CREATE INDEX IF NOT EXISTS idx_da_decision ON decision_alternatives(decision_id);
CREATE INDEX IF NOT EXISTS idx_da_status ON decision_alternatives(status);

CREATE TABLE IF NOT EXISTS decision_trace_graph (
    decision_id TEXT PRIMARY KEY,
    graph_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_replay_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    replay_status TEXT NOT NULL,
    expected_output_hash TEXT,
    actual_output_hash TEXT,
    details_json TEXT,
    run_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drr_decision ON decision_replay_runs(decision_id);
CREATE INDEX IF NOT EXISTS idx_drr_run_at ON decision_replay_runs(run_at DESC);

-- --------------------------------------------------------------
-- Legacy unified_portfolio is intentionally retired.
-- Compatibility is provided later via a read-only VIEW that is
-- derived from institutional raw tables (no derived storage).
-- --------------------------------------------------------------

-- --------------------------------------------------------------
-- Institutional Raw Portfolio Core (No Derived Storage)
-- --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS instrument_master (
    isin                 TEXT PRIMARY KEY,
    instrument_name      TEXT NOT NULL,
    issuer               TEXT NOT NULL,
    coupon_rate          REAL NOT NULL,
    maturity_date        TEXT NOT NULL,
    put_date             TEXT,
    call_date            TEXT,
    sector               TEXT,
    category             TEXT,
    instrument_category  TEXT,
    rating_latest        TEXT,
    rating_agency        TEXT,
    payment_frequency    INTEGER NOT NULL DEFAULT 2,
    day_count_convention TEXT NOT NULL DEFAULT 'ACT/365',
    source_name          TEXT,
    source_type          TEXT,
    ingestion_method     TEXT,
    source_reference     TEXT,
    timestamp_ingested   TEXT,
    timestamp_effective  TEXT,
    provenance_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_im_maturity ON instrument_master(maturity_date);
CREATE INDEX IF NOT EXISTS idx_im_issuer   ON instrument_master(issuer);

CREATE TABLE IF NOT EXISTS portfolio_holdings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id  TEXT NOT NULL,
    isin          TEXT NOT NULL REFERENCES instrument_master(isin) ON DELETE CASCADE,
    purchase_date TEXT,
    quantity      REAL NOT NULL,
    purchase_price REAL NOT NULL,
    face_value    REAL NOT NULL DEFAULT 1000,
    instrument_category TEXT,
    source_name   TEXT,
    source_type   TEXT,
    ingestion_method TEXT,
    source_reference TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_ph_portfolio_id ON portfolio_holdings(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_ph_isin         ON portfolio_holdings(isin);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ph_portfolio_isin_unique ON portfolio_holdings(portfolio_id, isin);

CREATE TABLE IF NOT EXISTS portfolio_upload_batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL UNIQUE,
    trust_id            TEXT NOT NULL,
    portfolio_id        TEXT NOT NULL,
    file_name           TEXT NOT NULL,
    mime_type           TEXT,
    file_hash           TEXT NOT NULL,
    raw_content         BYTEA,
    raw_storage_path    TEXT,
    uploaded_by         TEXT NOT NULL,
    uploaded_at         TEXT NOT NULL DEFAULT (datetime('now')),
    batch_status        TEXT NOT NULL DEFAULT 'RECEIVED',
    row_count           INTEGER NOT NULL DEFAULT 0,
    accepted_count      INTEGER NOT NULL DEFAULT 0,
    rejected_count      INTEGER NOT NULL DEFAULT 0,
    version_number      INTEGER,
    validation_report_json TEXT,
    source_reference    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pub_trust_batch ON portfolio_upload_batches(trust_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_pub_hash ON portfolio_upload_batches(file_hash);
CREATE INDEX IF NOT EXISTS idx_pub_uploaded_at ON portfolio_upload_batches(uploaded_at DESC);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    attempt_at TEXT NOT NULL,
    ip_address TEXT,
    success INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_login_user ON login_attempts(username);

CREATE TABLE IF NOT EXISTS mfa_backup_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mfa_backup_username ON mfa_backup_codes(username);
CREATE INDEX IF NOT EXISTS idx_mfa_backup_used ON mfa_backup_codes(username, used);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_at TEXT,
    ip_address TEXT
);

CREATE INDEX IF NOT EXISTS idx_prt_username ON password_reset_tokens(username);
CREATE INDEX IF NOT EXISTS idx_prt_token_hash ON password_reset_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_prt_used ON password_reset_tokens(username, used);

CREATE TABLE IF NOT EXISTS portfolio_upload_rows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL,
    portfolio_id        TEXT NOT NULL,
    row_number          INTEGER NOT NULL,
    source_sheet_name   TEXT,
    stage               TEXT NOT NULL,
    status              TEXT NOT NULL,
    raw_json            TEXT,
    normalized_json     TEXT,
    validated_json      TEXT,
    canonical_json      TEXT,
    error_json          TEXT,
    warning_json        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(batch_id, row_number, stage)
);

CREATE INDEX IF NOT EXISTS idx_pur_batch ON portfolio_upload_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_pur_portfolio ON portfolio_upload_rows(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_pur_stage ON portfolio_upload_rows(stage, status);

CREATE TABLE IF NOT EXISTS portfolio_upload_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL,
    trust_id            TEXT NOT NULL,
    portfolio_id        TEXT NOT NULL,
    version_number      INTEGER NOT NULL,
    approved_by         TEXT,
    approved_at         TEXT NOT NULL DEFAULT (datetime('now')),
    version_status      TEXT NOT NULL DEFAULT 'APPROVED',
    summary_json        TEXT,
    UNIQUE(trust_id, portfolio_id, version_number),
    UNIQUE(batch_id)
);

CREATE INDEX IF NOT EXISTS idx_puv_portfolio_version ON portfolio_upload_versions(portfolio_id, version_number DESC);

CREATE TABLE IF NOT EXISTS portfolio_validation_reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL UNIQUE,
    portfolio_id        TEXT NOT NULL,
    trust_id            TEXT NOT NULL,
    report_json         TEXT NOT NULL,
    report_status       TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pvr_portfolio ON portfolio_validation_reports(portfolio_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_psa_unique ON portfolio_sheet_allocations(isin, category_bucket, sector_bucket);

CREATE TABLE IF NOT EXISTS market_prices (
    isin    TEXT NOT NULL REFERENCES instrument_master(isin) ON DELETE CASCADE,
    date    TEXT NOT NULL,
    price   REAL NOT NULL,
    yield   REAL,
    source  TEXT,
    source_name   TEXT,
    source_type   TEXT,
    ingestion_method TEXT,
    source_reference TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json TEXT,
    UNIQUE(isin, date, source)
);

CREATE INDEX IF NOT EXISTS idx_mp_isin_date ON market_prices(isin, date DESC);

CREATE TABLE IF NOT EXISTS cashflows (
    isin             TEXT NOT NULL REFERENCES instrument_master(isin) ON DELETE CASCADE,
    payment_date     TEXT NOT NULL,
    coupon_amount    REAL NOT NULL,
    principal_amount REAL NOT NULL,
    source_name      TEXT,
    source_type      TEXT,
    ingestion_method  TEXT,
    source_reference  TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json   TEXT,
    UNIQUE(isin, payment_date)
);

CREATE INDEX IF NOT EXISTS idx_cf_isin_date ON cashflows(isin, payment_date);

-- --------------------------------------------------------------
-- Legacy Compatibility View (Read-Only)
-- --------------------------------------------------------------

CREATE VIEW IF NOT EXISTS unified_portfolio AS
SELECT
    im.isin AS isin,
    im.issuer AS issuer_name,
    COALESCE(ubm.face_value, 100.0) AS face_value,
    SUM(
        COALESCE(ph.quantity, 0.0)
        * COALESCE(ubm.face_value, 100.0)
        * COALESCE(ph.purchase_price, 0.0)
        / 100.0
    ) AS book_value,
    SUM(COALESCE(ph.quantity, 0.0)) AS quantity,
    SUM(
        COALESCE(ph.quantity, 0.0)
        * COALESCE(ubm.face_value, 100.0)
        * COALESCE(lp.price, ph.purchase_price, 0.0)
        / 100.0
    ) AS market_value,
    MIN(ph.purchase_date) AS purchase_date,
    im.maturity_date AS maturity_date,
    im.coupon_rate AS coupon_rate,
    NULL AS ytm_at_purchase,
    im.category AS category,
    im.sector AS sector,
    im.rating_latest AS rating,
    datetime('now') AS last_updated
FROM portfolio_holdings ph
INNER JOIN instrument_master im ON im.isin = ph.isin
LEFT JOIN universal_bond_master ubm ON ubm.isin = ph.isin
LEFT JOIN (
    SELECT
        m.isin,
        (
            SELECT m2.price
            FROM market_prices m2
            WHERE m2.isin = m.isin
            ORDER BY m2.date DESC, COALESCE(m2.source, '') DESC
            LIMIT 1
        ) AS price
    FROM market_prices m
    GROUP BY m.isin
) lp ON lp.isin = ph.isin
GROUP BY
    im.isin,
    im.issuer,
    ubm.face_value,
    im.maturity_date,
    im.coupon_rate,
    im.category,
    im.sector,
    im.rating_latest;

CREATE VIEW IF NOT EXISTS portfolio_current_positions AS
SELECT * FROM unified_portfolio;

-- --------------------------------------------------------------
-- Trade Log (full history of every buy/sell)
-- --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    issuer_name     TEXT,
    action          TEXT NOT NULL,          -- BUY | SELL
    quantity        INTEGER NOT NULL,
    price           REAL NOT NULL,
    total_value     REAL,
    ytm_at_trade    REAL,
    trade_date      TEXT NOT NULL,
    settlement_date TEXT,
    broker          TEXT,
    notes           TEXT,
    executed_by     TEXT DEFAULT 'admin',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tl_isin   ON trade_log(isin);
CREATE INDEX IF NOT EXISTS idx_tl_date   ON trade_log(trade_date DESC);

-- --------------------------------------------------------------
-- Quarantine table for invalid / suspicious portfolio records
-- --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS portfolio_data_quarantine (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system       TEXT NOT NULL DEFAULT 'UPLOAD',
    source_file         TEXT,
    source_row_number   INTEGER,
    isin                TEXT,
    issuer_name         TEXT,
    reason_code         TEXT NOT NULL,
    reason_detail       TEXT,
    raw_payload_json    TEXT,
    status              TEXT NOT NULL DEFAULT 'OPEN', -- OPEN | REVIEWED | RESOLVED | DISMISSED
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at         TEXT,
    reviewed_by         TEXT
);

CREATE INDEX IF NOT EXISTS idx_pdq_status ON portfolio_data_quarantine(status);
CREATE INDEX IF NOT EXISTS idx_pdq_isin ON portfolio_data_quarantine(isin);
CREATE INDEX IF NOT EXISTS idx_pdq_created ON portfolio_data_quarantine(created_at DESC);

-- ──────────────────────────────────────────────────────────────
-- Broker Daily Quotes — fully parsed, same-day comparable
-- UNIQUE(isin, broker_name, quote_date) ensures one record per
-- broker per ISIN per day.  Historical rows are preserved.
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS broker_daily_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    broker_name     TEXT NOT NULL,
    quote_date      TEXT NOT NULL,          -- YYYY-MM-DD (the date the broker sent this)
    bond_name       TEXT,
    category        TEXT,                   -- GOI | SDL | PSU | CORPORATE | BANK etc
    instrument_category TEXT,
    coupon_rate     REAL,
    maturity_date   TEXT,
    rating          TEXT,
    rating_agency   TEXT,
    -- Price fields
    clean_price     REAL,                   -- Price per ₹100 face value
    ytm             REAL,                   -- Annual YTM (decimal, e.g. 0.0687)
    ytm_semi        REAL,                   -- Semi-annual YTM
    ytm_screen      REAL,                   -- Screen/market YTM (broker's reference)
    -- Optional bid/offer semantics
    bid_price       REAL,
    ask_price       REAL,
    bid_yield       REAL,
    ask_yield       REAL,
    -- Quantity
    lot_size        INTEGER,
    quantity_lacs   REAL,                   -- Available quantity in lacs (INR)
    -- Metadata
    put_call_date   TEXT,
    ip_dates        TEXT,
    settlement_days INTEGER DEFAULT 1,
    source_file     TEXT,
    source_format   TEXT,                   -- EXCEL_RFS | EXCEL_NUVAMA | EXCEL_GENERIC | EXCEL_DARASHAW | PDF_NUVAMA | PDF_BIRLA
    source_name     TEXT,
    source_type     TEXT,
    ingestion_method TEXT,
    source_reference TEXT,
    timestamp_ingested TEXT,
    timestamp_effective TEXT,
    provenance_json TEXT,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, broker_name, quote_date)
);

-- ──────────────────────────────────────────────────────────────
-- Governance Ledger and Calculation Trace
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS data_provenance_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_name TEXT,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    ingestion_method TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    timestamp_ingested TEXT NOT NULL,
    timestamp_effective TEXT NOT NULL,
    confidence_level TEXT NOT NULL DEFAULT 'low',
    actor TEXT NOT NULL DEFAULT 'system',
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entity_type, entity_id, field_name, source_reference, timestamp_ingested)
);

CREATE INDEX IF NOT EXISTS idx_dpl_entity ON data_provenance_ledger(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_dpl_source ON data_provenance_ledger(source_name, source_type);

CREATE TABLE IF NOT EXISTS calculation_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    calculation_name TEXT NOT NULL,
    formula_version TEXT NOT NULL,
    input_values_snapshot TEXT NOT NULL,
    output_values_snapshot TEXT NOT NULL,
    source_snapshot_json TEXT,
    computed_timestamp TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_calculation_audit_entity ON calculation_audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_calculation_audit_calc ON calculation_audit_log(calculation_name, computed_timestamp DESC);

CREATE TABLE IF NOT EXISTS im_document_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_hash TEXT,
    file_path TEXT,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    ingestion_method TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    timestamp_ingested TEXT NOT NULL,
    timestamp_effective TEXT NOT NULL,
    confidence_level TEXT NOT NULL DEFAULT 'medium',
    extracted_summary_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_imdr_isin ON im_document_registry(isin);

CREATE INDEX IF NOT EXISTS idx_bdq_isin        ON broker_daily_quotes(isin);
CREATE INDEX IF NOT EXISTS idx_bdq_broker      ON broker_daily_quotes(broker_name);
CREATE INDEX IF NOT EXISTS idx_bdq_date        ON broker_daily_quotes(quote_date DESC);
CREATE INDEX IF NOT EXISTS idx_bdq_isin_date   ON broker_daily_quotes(isin, quote_date DESC);

-- ──────────────────────────────────────────────────────────────
-- Broker Profiles — computed behaviour analytics per broker
-- Updated after each ingest cycle
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS broker_profiles (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_name           TEXT NOT NULL UNIQUE,
    -- Coverage
    total_isins_quoted    INTEGER DEFAULT 0,
    active_days           INTEGER DEFAULT 0,   -- number of distinct dates with quotes
    first_seen_date       TEXT,
    last_seen_date        TEXT,
    -- Pricing behaviour (basis points relative to average across brokers on same day)
    avg_ytm_premium_bps   REAL,   -- positive = broker quotes higher yield (cheaper price)
    avg_spread_bps        REAL,   -- avg bid-ask spread in bps
    pricing_consistency   REAL,   -- stdev of ytm_premium across days (lower = more consistent)
    -- Trend
    trend_direction       TEXT,   -- TIGHTENING | WIDENING | STABLE
    trend_slope_bps       REAL,   -- bps per day change in ytm_premium (recent 7 days)
    -- Scores
    reliability_score     REAL,   -- 0-100 (coverage × consistency × recency)
    -- Human-readable analytics
    pattern_summary       TEXT,   -- e.g. "Quotes 8-12bps above market, tightening spreads"
    behaviour_tags        TEXT,   -- JSON array e.g. ["aggressive_pricer","high_coverage"]
    -- Metadata
    last_computed         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bp_broker ON broker_profiles(broker_name);
CREATE INDEX IF NOT EXISTS idx_bp_score  ON broker_profiles(reliability_score DESC);

-- ──────────────────────────────────────────────────────────────
-- Broker Quote Ingest Log — track each file upload/parse run
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS broker_ingest_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    broker_name     TEXT,
    quote_date      TEXT,
    source_format   TEXT,
    quotes_parsed   INTEGER DEFAULT 0,
    quotes_inserted INTEGER DEFAULT 0,
    quotes_updated  INTEGER DEFAULT 0,
    errors_json     TEXT,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bil_date    ON broker_ingest_log(ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_bil_broker  ON broker_ingest_log(broker_name);

-- ──────────────────────────────────────────────────────────────
-- Price History — Intraday & historical tick cache
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    price_date      TEXT NOT NULL,
    price_time      TEXT,                   -- HH:MM:SS if intraday
    clean_price     REAL,
    dirty_price     REAL,
    accrued_interest REAL,
    ytm             REAL,
    bid_price       REAL,
    ask_price       REAL,
    bid_yield       REAL,
    ask_yield       REAL,
    traded_volume   REAL,
    data_source     TEXT NOT NULL DEFAULT 'SYNTHETIC',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, price_date, price_time)
);

CREATE INDEX IF NOT EXISTS idx_ph_isin      ON price_history(isin);
CREATE INDEX IF NOT EXISTS idx_ph_date      ON price_history(price_date DESC);

-- ──────────────────────────────────────────────────────────────
-- Risk Metrics — per-date portfolio-level risk snapshot
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS risk_metrics (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    calc_date               TEXT NOT NULL,
    -- Portfolio-level
    total_market_value      REAL,
    total_accrued_interest  REAL,
    total_dirty_value       REAL,
    weighted_ytm            REAL,
    weighted_mod_duration   REAL,
    weighted_mac_duration   REAL,
    weighted_avg_maturity   REAL,
    convexity               REAL,
    avg_credit_score        REAL,
    liquidity_score         REAL,
    portfolio_xirr          REAL,
    -- VaR
    var_95_1d               REAL,
    var_99_1d               REAL,
    expected_shortfall_95   REAL,
    -- Stress
    stress_rate_up_25       REAL,
    stress_rate_up_50       REAL,
    stress_rate_up_100      REAL,
    stress_rate_down_50     REAL,
    stress_spread_50        REAL,
    stress_largest_default  REAL,
    -- Health score
    health_score            REAL,
    health_credit_score     REAL,
    health_diversification  REAL,
    health_duration_score   REAL,
    health_liquidity_score  REAL,
    health_concentration    REAL,
    strengths_json          TEXT,
    weaknesses_json         TEXT,
    suggestions_json        TEXT,
    -- Meta
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(calc_date)
);

CREATE INDEX IF NOT EXISTS idx_rm_date ON risk_metrics(calc_date DESC);

-- ──────────────────────────────────────────────────────────────
-- Scenario Results — stress test outputs
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS scenario_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calc_date       TEXT NOT NULL,
    scenario_name   TEXT NOT NULL,
    scenario_type   TEXT NOT NULL,           -- RATE_SHOCK | SPREAD_SHOCK | CREDIT | MACRO
    -- Inputs
    rate_shift_bps  REAL,
    spread_shift_bps REAL,
    -- Outputs
    portfolio_pnl   REAL,
    portfolio_pnl_pct REAL,
    new_nav         REAL,
    duration_impact REAL,
    liquidity_stress TEXT,
    rating_stress   TEXT,
    details_json    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(calc_date, scenario_name)
);

CREATE INDEX IF NOT EXISTS idx_sr_date ON scenario_results(calc_date DESC);

-- ──────────────────────────────────────────────────────────────
-- Bond Issue Detection — per-bond health status
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bond_health_status (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    check_date      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'HEALTHY',  -- HEALTHY | MONITOR | RISK
    -- Individual flags
    rating_downgrade        INTEGER DEFAULT 0,
    rating_outlook_negative INTEGER DEFAULT 0,
    spread_widening         INTEGER DEFAULT 0,
    low_liquidity           INTEGER DEFAULT 0,
    price_drop              INTEGER DEFAULT 0,
    high_concentration      INTEGER DEFAULT 0,
    maturity_clustering     INTEGER DEFAULT 0,
    call_put_risk           INTEGER DEFAULT 0,
    -- Details
    issues_json     TEXT,
    severity_score  REAL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, check_date)
);

CREATE INDEX IF NOT EXISTS idx_bhs_isin ON bond_health_status(isin);
CREATE INDEX IF NOT EXISTS idx_bhs_date ON bond_health_status(check_date DESC);
CREATE INDEX IF NOT EXISTS idx_bhs_status ON bond_health_status(status);

-- ──────────────────────────────────────────────────────────────
-- Coupon Schedule — per-bond coupon payment dates
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS coupon_schedule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    coupon_date     TEXT NOT NULL,
    coupon_amount   REAL,
    is_paid         INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, coupon_date)
);

CREATE INDEX IF NOT EXISTS idx_cs_isin ON coupon_schedule(isin);
CREATE INDEX IF NOT EXISTS idx_cs_date ON coupon_schedule(coupon_date);

-- ──────────────────────────────────────────────────────────────
-- Exchange Trades — real trade data pulled from NSE / BSE debt
-- market APIs.  One row per ISIN per date per exchange.
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS exchange_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    isin            TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    exchange        TEXT NOT NULL,   -- NSE | BSE
    bond_name       TEXT,
    last_price      REAL,            -- Last traded price (per ₹100 face)
    prev_close      REAL,
    price_change    REAL,
    pct_change      REAL,
    open_price      REAL,
    high_price      REAL,
    low_price       REAL,
    high_52w        REAL,
    low_52w         REAL,
    total_volume    REAL,            -- Quantity (units / lots)
    total_value     REAL,            -- Total traded value (INR)
    number_of_trades INTEGER,
    ytm             REAL,            -- Exchange-reported YTM if available
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, trade_date, exchange)
);

CREATE INDEX IF NOT EXISTS idx_et_isin     ON exchange_trades(isin);
CREATE INDEX IF NOT EXISTS idx_et_date     ON exchange_trades(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_et_exchange ON exchange_trades(exchange);

-- ──────────────────────────────────────────────────────────────
-- Bond IM Registry — Information Memorandum metadata
-- Sourced from NSE corporate filings, BSE MemDisclose, SEBI.
-- Unique per (ISIN, source) so all three can coexist.
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bond_im_registry (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    isin             TEXT NOT NULL,
    issuer_name      TEXT,
    source           TEXT NOT NULL,    -- NSE | BSE | SEBI | MANUAL
    document_type    TEXT DEFAULT 'IM',-- IM | IM-REVISED | SHELFDOC | PROSPECTUS
    document_url     TEXT,             -- Link to PDF / filing page
    -- Core IM terms
    issue_size_cr    REAL,             -- Issuance size (₹ crore)
    coupon_rate      REAL,             -- % p.a.
    maturity_date    TEXT,
    allotment_date   TEXT,
    listing_date     TEXT,
    credit_rating    TEXT,
    rating_agency    TEXT,
    trustee          TEXT,
    secured          INTEGER DEFAULT 0,-- 1 = secured debenture
    -- Filing metadata
    filing_date      TEXT,             -- YYYY-MM-DD when filed with exchange/SEBI
    symbol           TEXT,             -- Exchange symbol if known
    series           TEXT,             -- Tranche / series
    -- Internal flags
    terms_verified   INTEGER DEFAULT 0,-- 1 = checked against bond master
    compliance_notes TEXT,
    registered_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(isin, source, document_type)
);

CREATE INDEX IF NOT EXISTS idx_bimr_isin    ON bond_im_registry(isin);
CREATE INDEX IF NOT EXISTS idx_bimr_source  ON bond_im_registry(source);
CREATE INDEX IF NOT EXISTS idx_bimr_date    ON bond_im_registry(filing_date DESC);

-- ──────────────────────────────────────────────────────────────
-- IM Fetch Log — audit trail for every IM pull operation
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS im_fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_date      TEXT NOT NULL,
    source          TEXT NOT NULL,   -- NSE | BSE | SEBI
    isins_found     INTEGER DEFAULT 0,
    new_records     INTEGER DEFAULT 0,
    updated_records INTEGER DEFAULT 0,
    errors          TEXT,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ifl_date   ON im_fetch_log(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_ifl_source ON im_fetch_log(source);

-- ──────────────────────────────────────────────────────────────
-- System Config — runtime-mutable key-value settings
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS system_config (
    config_key   TEXT PRIMARY KEY,
    config_value TEXT NOT NULL,
    updated_by   TEXT NOT NULL DEFAULT 'system',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ──────────────────────────────────────────────────────────────
-- Bond IM Data — structured IM intelligence extracted / entered
-- One row per ISIN. Updated as new IM data becomes available.
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bond_im_data (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    isin                    TEXT NOT NULL UNIQUE,
    -- Core IM structure
    object_of_issue         TEXT,
    security_structure      TEXT,
    charge_type             TEXT DEFAULT 'FIRST', -- FIRST | SECOND | PARI_PASSU | UNSECURED
    redemption_structure    TEXT DEFAULT 'BULLET',-- BULLET | AMORTIZING | SINKING | STRUCTURED
    -- Covenants (JSON arrays)
    covenants_financial     TEXT,                 -- JSON list e.g. ['DSCR > 1.2', 'D/E < 3']
    covenants_non_financial TEXT,
    -- Key ratio requirements
    dscr_requirement        REAL,
    interest_coverage_req   REAL,
    debt_ebitda_limit       REAL,
    -- Optionality
    call_option_details     TEXT,
    put_option_details      TEXT,
    step_up_clauses         TEXT,
    -- Legal clauses
    default_clauses         TEXT,
    cross_default_triggers  TEXT,
    -- Trustee & payment
    trustee_name            TEXT,
    trustee_contact         TEXT,
    escrow_mechanism        TEXT,
    waterfall_structure     TEXT,
    payment_mechanism       TEXT,
    -- Guarantee
    guarantee_details       TEXT,
    -- Full extracted text from IM PDF (for text-based heuristics)
    im_full_text            TEXT,
    -- Risk output
    risk_flags              TEXT,                -- JSON: auto-detected flags
    heuristic_results       TEXT,                -- JSON: full last-run heuristic output
    hidden_risk_score       REAL DEFAULT 0,
    -- Metadata
    im_parsed_at            TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bimd_isin  ON bond_im_data(isin);

-- ──────────────────────────────────────────────────────────────
-- Issuer Financials — annual P&L and balance sheet data
-- Used by heuristics H8 (covenant headroom) and health scoring.
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS issuer_financials (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    isin                    TEXT,                -- optional link to specific bond
    issuer_name             TEXT NOT NULL,
    financial_year          TEXT NOT NULL,       -- e.g. 'FY2025', '2024-25'
    -- P&L (₹ crore)
    revenue_cr              REAL,
    ebitda_cr               REAL,
    net_profit_cr           REAL,
    interest_expense_cr     REAL,
    -- Balance sheet (₹ crore)
    total_debt_cr           REAL,
    cash_cr                 REAL,
    total_assets_cr         REAL,
    net_worth_cr            REAL,
    current_assets_cr       REAL,
    current_liabilities_cr  REAL,
    -- Cash flow
    cfo_cr                  REAL,
    capex_cr                REAL,
    -- Auto-computed ratios (stored for heuristic use)
    debt_ebitda             REAL,               -- total_debt / ebitda
    interest_coverage       REAL,               -- ebitda / interest_expense
    dscr                    REAL,               -- cfo / interest_expense
    current_ratio           REAL,               -- current_assets / current_liabilities
    roe                     REAL,               -- net_profit / net_worth * 100
    -- Source
    data_source             TEXT DEFAULT 'MANUAL',
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(issuer_name, financial_year)
);

CREATE INDEX IF NOT EXISTS idx_ifin_issuer ON issuer_financials(issuer_name);
CREATE INDEX IF NOT EXISTS idx_ifin_isin   ON issuer_financials(isin);

-- ──────────────────────────────────────────────────────────────
-- IM Documents — raw scraped/uploaded IM documents from SEBI/BSE/NSE
-- One row per document. Stores metadata + full extracted text.
-- Processing pipeline: PENDING → PROCESSING → DONE | FAILED
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS im_documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    isin                TEXT,                       -- extracted from PDF text (nullable until parsed)
    issuer_name         TEXT,                       -- from listing or extracted
    doc_title           TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'SEBI', -- SEBI | BSE | NSE | MANUAL
    doc_type            TEXT DEFAULT 'PROSPECTUS',  -- PROSPECTUS | SHELF_PROSPECTUS | TRANCHE | IM | OTHER
    listing_url         TEXT,                       -- page on SEBI/BSE/NSE listing
    doc_url             TEXT UNIQUE,                -- HTML detail page URL
    pdf_url             TEXT,                       -- direct link to PDF
    filing_date         TEXT,                       -- YYYY-MM-DD (from listing)
    full_text           TEXT,                       -- extracted PDF text (may be large)
    text_length         INTEGER DEFAULT 0,
    extraction_status   TEXT NOT NULL DEFAULT 'PENDING', -- PENDING|PROCESSING|DONE|FAILED|NO_PDF
    error_message       TEXT,
    heuristic_score     REAL,                       -- hidden risk score after analysis
    heuristic_run_at    TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_imdoc_isin    ON im_documents(isin);
CREATE INDEX IF NOT EXISTS idx_imdoc_source  ON im_documents(source);
CREATE INDEX IF NOT EXISTS idx_imdoc_status  ON im_documents(extraction_status);
CREATE INDEX IF NOT EXISTS idx_imdoc_date    ON im_documents(filing_date DESC);

-- ──────────────────────────────────────────────────────────────
-- Bond IM Documents — versioned uploaded/ingested IM PDFs
-- Stores metadata per uploaded file with version tracking.
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bond_im_uploads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    isin                TEXT NOT NULL,
    issuer_name         TEXT,
    file_path           TEXT NOT NULL,           -- relative path in im_storage/
    file_name           TEXT NOT NULL,            -- original filename
    file_size           INTEGER DEFAULT 0,
    checksum            TEXT NOT NULL,            -- SHA-256 of file
    version             INTEGER NOT NULL DEFAULT 1,
    is_active           INTEGER NOT NULL DEFAULT 1, -- 1 = current version, 0 = superseded
    upload_source       TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL | SEBI | BSE | NSE_FUTURE | ISSUER
    uploaded_by         TEXT DEFAULT 'admin',
    -- Auto-detected / user-confirmed metadata
    coupon              REAL,
    maturity_date       TEXT,
    issue_size_cr       REAL,
    face_value          REAL,
    credit_rating       TEXT,
    rating_agency       TEXT,
    call_option         INTEGER DEFAULT 0,       -- 1 = yes
    put_option          INTEGER DEFAULT 0,
    security_type       TEXT,                     -- SECURED | UNSECURED
    seniority           TEXT,                     -- SENIOR | SUBORDINATED | PARI_PASSU
    -- Extraction state
    extraction_status   TEXT NOT NULL DEFAULT 'PENDING', -- PENDING | PROCESSING | DONE | FAILED
    extracted_text      TEXT,
    text_length         INTEGER DEFAULT 0,
    -- Risk engine output
    risk_score          REAL,
    risk_level          TEXT,                     -- LOW | MEDIUM | HIGH | CRITICAL
    risk_flags          TEXT,                     -- JSON array of risk flags
    risk_run_at         TEXT,
    -- Audit
    disclaimer          TEXT DEFAULT 'Document uploaded by user. Platform performs analytical processing only.',
    legal_source        TEXT DEFAULT 'Manual Upload by User',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_biu_isin     ON bond_im_uploads(isin);
CREATE INDEX IF NOT EXISTS idx_biu_active   ON bond_im_uploads(is_active);
CREATE INDEX IF NOT EXISTS idx_biu_checksum ON bond_im_uploads(checksum);

-- ──────────────────────────────────────────────────────────────
-- IM Extracted Data — structured fields parsed from IM PDFs
-- One row per upload. Stores covenants, risk factors, clauses.
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS im_extracted_data (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id               INTEGER NOT NULL,     -- FK to bond_im_uploads
    isin                    TEXT NOT NULL,
    -- Core terms
    coupon                  REAL,
    maturity_date           TEXT,
    call_option             INTEGER DEFAULT 0,
    put_option              INTEGER DEFAULT 0,
    seniority               TEXT,
    security_type           TEXT,
    -- Covenants & clauses
    covenants               TEXT,                  -- JSON array
    risk_factors            TEXT,                  -- JSON array
    cross_default_clause    INTEGER DEFAULT 0,     -- 1 = present
    change_of_control       INTEGER DEFAULT 0,
    acceleration_clause     INTEGER DEFAULT 0,
    negative_pledge         INTEGER DEFAULT 0,
    step_up_triggers        TEXT,                  -- JSON
    event_of_default        TEXT,                  -- extracted text
    debt_covenants          TEXT,                  -- JSON
    promoter_guarantee      TEXT,
    liquidity_risk          TEXT,
    security_charge_ranking TEXT,
    -- Extracted text
    extracted_text          TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ied_upload ON im_extracted_data(upload_id);
CREATE INDEX IF NOT EXISTS idx_ied_isin   ON im_extracted_data(isin);

-- ──────────────────────────────────────────────────────────────
-- IM Upload Audit Log — every action on uploads tracked
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS im_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER,
    isin            TEXT,
    action          TEXT NOT NULL,           -- UPLOAD | REUPLOAD | EXTRACT | RISK_RUN | VERSION_CHANGE | DELETE | INTEGRITY_CHECK
    actor           TEXT DEFAULT 'system',
    details         TEXT,                    -- JSON
    ip_address      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ial_upload ON im_audit_log(upload_id);
CREATE INDEX IF NOT EXISTS idx_ial_isin   ON im_audit_log(isin);
CREATE INDEX IF NOT EXISTS idx_ial_date   ON im_audit_log(created_at DESC);

-- ══════════════════════════════════════════════════════════════════════════
-- IM Processing Log — tracks every extraction attempt with method & timing
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS im_processing_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL,
    method_used     TEXT NOT NULL DEFAULT 'TEXT',  -- TEXT | OCR | HYBRID
    char_count      INTEGER DEFAULT 0,
    pages_processed INTEGER DEFAULT 0,
    processing_time REAL DEFAULT 0,               -- seconds
    status          TEXT NOT NULL DEFAULT 'SUCCESS', -- SUCCESS | FAILED | PARTIAL
    error_message   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ipl_upload ON im_processing_log(upload_id);

-- ══════════════════════════════════════════════════════════════════════════
-- IM Full Text — stores entire extracted document text
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS im_full_text (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL UNIQUE,
    full_text       TEXT,
    text_length     INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ift_upload ON im_full_text(upload_id);

-- ══════════════════════════════════════════════════════════════════════════
-- IM Pages — per-page extracted text for page-level referencing
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS im_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL,         -- FK to bond_im_uploads
    page_number     INTEGER NOT NULL,
    raw_text        TEXT,
    clean_text      TEXT,
    token_count     INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_imp_upload ON im_pages(upload_id);

-- ══════════════════════════════════════════════════════════════════════════
-- IM Sections — detected logical sections in the IM document
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS im_sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL,
    section_name    TEXT NOT NULL,             -- e.g. "Risk Factors", "Events of Default", "Covenants"
    start_page      INTEGER,
    end_page        INTEGER,
    section_text    TEXT,
    confidence      REAL DEFAULT 1.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ims_upload  ON im_sections(upload_id);
CREATE INDEX IF NOT EXISTS idx_ims_section ON im_sections(section_name);

-- ══════════════════════════════════════════════════════════════════════════
-- Bond Metadata — structured bond terms extracted from IM
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bond_metadata (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id           INTEGER NOT NULL,
    isin                TEXT,
    issuer_name         TEXT,
    currency            TEXT DEFAULT 'INR',
    issue_size          REAL,                  -- in crores
    coupon_rate         REAL,
    coupon_type         TEXT,                  -- FIXED | FLOATING | ZERO
    maturity_date       TEXT,
    issue_date          TEXT,
    trustee             TEXT,
    debenture_trustee   TEXT,
    security_trustee    TEXT,
    registrar           TEXT,
    listing_exchange    TEXT,
    governing_law       TEXT,
    jurisdiction        TEXT,
    security_type       TEXT,                  -- SECURED | UNSECURED
    seniority           TEXT,                  -- SENIOR | SUBORDINATED | PARI_PASSU
    guarantee           TEXT,
    face_value          REAL,
    credit_rating       TEXT,
    rating_agency       TEXT,
    tenor               TEXT,
    call_option         TEXT,
    put_option          TEXT,
    step_up_coupon      TEXT,
    isin_validated      INTEGER DEFAULT 0,     -- 1 = cross-checked with master
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bmd_upload ON bond_metadata(upload_id);
CREATE INDEX IF NOT EXISTS idx_bmd_isin   ON bond_metadata(isin);

-- ══════════════════════════════════════════════════════════════════════════
-- Bond Clauses — individual clause detection results from agents
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bond_clauses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL,
    clause_type     TEXT NOT NULL,             -- ACCELERATION | CROSS_DEFAULT | NEGATIVE_PLEDGE | CHANGE_OF_CONTROL | SECURITY | GUARANTEE | EVENT_OF_DEFAULT
    status          TEXT NOT NULL DEFAULT 'ABSENT', -- PRESENT | ABSENT | PARTIAL
    page_number     INTEGER,
    section         TEXT,
    clause_text     TEXT,
    confidence      REAL DEFAULT 0.0,
    detected_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bcl_upload ON bond_clauses(upload_id);
CREATE INDEX IF NOT EXISTS idx_bcl_type   ON bond_clauses(clause_type);

-- ══════════════════════════════════════════════════════════════════════════
-- Bond Covenants — financial and operational covenants detected
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bond_covenants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL,
    covenant_type   TEXT NOT NULL,             -- FINANCIAL | OPERATIONAL
    covenant_name   TEXT NOT NULL,             -- e.g. "Debt/Equity", "No Additional Borrowing"
    threshold       TEXT,                      -- e.g. "<= 2.5x"
    page_number     INTEGER,
    section         TEXT,
    text            TEXT,
    confidence      REAL DEFAULT 0.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bcov_upload ON bond_covenants(upload_id);
CREATE INDEX IF NOT EXISTS idx_bcov_type   ON bond_covenants(covenant_type);

-- ══════════════════════════════════════════════════════════════════════════
-- Bond Risk Analysis — structured risk scoring per IM
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bond_risk_analysis (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id           INTEGER NOT NULL,
    isin                TEXT,
    risk_score          REAL DEFAULT 0,
    risk_level          TEXT DEFAULT 'UNKNOWN', -- LOW | MEDIUM | HIGH | CRITICAL
    -- Clause presence flags (1=present, 0=absent)
    cross_default       INTEGER DEFAULT 0,
    negative_pledge     INTEGER DEFAULT 0,
    acceleration        INTEGER DEFAULT 0,
    change_of_control   INTEGER DEFAULT 0,
    security            INTEGER DEFAULT 0,
    guarantee           INTEGER DEFAULT 0,
    covenants           INTEGER DEFAULT 0,      -- count of covenants found
    -- Risk breakdown (points added)
    unsecured_pts       REAL DEFAULT 0,
    no_cross_default_pts     REAL DEFAULT 0,
    no_negative_pledge_pts   REAL DEFAULT 0,
    no_guarantee_pts         REAL DEFAULT 0,
    no_covenants_pts         REAL DEFAULT 0,
    low_rating_pts           REAL DEFAULT 0,
    high_coupon_pts          REAL DEFAULT 0,
    bullet_repayment_pts     REAL DEFAULT 0,
    red_flags           TEXT,                   -- JSON array of red flag strings
    analysis_time       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bra_upload ON bond_risk_analysis(upload_id);
CREATE INDEX IF NOT EXISTS idx_bra_isin   ON bond_risk_analysis(isin);

-- ══════════════════════════════════════════════════════════════════════════
-- Clause Evidence — proof/source text for every clause detection
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS clause_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL,
    clause_type     TEXT NOT NULL,
    page_number     INTEGER,
    section         TEXT,
    text_snippet    TEXT NOT NULL,
    confidence      REAL DEFAULT 0.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cev_upload ON clause_evidence(upload_id);
CREATE INDEX IF NOT EXISTS idx_cev_type   ON clause_evidence(clause_type);

-- ══════════════════════════════════════════════════════════════════════════
-- Bond News — aggregated from RSS feeds + regulatory sources
-- ══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bond_news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hash_id         TEXT    UNIQUE NOT NULL,
    title           TEXT    NOT NULL,
    summary         TEXT,
    url             TEXT,
    source          TEXT    NOT NULL,
    source_name     TEXT,
    category        TEXT    DEFAULT 'GENERAL',
    relevance_score INTEGER DEFAULT 0,
    matched_issuers TEXT,
    published_at    TEXT,
    fetched_at      TEXT    DEFAULT (datetime('now')),
    is_read         INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bond_news_published  ON bond_news(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_bond_news_relevance  ON bond_news(relevance_score DESC, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_bond_news_source     ON bond_news(source);
