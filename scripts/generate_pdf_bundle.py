"""
PDF Bundle Generator
====================
Converts all .md reports and governance documents to beautiful PDFs
and stores them in reports/pdf_bundle/

Uses: markdown2 → HTML → weasyprint → PDF

Usage:
    python scripts/generate_pdf_bundle.py
    python scripts/generate_pdf_bundle.py --dir governance
    python scripts/generate_pdf_bundle.py --all
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import io

import markdown2
from xhtml2pdf import pisa

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent          # Private3/
BUNDLE_DIR = ROOT / "reports" / "pdf_bundle"
BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Beautiful CSS stylesheet for institutional-grade PDFs
# ---------------------------------------------------------------------------
STYLESHEET = """
@page {
    size: A4;
    margin: 22mm 18mm 22mm 18mm;
}

body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.6;
    color: #1e293b;
}

h1 {
    font-size: 18pt;
    font-weight: bold;
    color: #0f172a;
    margin: 0 0 8pt 0;
    padding-bottom: 6pt;
    border-bottom: 2pt solid #0f172a;
}

h2 {
    font-size: 13pt;
    font-weight: bold;
    color: #1e3a5f;
    margin: 16pt 0 5pt 0;
    padding-bottom: 2pt;
    border-bottom: 0.75pt solid #cbd5e1;
}

h3 {
    font-size: 11pt;
    font-weight: bold;
    color: #334155;
    margin: 10pt 0 3pt 0;
}

h4 {
    font-size: 10pt;
    font-weight: bold;
    color: #475569;
    margin: 7pt 0 2pt 0;
}

p { margin: 0 0 5pt 0; }

table {
    width: 100%;
    border-collapse: collapse;
    margin: 6pt 0 10pt 0;
    font-size: 8.5pt;
}

thead tr { background-color: #0f172a; color: #f1f5f9; }

th {
    padding: 4pt 7pt;
    text-align: left;
    font-weight: bold;
    font-size: 8pt;
}

td {
    padding: 3pt 7pt;
    border-bottom: 0.5pt solid #e2e8f0;
    vertical-align: top;
}

tr:nth-child(even) td { background-color: #f8fafc; }

pre {
    background-color: #1e293b;
    color: #e2e8f0;
    padding: 8pt 10pt;
    margin: 6pt 0 8pt 0;
    font-size: 8pt;
    font-family: Courier, monospace;
    white-space: pre-wrap;
    word-wrap: break-word;
}

code {
    font-family: Courier, monospace;
    background-color: #f1f5f9;
    color: #1e40af;
    padding: 1pt 3pt;
    font-size: 8.5pt;
}

pre code {
    background-color: transparent;
    color: #e2e8f0;
    padding: 0;
}

ul, ol { margin: 3pt 0 6pt 16pt; }
li { margin: 1pt 0; }

blockquote {
    border-left: 3pt solid #1e3a5f;
    padding: 5pt 10pt;
    margin: 6pt 0;
    background-color: #f0f9ff;
    color: #1e3a5f;
    font-style: italic;
}

hr {
    border: none;
    border-top: 0.75pt solid #cbd5e1;
    margin: 12pt 0;
}

strong { font-weight: bold; }
em { font-style: italic; }
a { color: #1e40af; }
"""

# ---------------------------------------------------------------------------
# Document categories and source files
# ---------------------------------------------------------------------------
GOVERNANCE_DIR   = ROOT / "governance"
BOARD_PACK_DIR   = ROOT / "board_pack"
SCRIPTS_DIR      = ROOT / "scripts"

# Files to convert — in logical bundle order
DOCUMENT_GROUPS: list[tuple[str, list[Path]]] = [
    # Group 1 — Certification
    ("01_Certification", [
        ROOT / "INSTITUTIONAL_AUDIT_CERTIFICATION_2026-03-03.md",
    ]),
    # Group 2 — Board Pack
    ("02_Board_Pack", sorted(BOARD_PACK_DIR.glob("*.md")) if BOARD_PACK_DIR.exists() else []),
    # Group 3 — Governance
    ("03_Governance", sorted(GOVERNANCE_DIR.glob("*.md")) if GOVERNANCE_DIR.exists() else []),
    # Group 4 — Audit reports in root
    ("04_Audit_Reports", [
        p for p in ROOT.glob("*.md")
        if p.stem not in {"INSTITUTIONAL_AUDIT_CERTIFICATION_2026-03-03", "README"}
           and "REPORT" in p.stem.upper() or "AUDIT" in p.stem.upper() or "READINESS" in p.stem.upper()
    ]),
]

# ---------------------------------------------------------------------------
# Markdown → HTML → PDF pipeline
# ---------------------------------------------------------------------------
EXTRAS = [
    "tables", "fenced-code-blocks", "header-ids", "task_list",
    "strike", "footnotes", "code-friendly", "cuddled-lists",
]

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <style>
{stylesheet}
  </style>
</head>
<body>
{body}
<div style="margin-top:20pt;font-size:7pt;color:#94a3b8;border-top:0.5pt solid #e2e8f0;padding-top:6pt;">
  Generated: {generated} &nbsp;|&nbsp; PF Trust Bond Intelligence Platform &nbsp;|&nbsp; CONFIDENTIAL
</div>
</body>
</html>
"""


def md_to_html(md_text: str, title: str) -> str:
    body = markdown2.markdown(md_text, extras=EXTRAS)
    return HTML_TEMPLATE.format(
        title=title,
        stylesheet=STYLESHEET,
        body=body,
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def html_to_pdf(html: str, out_path: Path) -> bool:
    """Convert HTML string to PDF using xhtml2pdf (pure Python, no native deps)."""
    try:
        buf = io.BytesIO()
        result = pisa.CreatePDF(html, dest=buf, encoding="utf-8")
        buf.seek(0)
        out_path.write_bytes(buf.read())
        if result.err:
            print(f"    ⚠️  xhtml2pdf minor errors: {result.err}")
        return True  # file is still written even with minor layout errors
    except Exception as exc:
        print(f"    ⚠️  PDF error: {exc}")
        return False


def convert_md_file(md_path: Path, out_dir: Path) -> dict:
    """Convert a single .md file to PDF. Returns metadata dict."""
    stem = md_path.stem
    pdf_name = stem + ".pdf"
    out_path = out_dir / pdf_name

    md_text = md_path.read_text(encoding="utf-8", errors="replace")
    # Extract title from first H1 heading
    title = stem.replace("_", " ").replace("-", " ")
    for line in md_text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    html = md_to_html(md_text, title)
    success = html_to_pdf(html, out_path)

    size_kb = round(out_path.stat().st_size / 1024, 1) if out_path.exists() else 0
    return {
        "source": str(md_path.relative_to(ROOT)),
        "pdf": pdf_name,
        "title": title,
        "size_kb": size_kb,
        "success": success,
    }


# ---------------------------------------------------------------------------
# Bundle index generator
# ---------------------------------------------------------------------------
def generate_index_html(manifest: list[dict]) -> str:
    """Generate an HTML index page of all PDFs."""
    rows = ""
    for i, item in enumerate(manifest, 1):
        icon = "✅" if item["success"] else "❌"
        rows += f"""
        <tr>
          <td style="font-family:monospace;color:#64748b">{i:02d}</td>
          <td><a href="{item['pdf']}" style="color:#1e40af;font-weight:600">{item['title']}</a></td>
          <td style="font-family:monospace;color:#64748b">{item['size_kb']} KB</td>
          <td style="font-size:12px;color:#64748b">{item['source']}</td>
          <td>{icon}</td>
        </tr>"""

    total_size = sum(i["size_kb"] for i in manifest)
    success_count = sum(1 for i in manifest if i["success"])
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>PDF Bundle — PF Trust Bond Intelligence Platform</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;padding:40px;line-height:1.6}}
    h1{{font-size:26px;font-weight:800;margin-bottom:4px}}
    .sub{{color:#64748b;font-size:13px;margin-bottom:32px}}
    .kpi{{display:flex;gap:32px;margin-bottom:32px}}
    .kpi-item{{background:#1e293b;border-radius:12px;padding:16px 24px}}
    .kpi-item .label{{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px}}
    .kpi-item .val{{font-size:28px;font-weight:800;color:#22c55e;margin-top:4px}}
    table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden}}
    th{{background:#0f172a;color:#64748b;font-size:11px;text-transform:uppercase;padding:10px 16px;text-align:left}}
    td{{padding:10px 16px;border-bottom:1px solid #0f172a;font-size:13px}}
    tr:last-child td{{border-bottom:none}}
    .footer{{margin-top:32px;font-size:11px;color:#475569;text-align:center}}
  </style>
</head>
<body>
  <h1>📄 PDF Report Bundle</h1>
  <div class="sub">PF Trust Bond Intelligence Platform · All Governance, Board Pack & Audit Documents · {generated}</div>
  <div class="kpi">
    <div class="kpi-item"><div class="label">Documents</div><div class="val">{len(manifest)}</div></div>
    <div class="kpi-item"><div class="label">Converted</div><div class="val">{success_count}</div></div>
    <div class="kpi-item"><div class="label">Total Size</div><div class="val">{total_size:.0f} KB</div></div>
  </div>
  <table>
    <thead><tr><th>#</th><th>Document</th><th>Size</th><th>Source</th><th>Status</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="footer">Generated by generate_pdf_bundle.py · PF Trust Bond Intelligence Platform · CONFIDENTIAL</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Convert all .md reports to PDF bundle")
    parser.add_argument("--all", action="store_true", help="Include all .md files recursively")
    parser.add_argument("--dir", default=None, help="Only convert files from specific subdir")
    parser.add_argument("--out", default=str(BUNDLE_DIR), help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []

    if args.all or not args.dir:
        # Collect all .md files in structured order
        all_files: list[Path] = []
        seen: set[Path] = set()

        for _group, paths in DOCUMENT_GROUPS:
            for p in paths:
                if p.exists() and p not in seen:
                    all_files.append(p)
                    seen.add(p)

        # Also pick up any .md in root Private3 not yet included
        for p in sorted(ROOT.glob("*.md")):
            if p not in seen and p.exists():
                all_files.append(p)
                seen.add(p)
    else:
        target_dir = ROOT / args.dir
        all_files = sorted(target_dir.glob("*.md")) if target_dir.exists() else []

    print(f"\n{'═' * 60}")
    print("  PF TRUST — PDF BUNDLE GENERATOR")
    print(f"  Output: {out_dir}")
    print(f"  Files to process: {len(all_files)}")
    print(f"{'═' * 60}\n")

    for md_path in all_files:
        rel = md_path.relative_to(ROOT)
        print(f"  Converting: {rel}  ... ", end="", flush=True)
        result = convert_md_file(md_path, out_dir)
        manifest.append(result)
        if result["success"]:
            print(f"✅ {result['size_kb']} KB  →  {result['pdf']}")
        else:
            print(f"❌ FAILED")

    # Write manifest JSON
    manifest_path = out_dir / "BUNDLE_MANIFEST.json"
    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_documents": len(manifest),
                "success_count": sum(1 for m in manifest if m["success"]),
                "total_size_kb": round(sum(m["size_kb"] for m in manifest), 1),
                "documents": manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Write HTML index
    index_path = out_dir / "INDEX.html"
    index_path.write_text(generate_index_html(manifest), encoding="utf-8")

    success_n = sum(1 for m in manifest if m["success"])
    total_kb = round(sum(m["size_kb"] for m in manifest), 1)

    print(f"\n{'═' * 60}")
    print(f"  ✅  {success_n}/{len(manifest)} PDFs generated")
    print(f"  📦  Total bundle size: {total_kb} KB")
    print(f"  📋  Manifest: {manifest_path.name}")
    print(f"  🌐  Index:    {index_path.name}")
    print(f"  📁  Location: {out_dir}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
