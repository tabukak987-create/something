# Task: Add Feature

## Steps
1. **Identify Target Domain**: Check `docs/FILE_INDEX.json` and decide which domain the feature belongs to.
2. **Read Domain Rules**: Read `backend/app/domain/<domain>/README.md`.
3. **Review CRITICAL Files**: If the feature requires new endpoints, authentication, or DB schema changes, review the `CRITICAL` files in the index first.
4. **Implement Minimally**: Add the feature directly in the identified domain. If the feature spans multiple domains, build explicit interfaces via the API or `core_trust` orchestrator.
5. **Update Docs**: If a new file is created, update `FILE_SUMMARY.md` in that folder and add it to `FILE_INDEX.json`.

## Never
- **NEVER** bypass the `core_trust` decision pipeline for investment/compliance features.
- **NEVER** load the full repo to find where to put a new feature.
