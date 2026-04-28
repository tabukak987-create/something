# AI Usage Rules

You must ALWAYS follow this workflow when navigating the codebase:

1. **Always read ENTRY files first**: To understand where the execution begins (`backend/main.py`, `frontend/index.html`).
2. **Then read CRITICAL files**: If the task involves database, auth, or major orchestration logic, read the critical files to understand system-wide rules and constraints.
3. **Then DOMAIN-specific files**: Use `FILE_INDEX.json` to identify the right domain (e.g., `compliance`, `bond`). Read only those files. Do not scan unrelated domains.
4. **Avoid HEAVY unless needed**: Large files cost tokens. Only open them if the specific bug or feature lives entirely within them.
5. **Never scan IGNORE paths**: Ignore compiled files, cache directories, and Git folders to preserve context windows.
