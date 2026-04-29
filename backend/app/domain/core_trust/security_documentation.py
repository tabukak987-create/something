"""
Security & Infrastructure Documentation Service
==================================================
Phase 6: Generates comprehensive security documentation for audit readiness.
Covers hosting, encryption, auth, session management, backup, DR, and network security.
"""

from __future__ import annotations

from typing import Any

from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso

log = get_logger("services.security_docs")


class SecurityDocumentationService:
    """Generates institutional-grade security & infrastructure documentation."""

    @staticmethod
    def get_full_documentation() -> dict[str, Any]:
        """Return complete security posture documentation."""
        return {
            "document_title": "PF Trust Bond Investment Platform — Security & Infrastructure Documentation",
            "version": "1.0",
            "classification": "CONFIDENTIAL — For Authorized Personnel Only",
            "generated_at": utcnow_iso(),
            "sections": {
                "1_architecture_overview": SecurityDocumentationService._architecture_overview(),
                "2_authentication_authorization": SecurityDocumentationService._auth_documentation(),
                "3_encryption": SecurityDocumentationService._encryption_documentation(),
                "4_network_security": SecurityDocumentationService._network_security(),
                "5_data_protection": SecurityDocumentationService._data_protection(),
                "6_backup_disaster_recovery": SecurityDocumentationService._backup_dr(),
                "7_monitoring_alerting": SecurityDocumentationService._monitoring(),
                "8_incident_response": SecurityDocumentationService._incident_response(),
                "9_compliance_certifications": SecurityDocumentationService._compliance_certifications(),
                "10_vulnerability_management": SecurityDocumentationService._vulnerability_management(),
            },
        }

    @staticmethod
    def _architecture_overview() -> dict:
        return {
            "title": "Architecture Overview",
            "deployment_model": "Hybrid — Docker Compose containerized services with local PostgreSQL 18",
            "services": [
                {
                    "name": "Unified Gateway",
                    "port": 9000,
                    "technology": "FastAPI (Python 3.12+)",
                    "role": "Central API gateway — authentication, routing, aggregation",
                    "containerized": True,
                },
                {
                    "name": "Investment Decision Engine",
                    "port": 8900,
                    "technology": "FastAPI (Python 3.12+)",
                    "role": "Portfolio analytics, risk calculations, trade execution",
                    "containerized": True,
                },
                {
                    "name": "Compliance Engine",
                    "port": 8100,
                    "technology": "FastAPI (Python 3.12+)",
                    "role": "37+ regulatory compliance rule checks, EPFO/SEBI/RBI validation",
                    "containerized": True,
                },
                {
                    "name": "Bond Intelligence",
                    "port": 8500,
                    "technology": "FastAPI (Python 3.12+)",
                    "role": "Bond data aggregation — BSE, NSE, SEBI, RBI data feeds",
                    "containerized": True,
                },
                {
                    "name": "Institutional FIS",
                    "port": 8600,
                    "technology": "FastAPI (Python 3.12+)",
                    "role": "Fixed Income Securities — yield curve, G-Sec benchmarks",
                    "containerized": True,
                },
                {
                    "name": "Bond Quote System",
                    "port": 8000,
                    "technology": "FastAPI (Python 3.12+)",
                    "role": "Real-time broker quote aggregation",
                    "containerized": True,
                },
            ],
            "infrastructure": {
                "database": "PostgreSQL 18 (local) with async SQLAlchemy (asyncpg)",
                "cache": "Redis 7 (containerized) — session cache, pub/sub",
                "reverse_proxy": "Nginx — TLS termination, rate limiting, load balancing",
                "tunnel": "Cloudflare Tunnel for secure external access",
                "container_runtime": "Docker Compose v3.9",
            },
        }

    @staticmethod
    def _auth_documentation() -> dict:
        return {
            "title": "Authentication & Authorization",
            "authentication_method": "JWT Bearer Token (OAuth2 Password Flow)",
            "token_configuration": {
                "access_token": {
                    "algorithm": "HS256",
                    "ttl_minutes": 60,
                    "payload": ["user_id", "username", "role", "exp", "iat"],
                },
                "refresh_token": {
                    "algorithm": "HS256",
                    "ttl_days": 7,
                    "rotation": "Issued on refresh, old token invalidated",
                },
                "secret_key": "Auto-generated 64-byte hex via secrets.token_hex(32) if not set in env",
            },
            "password_security": {
                "hashing": "bcrypt with automatic salt generation",
                "library": "bcrypt (direct) — passlib removed for compatibility",
                "min_rounds": 12,
            },
            "rbac_model": {
                "type": "7-Tier Role-Based Access Control",
                "roles": [
                    {"role": "SUPER_ADMIN", "level": 7, "description": "Full system control, security configuration"},
                    {"role": "ADMIN", "level": 6, "description": "User management, system configuration"},
                    {"role": "PORTFOLIO", "level": 5, "description": "Portfolio management, trade execution"},
                    {"role": "TRUSTEE", "level": 4, "description": "Board-level reporting, approval authority"},
                    {"role": "COMPLIANCE", "level": 3, "description": "Compliance monitoring, rule management"},
                    {"role": "AUDITOR", "level": 2, "description": "Read-only audit trail, integrity verification"},
                    {"role": "VIEWER", "level": 1, "description": "Read-only portfolio and market data"},
                ],
                "permission_enforcement": "Endpoint-level via require_role() dependency injection",
                "permission_granularity": "Fine-grained: 50+ discrete permissions mapped to roles",
            },
            "session_management": {
                "type": "Stateless JWT — no server-side session storage",
                "concurrent_sessions": "Unlimited (controlled by token expiry)",
                "logout": "Client-side token disposal",
                "recommendation": "Implement server-side token blacklist for forced logout",
            },
        }

    @staticmethod
    def _encryption_documentation() -> dict:
        return {
            "title": "Encryption & Data Protection",
            "transport_encryption": {
                "protocol": "TLS 1.2 / TLS 1.3",
                "enforced_via": "Nginx SSL termination + HSTS header (max-age=31536000)",
                "http_redirect": "HTTP → HTTPS 301 redirect configured in Nginx",
                "cipher_suites": "HIGH:!aNULL:!MD5 (Nginx configuration)",
                "certificate": "Self-signed (dev) / Let's Encrypt (prod)",
            },
            "data_at_rest": {
                "database": "PostgreSQL native encryption — pg_tde extension recommended for column-level",
                "file_system": "Windows NTFS — BitLocker recommended for production",
                "backups": "Unencrypted currently — GPG encryption recommended",
                "status": "PARTIAL — Transport encrypted, at-rest encryption needs hardening",
            },
            "api_security_headers": {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "X-XSS-Protection": "1; mode=block",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                "Cache-Control": "no-store",
            },
        }

    @staticmethod
    def _network_security() -> dict:
        return {
            "title": "Network Security",
            "reverse_proxy": {
                "technology": "Nginx",
                "capabilities": [
                    "TLS termination",
                    "HTTP → HTTPS redirect",
                    "Rate limiting (API: 30r/m, Auth: 5r/m per IP)",
                    "Security headers injection",
                    "Upstream health checks",
                    "Request buffering",
                ],
            },
            "rate_limiting": {
                "nginx_layer": {
                    "api_endpoints": "30 requests/minute per IP (limit_req_zone)",
                    "auth_endpoints": "5 requests/minute per IP (against brute force)",
                },
                "application_layer": {
                    "gateway": "120 RPM per IP (token-bucket algorithm)",
                    "investment_engine": "30 RPM per IP",
                    "configurable": "Via RATE_LIMIT_RPM environment variable",
                },
            },
            "cors_policy": {
                "allowed_origins": "localhost:9000, 127.0.0.1:9000 (default)",
                "configurable": "Via CORS_ALLOWED_ORIGINS environment variable",
                "credentials": True,
                "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                "headers": ["Authorization", "Content-Type", "Accept"],
            },
            "external_access": {
                "method": "Cloudflare Tunnel (cloudflared)",
                "benefits": [
                    "No inbound ports required",
                    "DDoS protection by Cloudflare",
                    "Zero-trust network access",
                    "Automatic TLS",
                ],
            },
            "container_networking": {
                "type": "Docker bridge network (bond_net)",
                "inter_service": "Container-to-container via service names",
                "external_exposure": "Only gateway port 9000 exposed",
            },
        }

    @staticmethod
    def _data_protection() -> dict:
        return {
            "title": "Data Protection & Privacy",
            "data_classification": {
                "critical": ["user credentials", "JWT secrets", "API keys", "portfolio positions"],
                "sensitive": ["trade history", "compliance breaches", "risk metrics", "audit logs"],
                "internal": ["bond master data", "market data", "yield curves"],
                "public": ["API documentation", "static UI assets"],
            },
            "pii_handling": {
                "user_data_stored": ["username", "hashed_password", "role", "email (optional)"],
                "no_pii_collected": "No Aadhaar, PAN, bank account, or personal identifiers stored",
                "data_minimization": "Principle of least data collection followed",
            },
            "audit_trail": {
                "implementation": "Immutable append-only audit log (institutional_audit_log table)",
                "integrity": "SHA-256 chain checksums — tamper-evident",
                "retention": "Indefinite — no auto-purge (regulatory retention compliance)",
                "actions_logged": "30+ action types including trades, compliance changes, auth events",
            },
            "input_validation": {
                "framework": "Pydantic v2 models for all API inputs",
                "sql_injection": "Prevented via SQLAlchemy parameterized queries",
                "xss": "API-only (no server-side HTML rendering) — React frontend handles sanitization",
            },
        }

    @staticmethod
    def _backup_dr() -> dict:
        return {
            "title": "Backup & Disaster Recovery",
            "backup_strategy": {
                "method": "PowerShell scheduled task (Backup_TaskScheduler.xml)",
                "database": "pg_dump full database backup",
                "frequency": "Daily automated + on-demand",
                "storage": "Local backup directory with timestamped folders",
                "retention": "30-day rolling (configurable)",
                "script": "Backup_Compliance.ps1",
            },
            "disaster_recovery": {
                "rpo": "24 hours (daily backup cycle)",
                "rto": "2-4 hours (Docker Compose full stack redeploy)",
                "procedure": [
                    "1. Restore PostgreSQL from latest pg_dump backup",
                    "2. docker compose up -d (all services from images)",
                    "3. Verify data integrity via /api/v1/governance/audit-log/integrity",
                    "4. Run compliance scan via /api/v1/institutional/compliance/scan",
                    "5. Validate market data freshness via /api/v1/institutional/data/freshness",
                ],
                "recommendations": [
                    "Implement off-site backup replication (S3/Azure Blob)",
                    "Enable PostgreSQL WAL archiving for point-in-time recovery",
                    "Add automated DR drill schedule (quarterly)",
                    "Implement hot standby database replication",
                ],
            },
        }

    @staticmethod
    def _monitoring() -> dict:
        return {
            "title": "Monitoring & Alerting",
            "health_checks": {
                "endpoint": "/api/v1/health",
                "frequency": "Every 5 minutes (APScheduler)",
                "checks": ["database connectivity", "service availability", "market data freshness"],
            },
            "logging": {
                "framework": "structlog (JSON structured logging)",
                "log_levels": "Configurable via LOG_LEVEL env var",
                "output": "stdout (Docker) + file rotation",
                "context": "Service name, timestamp, request_id, user_id attached to all entries",
            },
            "alerts": {
                "compliance_breaches": "Logged with severity level, tracked in breach history",
                "data_anomalies": "Automatic detection via price validation (>5% spike, zero/negative)",
                "system_alerts": "APScheduler-driven periodic checks",
                "channels": "Email alerts configurable (email_config.json)",
            },
            "monitoring_stack": {
                "available": "Docker Compose --profile monitoring",
                "components": ["Prometheus (metrics collection)", "Grafana (dashboards)"],
                "status": "Configured but optional profile",
            },
        }

    @staticmethod
    def _incident_response() -> dict:
        return {
            "title": "Incident Response Plan",
            "severity_levels": [
                {
                    "level": "P1 — Critical",
                    "definition": "System down, data breach, unauthorized access",
                    "response_time": "< 15 minutes",
                    "examples": ["Database compromise", "JWT secret leak", "Total service outage"],
                },
                {
                    "level": "P2 — High",
                    "definition": "Major compliance breach, data feed failure",
                    "response_time": "< 1 hour",
                    "examples": ["EPFO limit exceedance", "All market data stale >24h", "Auth service failure"],
                },
                {
                    "level": "P3 — Medium",
                    "definition": "Single service degradation, minor data anomaly",
                    "response_time": "< 4 hours",
                    "examples": ["One data source offline", "Rate limit exceeded", "Non-critical API error spike"],
                },
                {
                    "level": "P4 — Low",
                    "definition": "Cosmetic issues, performance optimization",
                    "response_time": "Next business day",
                    "examples": ["Slow report generation", "UI rendering issue", "Non-blocking warning logs"],
                },
            ],
            "response_procedure": [
                "1. DETECT — Automated monitoring + manual observation",
                "2. TRIAGE — Classify severity, assign responder",
                "3. CONTAIN — Isolate affected service, activate fallback",
                "4. INVESTIGATE — Review audit logs, identify root cause",
                "5. RESOLVE — Apply fix, verify resolution",
                "6. COMMUNICATE — Notify stakeholders per severity level",
                "7. POST-MORTEM — Document RCA, update runbooks",
            ],
            "escalation_matrix": {
                "P1": "CTO + CISO + Board notification within 30 minutes",
                "P2": "CTO + Compliance Officer within 2 hours",
                "P3": "Engineering Lead within 4 hours",
                "P4": "Standard ticket workflow",
            },
        }

    @staticmethod
    def _compliance_certifications() -> dict:
        return {
            "title": "Regulatory Compliance & Certifications",
            "applicable_regulations": [
                {
                    "regulation": "EPFO Investment Guidelines",
                    "status": "IMPLEMENTED",
                    "coverage": "37+ compliance rule checks, single/group/sector limits",
                },
                {
                    "regulation": "SEBI (Mutual Funds) Regulations",
                    "status": "MONITORED",
                    "coverage": "rating-based limits, concentration checks",
                },
                {
                    "regulation": "RBI Guidelines on PF Investments",
                    "status": "MONITORED",
                    "coverage": "Government securities allocation, duration limits",
                },
                {
                    "regulation": "IT Act 2000 — Data Security",
                    "status": "PARTIAL",
                    "coverage": "Encryption in transit, access controls, audit trail",
                },
                {
                    "regulation": "DPDP Act 2023 (Data Protection)",
                    "status": "NOT APPLICABLE",
                    "coverage": "No personal data (PII) collected beyond username/email",
                },
            ],
            "certifications_pending": [
                "ISO 27001 (Information Security) — Recommended",
                "SOC 2 Type II — Recommended for institutional clients",
                "STQC Certification — Required for government-facing deployment",
            ],
        }

    @staticmethod
    def _vulnerability_management() -> dict:
        return {
            "title": "Vulnerability Management",
            "dependency_scanning": {
                "tool": "pip-audit / safety (recommended)",
                "frequency": "Weekly automated + before each release",
                "known_issues": "passlib bcrypt compatibility workaround in auth.py",
            },
            "code_review": {
                "static_analysis": "Recommended: bandit, semgrep for Python security scanning",
                "secrets_scanning": "Recommended: gitleaks, trufflehog for credential detection",
                "current_status": "Manual review — automated tools not yet integrated",
            },
            "penetration_testing": {
                "frequency": "Recommended: Annual third-party pentest",
                "last_test": "Not conducted",
                "scope": "API endpoints, authentication flow, authorization bypass, injection attacks",
            },
            "patch_management": {
                "os": "Windows Server — monthly Windows Update cycle",
                "python": "requirements.txt pinned versions — manual update cycle",
                "docker": "Base images updated on rebuild",
                "database": "PostgreSQL 18 — latest stable",
            },
            "known_security_gaps": [
                "No server-side token blacklist (logout is client-side only)",
                "JWT secret auto-generated on startup if not set in env (non-persistent across restarts)",
                "No at-rest encryption for database or backup files",
                "No automated dependency vulnerability scanning pipeline",
                "No WAF (Web Application Firewall) configured",
                "No IP allowlisting for admin endpoints",
            ],
            "remediation_roadmap": [
                {"priority": "HIGH", "item": "Pin JWT_SECRET_KEY in .env file", "effort": "1 hour"},
                {"priority": "HIGH", "item": "Enable BitLocker for data-at-rest encryption", "effort": "2 hours"},
                {"priority": "MEDIUM", "item": "Add server-side token blacklist (Redis)", "effort": "4 hours"},
                {"priority": "MEDIUM", "item": "Integrate pip-audit in CI/CD pipeline", "effort": "2 hours"},
                {"priority": "MEDIUM", "item": "Add IP allowlist for /governance/* endpoints", "effort": "3 hours"},
                {"priority": "LOW", "item": "Deploy WAF (Cloudflare WAF or ModSecurity)", "effort": "8 hours"},
                {"priority": "LOW", "item": "ISO 27001 gap assessment", "effort": "40 hours"},
            ],
        }
