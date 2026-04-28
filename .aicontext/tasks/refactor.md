# Task: Refactor

## Steps
1. **Analyze Current Structure**: Target specific files or domains. Do not attempt a global refactor.
2. **Review `docs/refactor/migration_plan.md`**: Ensure the refactor aligns with the overall domain-driven transition plan.
3. **Split HEAVY Files**: Check `FILE_INDEX.json` for HEAVY files in the domain. Break them into `helpers.py`, `validators.py`, etc., within the same domain.
4. **Enforce Boundaries**: Ensure no cross-domain imports are introduced.

## Never
- **NEVER** change the functionality of the code when refactoring.
- **NEVER** mix domains (e.g., placing market_data logic inside the risk folder).
