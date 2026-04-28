# System Map

## Core Structure
- **backend/**: Contains the main application logic, strictly partitioned by domain.
- **frontend/**: The user interface layer.
- **unified_gateway/**: The API gateway routing requests.
- **infrastructure/**: Infrastructure as code, deployment scripts.
- **jobs/**: Background tasks and jobs.
- **scripts/**: Utility and administration scripts.
- **docs/**: High-level system documentation.
- **tests/**: Unit and integration test suites.

## Domain Architecture (`backend/app/domain/`)
The backend is organized around bounded contexts to ensure separation of concerns:
- **compliance**: Rules, validation, and regulatory compliance.
- **core_trust**: Governance, security, data integrity.
- **market_data**: Ingestion, scoring, and analysis of market data.
- **portfolio**: Recommendations, allocations, institutional logic.
- **risk**: Alerts, heuristics, and scoring engines.
