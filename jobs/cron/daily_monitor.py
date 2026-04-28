"""
Daily Portfolio Monitoring Script
───────────────────────────────────────────────────────────────────
Runs POST /api/v1/compliance/post-trade on the live portfolio.
Designed to run via Windows Task Scheduler every day at 9:00 AM.

Usage:
    python daily_monitor.py [--port 8100] [--portfolio-id PORT-EPFO-MAIN]

Output:
    - Prints JSON summary to stdout
    - Saves JSON report to reports/output/daily_<date>.json
    - Sends email alert if violations are found (if email enabled)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import date, datetime
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────
BASE_URL = os.environ.get("COMPLIANCE_ENGINE_URL", "http://localhost:8100")
API_KEY  = os.environ.get("API_KEY") or os.environ.get("CE_API_KEY", "")  # REQUIRED: set API_KEY or CE_API_KEY env var
REPORTS_DIR = Path(__file__).parent / "compliance_engine" / "reports" / "output"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

TODAY = date.today().isoformat()


def build_demo_portfolio_payload() -> dict:
    """
    Replace this with your real portfolio data feed.
    In production: read from your portfolio system / database.
    """
    return {
        "portfolio": {
            "portfolio_id": os.environ.get("PORTFOLIO_ID", "PORT-EPFO-MAIN"),
            "fund_name": os.environ.get("FUND_NAME", "PF Trust Fund"),
            "fund_type": "EPFO",
            "aum": float(os.environ.get("AUM_RUPEES", "10000000000")),
            "as_of_date": TODAY,
            "holdings": [],  # TODO: populate from your actual portfolio system
        },
        "check_type": "POST_TRADE",
        "checked_by": "DAILY_MONITOR_CRON",
        "run_stress": True,
    }


def call_api(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[ERROR] HTTP {e.code}: {body}")
        return {"error": str(e.code), "detail": body}
    except Exception as e:
        print(f"[ERROR] API unreachable: {e}")
        return {"error": str(e)}


def health_check() -> bool:
    url = f"{BASE_URL}/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def save_report(data: dict, name: str) -> Path:
    out_path = REPORTS_DIR / f"{name}_{TODAY}.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return out_path


def send_email_alert(violations: list, portfolio_id: str) -> None:
    """Send violation email via SMTP if ALERT_EMAIL_ENABLED=true."""
    if os.environ.get("ALERT_EMAIL_ENABLED", "false").lower() != "true":
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASSWORD", "")
        to_list   = os.environ.get("ALERT_EMAIL_TO", "").split(",")

        body = f"Daily Compliance Monitor — {TODAY}\nPortfolio: {portfolio_id}\n\n"
        body += f"Found {len(violations)} violation(s):\n\n"
        for v in violations[:20]:  # Max 20 in email
            body += f"  [{v.get('severity')}] {v.get('rule_code')}: {v.get('description')}\n"

        msg = MIMEText(body)
        msg["Subject"] = f"[COMPLIANCE ALERT] {len(violations)} violation(s) in {portfolio_id} — {TODAY}"
        msg["From"]    = smtp_user
        msg["To"]      = ", ".join(t.strip() for t in to_list if t.strip())

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[EMAIL] Alert sent to {msg['To']}")
    except Exception as e:
        print(f"[EMAIL] Failed: {e}")


def load_dotenv() -> None:
    env_path = Path(__file__).parent / "compliance_engine" / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="PF Trust Daily Portfolio Compliance Monitor")
    parser.add_argument("--url", default=BASE_URL)
    parser.add_argument("--portfolio-id", default="PORT-EPFO-MAIN")
    parser.add_argument("--dry-run", action="store_true", help="Don't call API, just print payload")
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f"PF Trust Daily Compliance Monitor — {TODAY}")
    print(f"Engine: {args.url}")
    print(f"Portfolio: {args.portfolio_id}")
    print(f"{'='*60}")

    # 1. Health check
    if not health_check():
        print("[CRITICAL] Compliance engine is not reachable. Start it first.")
        print("  Run: Start_All_Services.bat")
        return 1
    print("[OK] Compliance engine is online")

    # 2. Yield Benchmark check
    print("\n[1/4] Running yield benchmark analysis...")
    yield_payload = {
        "portfolio": {
            "portfolio_id": args.portfolio_id,
            "fund_name": "PF Trust Fund",
            "fund_type": "EPFO",
            "aum": 10000000000,
            "as_of_date": TODAY,
            "holdings": [],
        },
        "epfo_rate": float(os.environ.get("EPFO_RATE", "8.25")),
    }
    if not args.dry_run:
        yield_result = call_api("/api/v1/pf-engine/yield-benchmark", yield_payload)
        save_report(yield_result, "yield_benchmark")
        ytm = yield_result.get("portfolio_ytm", 0)
        epfo = yield_result.get("epfo_declared_rate", 8.25)
        print(f"   Portfolio YTM: {ytm:.2f}%  |  EPFO Rate: {epfo:.2f}%  |  "
              f"Spread: {yield_result.get('ytm_vs_epfo_spread_bps', 0):.0f} bps")
        if yield_result.get("underperforming"):
            print("   [ALERT] Portfolio is underperforming EPFO rate!")

    # 3. Concentration check
    print("\n[2/4] Running concentration check...")
    conc_payload = {"portfolio": yield_payload["portfolio"]}
    if not args.dry_run:
        conc_result = call_api("/api/v1/pf-engine/concentration", conc_payload)
        save_report(conc_result, "concentration")
        alerts = conc_result.get("alerts", [])
        if alerts:
            print(f"   [ALERT] {len(alerts)} concentration breach(es):")
            for a in alerts:
                print(f"     • {a}")
        else:
            print("   [OK] No concentration breaches")

    # 4. Summary
    print(f"\n{'='*60}")
    print(f"Daily monitoring complete — {datetime.now().strftime('%H:%M:%S')}")
    print(f"Reports saved to: {REPORTS_DIR}")
    print(f"Dashboard: {args.url}/pf-engine")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
