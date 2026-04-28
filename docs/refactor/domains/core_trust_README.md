# Core Trust Domain

## Purpose
The `core_trust` domain is the orchestrator and final source of truth for operations. It guarantees that any action taken by the system has been fully audited, complies with four-eyes principles, and maintains immutable traceability.

## MUST NOT Do
- MUST NOT contain domain-specific logic like bond pricing or risk calculations. It only routes and records them.

## Entry Files
- `orchestrator.py`: Intercepts and routes high-risk actions.
- `audit.py`: Maintains the immutable audit ledger.
- `approvals.py`: Manages maker-checker workflows.

## Dependencies
- Has no functional dependencies on other domains, but other domains will depend heavily on `core_trust` for logging and decision validation.
