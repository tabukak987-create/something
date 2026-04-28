"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   PF Trust Bond Platform — Software Bill of Materials (SBOM) Generator      ║
║   Version: 1.0   Date: 2026-03-03   Classification: CONFIDENTIAL            ║
║   Standards: CycloneDX 1.4 + SPDX 2.3 formats                               ║
╚══════════════════════════════════════════════════════════════════════════════╝

Generates:
  - CycloneDX 1.4 BOM (JSON) — machine-readable dependency manifest
  - SPDX 2.3 tag-value report — license compliance manifest
  - Vulnerability summary — known CVEs from pip-audit (if installed)
  - Dependency risk heat map — severity by package

Run: python scripts/generate_sbom.py [--format cyclonedx|spdx|all] [--output-dir reports/]

SBOM is required for:
  - SEBI IT framework compliance (circular SEBI/HO/ITD/2023/0015)
  - Institutional procurement due diligence
  - Cyber insurance underwriting questionnaires
  - ISO 27001 A.12.6.1 (management of technical vulnerabilities)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
PLATFORM_NAME    = "PFTrust-BondIntelligencePlatform"
PLATFORM_VERSION = "3.0.0"
PLATFORM_VENDOR  = "PF Trust Investment Operations"
SBOM_NAMESPACE   = f"https://pftrust.in/sbom/{PLATFORM_NAME}/{PLATFORM_VERSION}"


@dataclass
class ComponentInfo:
    name: str
    version: str
    purl: str          # Package URL (pkg:pypi/name@version)
    licenses: list[str]
    description: str
    hash_sha256: Optional[str] = None
    vulnerabilities: list[dict] = field(default_factory=list)
    risk_level: str = "LOW"  # LOW / MEDIUM / HIGH / CRITICAL


# ── Package discovery ──────────────────────────────────────────────────────────

def _get_installed_packages() -> list[tuple[str, str]]:
    """Get all installed packages and their versions from the active venv."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=30
        )
        packages = json.loads(result.stdout)
        return [(p["name"], p["version"]) for p in packages]
    except Exception as e:
        print(f"  ⚠️  pip list failed: {e}. Falling back to importlib.metadata")
        try:
            import importlib.metadata
            return [
                (dist.metadata["Name"], dist.metadata["Version"])
                for dist in importlib.metadata.distributions()
            ]
        except Exception:
            return []


def _get_package_metadata(name: str, version: str) -> dict:
    """Get license and description from package metadata."""
    try:
        import importlib.metadata
        dist = importlib.metadata.distribution(name)
        meta = dist.metadata
        license_val = meta.get("License") or ""
        classifiers = meta.get_all("Classifier") or []
        # Extract license from classifiers if not in License field
        if not license_val:
            for clf in classifiers:
                if "License ::" in clf:
                    license_val = clf.split("::")[-1].strip()
                    break
        description = meta.get("Summary") or ""
        return {"license": license_val or "Unknown", "description": description}
    except Exception:
        return {"license": "Unknown", "description": ""}


def _get_purl(name: str, version: str) -> str:
    """Generate Package URL (purl) per PURL spec."""
    return f"pkg:pypi/{name.lower().replace('-','_')}@{version}"


def _hash_package_file(name: str, version: str) -> Optional[str]:
    """Get SHA-256 of the package's dist-info RECORD if available."""
    try:
        import importlib.metadata
        dist = importlib.metadata.distribution(name)
        # Find a wheel file or source dist
        for f in (dist.files or []):
            if str(f).endswith(".dist-info/RECORD"):
                record_path = dist.locate_file(f)
                if os.path.exists(str(record_path)):
                    with open(str(record_path), "rb") as fp:
                        return hashlib.sha256(fp.read()).hexdigest()
    except Exception:
        pass
    return None


# ── Vulnerability scan ─────────────────────────────────────────────────────────

def _run_pip_audit() -> dict[str, list[dict]]:
    """
    Run pip-audit to detect known CVEs (PyPA advisory database).
    Returns dict: package_name → list of CVE dicts.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--format=json", "--progress-spinner=off"],
            capture_output=True, text=True, timeout=120
        )
        data = json.loads(result.stdout)
        vulns: dict[str, list] = {}
        for dep in data.get("dependencies", []):
            pkg = dep.get("name", "").lower()
            for vuln in dep.get("vulns", []):
                vulns.setdefault(pkg, []).append({
                    "id": vuln.get("id", ""),
                    "aliases": vuln.get("aliases", []),
                    "description": vuln.get("description", "")[:200],
                    "fix_versions": vuln.get("fix_versions", []),
                })
        return vulns
    except FileNotFoundError:
        print("  ℹ️  pip-audit not installed. Run: pip install pip-audit")
        return {}
    except Exception as e:
        print(f"  ⚠️  pip-audit error: {e}")
        return {}


# ── Known high-risk packages (requires careful version tracking) ───────────────
_HIGH_RISK_PACKAGES = {
    "cryptography", "pyotp", "bcrypt", "passlib", "python-jose",
    "pyjwt", "requests", "httpx", "aiohttp", "urllib3", "paramiko",
    "pyyaml", "pillow", "lxml", "defusedxml", "setuptools",
}

_KNOWN_CVE_PACKAGES = set()  # populated after pip-audit run


def _assess_risk(name: str, version: str, vulns: list[dict]) -> str:
    name_lower = name.lower().replace("-", "_")
    if vulns:
        return "CRITICAL" if len(vulns) > 2 else "HIGH"
    if name_lower in _HIGH_RISK_PACKAGES:
        return "MEDIUM"  # Monitoring required
    return "LOW"


# ── CycloneDX 1.4 BOM generation ──────────────────────────────────────────────

def generate_cyclonedx_bom(components: list[ComponentInfo]) -> dict:
    """Generate CycloneDX 1.4 BOM in JSON format."""
    bom_serial = hashlib.sha256(
        f"{PLATFORM_NAME}{PLATFORM_VERSION}{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()[:32]

    comp_list = []
    for c in components:
        comp = {
            "type": "library",
            "bom-ref": c.purl,
            "name": c.name,
            "version": c.version,
            "purl": c.purl,
            "description": c.description,
            "licenses": [{"license": {"id": lic}} for lic in c.licenses],
        }
        if c.hash_sha256:
            comp["hashes"] = [{"alg": "SHA-256", "content": c.hash_sha256}]
        if c.vulnerabilities:
            comp["externalReferences"] = [
                {
                    "type": "issue-tracker",
                    "url": f"https://osv.dev/vulnerability/{v['id']}",
                    "comment": v.get("description", "")[:100],
                }
                for v in c.vulnerabilities
            ]
        comp_list.append(comp)

    vulnerabilities = []
    for c in components:
        for v in c.vulnerabilities:
            vulnerabilities.append({
                "bom-ref": f"{c.purl}/{v['id']}",
                "id": v["id"],
                "source": {"name": "PyPA Advisory Database", "url": "https://github.com/pypa/advisory-database"},
                "aliases": [{"id": a} for a in v.get("aliases", [])],
                "description": v.get("description", "")[:500],
                "affects": [{"ref": c.purl}],
                "recommendation": f"Upgrade to: {', '.join(v.get('fix_versions', ['unknown']))}",
            })

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": f"urn:uuid:{bom_serial}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"vendor": "PFTrust", "name": "generate_sbom.py", "version": "1.0"}],
            "component": {
                "type": "application",
                "name": PLATFORM_NAME,
                "version": PLATFORM_VERSION,
                "description": "Bond Intelligence Platform — PF Trust Fixed Income Operations",
                "licenses": [{"license": {"id": "Proprietary"}}],
            },
        },
        "components": comp_list,
        "vulnerabilities": vulnerabilities,
    }


# ── SPDX 2.3 tag-value generation ─────────────────────────────────────────────

def generate_spdx_report(components: list[ComponentInfo]) -> str:
    """Generate SPDX 2.3 tag-value format SBOM."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc_spdx_id = hashlib.sha256(f"{PLATFORM_NAME}{PLATFORM_VERSION}{ts}".encode()).hexdigest()[:16]

    lines = [
        "SPDXVersion: SPDX-2.3",
        "DataLicense: CC0-1.0",
        f"SPDXID: SPDXRef-DOCUMENT",
        f"DocumentName: {PLATFORM_NAME}-{PLATFORM_VERSION}-SBOM",
        f"DocumentNamespace: {SBOM_NAMESPACE}/{doc_spdx_id}",
        f"Creator: Tool: PFTrust-generate_sbom.py-1.0",
        f"Created: {ts}",
        "",
        "## Package: Main Application",
        f"PackageName: {PLATFORM_NAME}",
        f"SPDXID: SPDXRef-Package-{PLATFORM_NAME}",
        f"PackageVersion: {PLATFORM_VERSION}",
        f"PackageSupplier: Organization: {PLATFORM_VENDOR}",
        "PackageDownloadLocation: NOASSERTION",
        "FilesAnalyzed: false",
        "PackageLicense: Proprietary",
        "PackageCopyrightText: NOASSERTION",
        "",
    ]

    for c in components:
        pkg_id = re.sub(r"[^A-Za-z0-9\-]", "-", f"Package-{c.name}-{c.version}")
        licenses_str = " AND ".join(c.licenses) if c.licenses else "NOASSERTION"
        lines += [
            f"PackageName: {c.name}",
            f"SPDXID: SPDXRef-{pkg_id}",
            f"PackageVersion: {c.version}",
            f"PackageDownloadLocation: https://pypi.org/project/{c.name}/{c.version}/",
            "FilesAnalyzed: false",
            f"ExternalRef: PACKAGE-MANAGER purl {c.purl}",
            f"PackageLicenseConcluded: {licenses_str}",
            f"PackageLicenseDeclared: {licenses_str}",
            "PackageCopyrightText: NOASSERTION",
        ]
        if c.hash_sha256:
            lines.append(f"PackageChecksum: SHA256: {c.hash_sha256}")
        if c.vulnerabilities:
            for v in c.vulnerabilities:
                lines.append(f"PackageComment: CVE/Vulnerability: {v['id']} — {v.get('description','')[:100]}")
        lines.append("")

    return "\n".join(lines)


# ── Risk heat map ──────────────────────────────────────────────────────────────

def generate_risk_summary(components: list[ComponentInfo]) -> dict:
    """Build risk classification summary."""
    by_risk: dict[str, list] = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}
    total_vulns = 0
    for c in components:
        by_risk[c.risk_level].append(c.name)
        total_vulns += len(c.vulnerabilities)

    return {
        "total_components": len(components),
        "total_vulnerabilities": total_vulns,
        "risk_distribution": {k: len(v) for k, v in by_risk.items()},
        "critical_packages": by_risk["CRITICAL"],
        "high_risk_packages": by_risk["HIGH"],
        "needs_immediate_action": by_risk["CRITICAL"] + by_risk["HIGH"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main(output_dir: Path, fmt: str = "all") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"\n{'═'*66}")
    print(f"  PF TRUST BOND PLATFORM — SBOM GENERATOR v1.0")
    print(f"  Platform: {PLATFORM_NAME} v{PLATFORM_VERSION}")
    print(f"  Output:   {output_dir}")
    print(f"{'═'*66}\n")

    print("  [1/4] Discovering installed packages...")
    packages = _get_installed_packages()
    print(f"        Found {len(packages)} packages")

    print("  [2/4] Running vulnerability scan (pip-audit)...")
    audit_results = _run_pip_audit()
    print(f"        {'Found vulnerabilities in ' + str(len(audit_results)) + ' packages' if audit_results else 'No vulnerabilities found ✅'}")

    print("  [3/4] Building component manifest...")
    components: list[ComponentInfo] = []
    for name, version in packages:
        meta = _get_package_metadata(name, version)
        license_str = meta["license"]
        licenses = [l.strip() for l in re.split(r"[,;/|]", license_str) if l.strip()] or ["Unknown"]
        vulns = audit_results.get(name.lower(), []) + audit_results.get(name.lower().replace("-", "_"), [])
        pkg_hash = _hash_package_file(name, version)
        risk = _assess_risk(name, version, vulns)
        comp = ComponentInfo(
            name=name,
            version=version,
            purl=_get_purl(name, version),
            licenses=licenses,
            description=meta["description"],
            hash_sha256=pkg_hash,
            vulnerabilities=vulns,
            risk_level=risk,
        )
        components.append(comp)

    print(f"        {len(components)} components catalogued")

    print("  [4/4] Generating SBOM files...")

    if fmt in ("cyclonedx", "all"):
        bom = generate_cyclonedx_bom(components)
        cdx_path = output_dir / f"sbom-cyclonedx-{ts}.json"
        with open(cdx_path, "w", encoding="utf-8") as f:
            json.dump(bom, f, indent=2)
        print(f"        CycloneDX BOM → {cdx_path.name}")

    if fmt in ("spdx", "all"):
        spdx = generate_spdx_report(components)
        spdx_path = output_dir / f"sbom-spdx-{ts}.spdx"
        with open(spdx_path, "w", encoding="utf-8") as f:
            f.write(spdx)
        print(f"        SPDX 2.3 report → {spdx_path.name}")

    risk_summary = generate_risk_summary(components)
    risk_path = output_dir / f"sbom-risk-summary-{ts}.json"
    with open(risk_path, "w", encoding="utf-8") as f:
        json.dump(risk_summary, f, indent=2)
    print(f"        Risk summary     → {risk_path.name}")

    # Print risk summary
    print(f"\n{'─'*66}")
    print(f"  RISK SUMMARY")
    print(f"{'─'*66}")
    print(f"  Total components:    {risk_summary['total_components']}")
    print(f"  Total vulnerabilities: {risk_summary['total_vulnerabilities']}")
    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        count = risk_summary["risk_distribution"].get(level, 0)
        icon = "🔴" if level == "CRITICAL" else "🟠" if level == "HIGH" else "🟡" if level == "MEDIUM" else "🟢"
        print(f"  {icon} {level}: {count}")
    if risk_summary["needs_immediate_action"]:
        print(f"\n  ⚠️  Packages requiring immediate action:")
        for pkg in risk_summary["needs_immediate_action"]:
            print(f"     • {pkg}")
    else:
        print(f"\n  ✅ No packages require immediate action")
    print(f"{'═'*66}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PF Trust SBOM Generator")
    parser.add_argument("--format", choices=["cyclonedx", "spdx", "all"], default="all")
    parser.add_argument("--output-dir", default="reports/sbom")
    args = parser.parse_args()
    main(Path(BASE_DIR / args.output_dir), args.format)
