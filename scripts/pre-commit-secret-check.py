#!/usr/bin/env python3
"""
Git Pre-Commit Hook — Secret Detection
========================================
Prevents accidental commit of:
  - Plaintext passwords, API keys, JWT secrets
  - .env.secrets files
  - Database connection strings with embedded credentials

Install:
  cp scripts/pre-commit-secret-check.py .git/hooks/pre-commit
  chmod +x .git/hooks/pre-commit

Or via pre-commit framework:
  Add to .pre-commit-config.yaml
"""

import re
import subprocess
import sys

# Patterns that indicate hardcoded secrets
SECRET_PATTERNS = [
    (r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']?[A-Za-z0-9@#$%^&*!_]{4,}', "Hardcoded password detected"),
    (r'(?i)jwt_secret_key\s*=\s*[a-f0-9]{32,}', "JWT secret key in plaintext"),
    (r'(?i)(api[_-]?key|secret[_-]?key)\s*[=:]\s*["\']?[A-Za-z0-9]{16,}', "API/secret key detected"),
    (r'postgresql\+asyncpg://\w+:[^@\s]+@', "Database URL with embedded password"),
    (r'(?i)PGPASSWORD\s*=\s*["\']?[A-Za-z0-9@#$%^&*!_]{4,}', "PostgreSQL password in plaintext"),
    (r'admin123', "Default/weak password 'admin123'"),
    (r'(?i)admin@2026', "Default admin password detected"),
    (r'(?i)gw_secure_2026', "Default gateway DB password detected"),
]

# Files to always block
BLOCKED_FILES = [
    r'\.env\.secrets$',
    r'\.env\.local$',
    r'client_secret.*\.json$',
]

# Files to skip (templates, examples, this script itself)
SKIP_PATTERNS = [
    r'\.env\.example$',
    r'\.env\.secrets\.template$',
    r'pre-commit-secret-check\.py$',
    r'\.md$',  # Documentation may reference patterns
]


def get_staged_files():
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True
    )
    return result.stdout.strip().split("\n") if result.stdout.strip() else []


def check_file(filepath):
    violations = []

    # Check if file itself is blocked
    for pattern in BLOCKED_FILES:
        if re.search(pattern, filepath):
            violations.append(f"  BLOCKED FILE: {filepath} must not be committed")
            return violations

    # Skip documentation and template files
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, filepath):
            return []

    try:
        result = subprocess.run(
            ["git", "show", f":{filepath}"],
            capture_output=True, text=True
        )
        content = result.stdout
    except Exception:
        return []

    for pattern, message in SECRET_PATTERNS:
        matches = re.finditer(pattern, content)
        for match in matches:
            # Find line number
            line_num = content[:match.start()].count('\n') + 1
            violations.append(f"  {filepath}:{line_num} — {message}")

    return violations


def main():
    files = get_staged_files()
    if not files or files == ['']:
        sys.exit(0)

    all_violations = []
    for f in files:
        violations = check_file(f)
        all_violations.extend(violations)

    if all_violations:
        print("\n" + "=" * 70)
        print("🔒 SECRET DETECTION — COMMIT BLOCKED")
        print("=" * 70)
        print("\nThe following secrets/credentials were detected in staged files:\n")
        for v in all_violations:
            print(v)
        print(f"\n{'=' * 70}")
        print("Remove secrets and use environment variables or a secrets manager.")
        print("See .env.secrets.template for guidance.")
        print(f"{'=' * 70}\n")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
