"""
Prometheus Metrics & Alerting Service
======================================
Exposes application metrics in Prometheus format and provides
alerting hooks for email, webhook, and SMS notification channels.

Usage:
    from app.shared.metrics import metrics_router, track_request

    app.include_router(metrics_router)
"""

from __future__ import annotations

import os
import time
import json
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Response
from .logger import get_logger
from .time_utils import utcnow, utcnow_iso

log = get_logger("metrics")

metrics_router = APIRouter(tags=["Monitoring"])


# ══════════════════════════════════════════════════════════════════════════════
# In-Process Metrics Collection
# ══════════════════════════════════════════════════════════════════════════════

class MetricsCollector:
    """
    Lightweight in-process metrics collector.
    Tracks request counts, latencies, error rates, and custom gauges.
    Thread-safe via defaultdict atomicity.
    """

    def __init__(self):
        self._request_count: dict[str, int] = defaultdict(int)
        self._request_errors: dict[str, int] = defaultdict(int)
        self._request_duration_sum: dict[str, float] = defaultdict(float)
        self._request_duration_count: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._start_time = time.time()

    def track_request(self, method: str, path: str, status_code: int, duration_s: float):
        """Record a completed HTTP request."""
        key = f"{method}:{path}"
        self._request_count[key] += 1
        self._request_duration_sum[key] += duration_s
        self._request_duration_count[key] += 1
        if status_code >= 500:
            self._request_errors[key] += 1

    def set_gauge(self, name: str, value: float):
        """Set a gauge metric (e.g., active connections, queue depth)."""
        self._gauges[name] = value

    def to_prometheus(self) -> str:
        """Export all metrics in Prometheus text exposition format."""
        lines = []
        uptime = time.time() - self._start_time

        # Uptime
        lines.append("# HELP gateway_uptime_seconds Gateway uptime in seconds")
        lines.append("# TYPE gateway_uptime_seconds gauge")
        lines.append(f"gateway_uptime_seconds {uptime:.1f}")

        # Request count
        lines.append("# HELP gateway_http_requests_total Total HTTP requests")
        lines.append("# TYPE gateway_http_requests_total counter")
        for key, count in sorted(self._request_count.items()):
            method, path = key.split(":", 1)
            lines.append(f'gateway_http_requests_total{{method="{method}",path="{path}"}} {count}')

        # Error count
        lines.append("# HELP gateway_http_errors_total Total HTTP 5xx errors")
        lines.append("# TYPE gateway_http_errors_total counter")
        for key, count in sorted(self._request_errors.items()):
            method, path = key.split(":", 1)
            lines.append(f'gateway_http_errors_total{{method="{method}",path="{path}"}} {count}')

        # Latency
        lines.append("# HELP gateway_http_request_duration_seconds HTTP request duration")
        lines.append("# TYPE gateway_http_request_duration_seconds summary")
        for key in sorted(self._request_duration_sum.keys()):
            method, path = key.split(":", 1)
            total = self._request_duration_sum[key]
            count = self._request_duration_count[key]
            lines.append(f'gateway_http_request_duration_seconds_sum{{method="{method}",path="{path}"}} {total:.4f}')
            lines.append(f'gateway_http_request_duration_seconds_count{{method="{method}",path="{path}"}} {count}')

        # Custom gauges
        for name, value in sorted(self._gauges.items()):
            safe_name = name.replace(".", "_").replace("-", "_")
            lines.append(f"# HELP gateway_{safe_name} Custom gauge")
            lines.append(f"# TYPE gateway_{safe_name} gauge")
            lines.append(f"gateway_{safe_name} {value}")

        return "\n".join(lines) + "\n"


# Singleton
collector = MetricsCollector()


# ══════════════════════════════════════════════════════════════════════════════
# Prometheus Metrics Endpoint
# ══════════════════════════════════════════════════════════════════════════════

@metrics_router.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Prometheus scrape endpoint. Returns metrics in text exposition format."""
    content = collector.to_prometheus()
    return Response(content=content, media_type="text/plain; charset=utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# Alert Manager
# ══════════════════════════════════════════════════════════════════════════════

class AlertSeverity:
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertChannel:
    """Base class for alert delivery channels."""

    async def send(self, severity: str, title: str, message: str, metadata: dict = None) -> bool:
        raise NotImplementedError


class EmailAlertChannel(AlertChannel):
    """Send alerts via email (SMTP)."""

    def __init__(self):
        self.smtp_host = os.getenv("ALERT_SMTP_HOST", "")
        self.smtp_port = int(os.getenv("ALERT_SMTP_PORT", "587"))
        self.smtp_user = os.getenv("ALERT_SMTP_USER", "")
        self.smtp_pass = os.getenv("ALERT_SMTP_PASSWORD", "")
        self.from_addr = os.getenv("ALERT_FROM_EMAIL", "alerts@pftrust.bond")
        self.to_addrs = os.getenv("ALERT_TO_EMAIL", "").split(",")

    async def send(self, severity: str, title: str, message: str, metadata: dict = None) -> bool:
        if not self.smtp_host or not self.to_addrs[0]:
            log.warning("email_alert_skipped", reason="SMTP not configured")
            return False
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(f"Severity: {severity}\n\n{message}\n\nMetadata: {json.dumps(metadata or {})}")
            msg["Subject"] = f"[PF Trust Alert] [{severity}] {title}"
            msg["From"] = self.from_addr
            msg["To"] = ", ".join(self.to_addrs)
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_pass)
                server.send_message(msg)
            log.info("email_alert_sent", title=title, severity=severity)
            return True
        except Exception as exc:
            log.error("email_alert_failed", error=str(exc))
            return False


class WebhookAlertChannel(AlertChannel):
    """Send alerts via HTTP webhook (Slack, Teams, PagerDuty, etc.)."""

    def __init__(self):
        self.webhook_url = os.getenv("ALERT_WEBHOOK_URL", "")
        self.webhook_secret = os.getenv("ALERT_WEBHOOK_SECRET", "")

    async def send(self, severity: str, title: str, message: str, metadata: dict = None) -> bool:
        if not self.webhook_url:
            log.warning("webhook_alert_skipped", reason="ALERT_WEBHOOK_URL not configured")
            return False
        try:
            import httpx
            payload = {
                "severity": severity,
                "title": title,
                "message": message,
                "timestamp": utcnow_iso(),
                "source": "pf-trust-gateway",
                "metadata": metadata or {},
            }
            headers = {"Content-Type": "application/json"}
            if self.webhook_secret:
                import hashlib, hmac
                sig = hmac.new(self.webhook_secret.encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()
                headers["X-Webhook-Signature"] = sig
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(self.webhook_url, json=payload, headers=headers)
                log.info("webhook_alert_sent", title=title, status=resp.status_code)
                return resp.status_code < 400
        except Exception as exc:
            log.error("webhook_alert_failed", error=str(exc))
            return False


class SMSAlertChannel(AlertChannel):
    """Send alerts via SMS (stub — integrate with Twilio/AWS SNS)."""

    def __init__(self):
        self.enabled = os.getenv("ALERT_SMS_ENABLED", "false").lower() in ("true", "1")
        self.phone_numbers = os.getenv("ALERT_SMS_NUMBERS", "").split(",")

    async def send(self, severity: str, title: str, message: str, metadata: dict = None) -> bool:
        if not self.enabled or not self.phone_numbers[0]:
            log.warning("sms_alert_skipped", reason="SMS not configured")
            return False
        # Stub — replace with actual Twilio/SNS integration
        log.info("sms_alert_stub", title=title, severity=severity,
                 phones=self.phone_numbers, note="Implement Twilio/SNS integration")
        return True


class AlertManager:
    """
    Centralized alert dispatching across all configured channels.
    Implements deduplication to avoid alert storms.
    """

    def __init__(self):
        self._channels: list[AlertChannel] = [
            EmailAlertChannel(),
            WebhookAlertChannel(),
            SMSAlertChannel(),
        ]
        self._recent_alerts: dict[str, datetime] = {}
        self._dedup_window = timedelta(minutes=int(os.getenv("ALERT_DEDUP_MINUTES", "10")))

    async def fire(self, severity: str, title: str, message: str, metadata: dict = None):
        """Fire an alert to all configured channels with deduplication."""
        dedup_key = f"{severity}:{title}"
        now = utcnow()

        # Deduplication: skip if same alert fired within window
        last_fired = self._recent_alerts.get(dedup_key)
        if last_fired and (now - last_fired) < self._dedup_window:
            log.debug("alert_deduplicated", title=title)
            return

        self._recent_alerts[dedup_key] = now
        log.info("alert_firing", severity=severity, title=title)

        for channel in self._channels:
            try:
                await channel.send(severity, title, message, metadata)
            except Exception as exc:
                log.error("alert_channel_failed", channel=type(channel).__name__, error=str(exc))


# Singleton
alert_manager = AlertManager()
