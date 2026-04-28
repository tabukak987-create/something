# Coding Rules

## Naming Conventions
- **Python Code**: Use `snake_case` for variables, functions, and module names.
- **Classes**: Use `PascalCase` for class names, particularly in models and services.
- **Service Layer**: Name service classes with clear domain suffixes (e.g., `AlertEngine`, `DataIntegrityService`).

## Architecture Rules
- **Monolithic Gateway**: All primary API routing flows through the Unified Gateway (`backend/main.py`).
- **Traceability**: All critical investment and compliance decisions MUST route through the `DecisionOrchestrator` and `TrustDecisionPipeline`.
- **Fail-Closed Contract**: Responses on trace-enforced paths (`/api/v1/compliance/*`, `/api/v1/risk/*`, etc.) must return strict JSON schemas including `decision_id`, `input_hash`, etc.
- **Data Access**: Perform database interactions via the `app.shared.database` module using parameterized async queries (e.g., `fetch_one`, `execute_query`).

## AI Optimization & Token Management
- **Token Efficiency**: Before loading any file listed in the `HEAVY` section of `FILE_INDEX.json`, check if a corresponding summary exists in `.aicontext/SUMMARIES/`. Load the summary first to determine if the full file content is actually required.
- **Search Scope**: Avoid recursive directory listings (e.g., `ls -R`). Use `FILE_INDEX.json` and `NAVIGATION.md` to locate files.
- **Context Boundaries**: AI Agents should restrict their context retrieval to the specific domain directory relevant to the current task. Do not scan unrelated domains unless cross-domain integration is the primary objective.
- **Skeleton Files**: For massive third-party or internal dependencies, prefer reading `.aicontext/SUMMARIES/*.md` or looking for interface definitions (`__init__.py`) before reading implementation details.
- **Graphify**: Use the `graphify` MCP tool or review `graphify-out/GRAPH_REPORT.md` to perform structural queries before doing massive code dumps.

## Code Style
- **Asynchronous First**: Use `async def` for any I/O bound operations, route handlers, and database queries.
- **Type Hinting**: Always use type hints in Python function signatures to enforce clear contracts.
- **Error Handling**: Use `log.warning` and `log.error` via the structured logger (`app.shared.logger`) before raising HTTPExceptions or returning structured error responses.

## Do NOT Do
- Do not make synchronous blocking network or database calls in API route handlers.
- Do not expose administrative operations without checking user roles via `require_role`.
- Do not embed secrets directly in the codebase; always use environment variables (`.env`).
- Do not query external internet data if `ALLOW_EXTERNAL_DATA_EGRESS` is disabled.
