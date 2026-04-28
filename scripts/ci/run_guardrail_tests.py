from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "unified_gateway/tests/test_investment_analysis_guardrails.py",
        "unified_gateway/tests/test_intelligence_ops_endpoints.py",
        "unified_gateway/tests/test_investment_analysis_edge_scenarios.py",
        "unified_gateway/tests/test_runtime_monitor_and_gatekeeper_rules.py",
        "-q",
        "-ra",
    ]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    sys.stdout.write(output)

    if proc.returncode != 0:
        print("DEPLOYMENT BLOCKED")
        print("Reason: Guardrail test suite failed.")
        return proc.returncode

    skipped = re.search(r"\b(\d+)\s+skipped\b", output, flags=re.IGNORECASE)
    if skipped and int(skipped.group(1)) > 0:
        print("DEPLOYMENT BLOCKED")
        print("Reason: Guardrail test suite contains skipped tests.")
        return 1

    passed = re.search(r"\b(\d+)\s+passed\b", output, flags=re.IGNORECASE)
    if not passed:
        print("DEPLOYMENT BLOCKED")
        print("Reason: Could not verify passing guardrail tests.")
        return 1

    print("Guardrail test stage PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
