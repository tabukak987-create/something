# Task: Fix Bug

## Steps
1. **Identify Failing Module**: Use `grep_search` to find the exact file or trace the stack trace.
2. **Check `docs/FILE_INDEX.json`**: Identify which DOMAIN the failing module belongs to.
3. **Open ONLY Relevant Domain Files**: Read `backend/app/domain/<domain>/README.md` to understand domain constraints. Do not scan unrelated domains.
4. **Apply Minimal Fix**: Ensure the fix targets the exact issue without rewriting large portions of code.
5. **Verify Fix**: Ensure you run relevant tests or command tools to verify the fix works before reporting completion.

## Never
- **NEVER** scan the entire repo or open files in `IGNORE`.
- **NEVER** modify unrelated modules or domains.
- **NEVER** introduce cross-domain dependencies to fix a bug.
