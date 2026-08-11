# POI target architecture and delivery phases

## Architectural decision

POI extends the existing Spring Boot, React TypeScript, and MySQL application. It is initially single-institution. Financial calculations run on the server with `BigDecimal`; the browser only formats returned values. Uploaded originals and result snapshots are persisted, and extracted output is explicitly treated as draft evidence requiring human review.

## Current vertical slice

The implemented workflow is:

```text
Authenticated analyst
  -> upload CSV/XLSX/JSON/PDF/TXT sources
  -> validate size and type
  -> store original bytes and SHA-256 checksum
  -> extract structured rows or document ISIN evidence
  -> merge records by ISIN while retaining source names
  -> calculate market value, weighted coupon, quality, rating and issuer summaries
  -> flag missing, conflicting, concentrated, or low-rated data
  -> save immutable result JSON and append an audit event
  -> reopen the result from analysis history
```

Only `ANALYST` and `ADMIN` roles may submit analysis batches. A user may retrieve only results they created. All protected operations are checked on the server.

## Target bounded domains

1. Identity and institution: users, roles, permissions, sessions, institutions, account security.
2. Instruments and issuers: normalized bond terms, issuer groups, ratings, security, covenants, classifications, and source history.
3. Portfolios: portfolios, holdings, transactions, targets, valuation snapshots, income, allocation, and drill-down.
4. Market intelligence: observations, prices, broker quotes, exchange trades, benchmarks, news, freshness, conflicts, and official-value selection.
5. Documents and evidence: document versions, pages, OCR, facts, clauses, confidence, verification, and evidence references.
6. Analysis and recommendations: versioned cases, factors, alternatives, snapshots, conclusions, and reviewer workflow.
7. Compliance: versioned IPS policies, rules, evaluations, breaches, exceptions, and maker-checker activation.
8. Risk: duration, DV01, convexity, spread and yield shocks, scenarios, concentration, liquidity, and limitations.
9. Governance: approvals, decisions, overrides, append-only audit events, and historical reconstruction.
10. Reporting: reproducible snapshots and PDF/spreadsheet exports.

## Delivery order

### Phase 1 — implemented

Upload, source preservation, consolidation, initial calculations/findings, analysis history, bond-register synchronization, and audit event.

### Phase 2

Normalize institutions, users, granular roles/permissions, HTTP-only server sessions, password reset, throttling, optional MFA, session revocation, and full authentication audit.

### Phase 3

Expand instruments and build portfolios, holdings, transactions, price observations, valuation dates, allocation targets, income schedules, and decimal-safe portfolio calculations with independent test vectors.

### Phase 4

Add page-level PDF extraction, OCR adapter, document classification, facts/clauses, confidence, verification state, comments, and evidence navigation.

### Phase 5

Add recommendation versions, active IPS policy/rule engine, pre-trade simulation, current-state evaluation, breaches, risk methods, and clearly documented missing-data behavior.

### Phase 6

Add configurable approval workflows, maker-checker enforcement, overrides, decision reconstruction, reproducible reports, exports, OpenAPI, full security tests, and end-to-end coverage.

## Security assumptions and current limitations

- The current environment is local development and single-institution.
- JWT access is time-limited, but the React client currently stores its token locally. HTTP-only server-side sessions and revocation are Phase 2 requirements before production.
- Uploads enforce authentication, role checks, extension, count, and size limits. A malware-scanner adapter must be connected before accepting untrusted production uploads.
- PDF extraction reads embedded text. OCR, page-level evidence coordinates, confidence scoring, and human verification controls are not yet implemented.
- The current concentration finding uses a review threshold, not an active institutional IPS rule. It is labelled accordingly.
- The current result is decision support and cannot approve investments or execute trades.
- Database schema evolution currently uses Hibernate update for development. Versioned Flyway migrations are required before production.
