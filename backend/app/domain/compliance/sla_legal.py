"""
SLA & Legal Readiness Service
=================================
Phase 7: Service Level Agreement definitions, legal disclaimers,
data ownership terms, and liability limitations for institutional deployment.
"""

from __future__ import annotations

from typing import Any

from ..shared.logger import get_logger
from ..shared.time_utils import utcnow_iso

log = get_logger("services.sla_legal")


class SLALegalService:
    """Generates institutional SLA and legal documentation."""

    @staticmethod
    def get_full_documentation() -> dict[str, Any]:
        return {
            "document_title": "PF Trust Bond Investment Platform — SLA & Legal Framework",
            "version": "1.0",
            "classification": "CONFIDENTIAL",
            "effective_date": utcnow_iso()[:10],
            "generated_at": utcnow_iso(),
            "sections": {
                "1_service_level_agreement": SLALegalService._sla(),
                "2_data_disclaimer": SLALegalService._data_disclaimer(),
                "3_liability_limitation": SLALegalService._liability(),
                "4_data_ownership": SLALegalService._data_ownership(),
                "5_termination_terms": SLALegalService._termination(),
                "6_regulatory_compliance": SLALegalService._regulatory(),
                "7_confidentiality": SLALegalService._confidentiality(),
            },
        }

    @staticmethod
    def _sla() -> dict:
        return {
            "title": "Service Level Agreement (SLA)",
            "availability": {
                "target_uptime": "99.5% monthly (excludes scheduled maintenance)",
                "measurement": "Measured via health endpoint (/api/v1/health) at 5-minute intervals",
                "scheduled_maintenance_window": "Sunday 02:00-06:00 IST",
                "maintenance_notification": "48 hours advance notice via registered email",
                "unscheduled_downtime_target": "< 4.38 hours/month",
            },
            "performance_sla": {
                "api_response_time": {
                    "p50": "< 200ms",
                    "p95": "< 1000ms",
                    "p99": "< 3000ms",
                    "measurement": "Gateway-level response time including all downstream calls",
                },
                "report_generation": {
                    "standard_reports": "< 10 seconds",
                    "quarterly_trustee_report": "< 30 seconds",
                    "pdf_export": "< 60 seconds",
                },
                "data_freshness": {
                    "market_data": "Updated daily by 09:30 IST (T+0 trading day)",
                    "rating_changes": "Reflected within 24 hours of publication",
                    "new_bond_issuance": "Added within 48 hours of SEBI filing",
                },
            },
            "support_tiers": [
                {
                    "tier": "Standard",
                    "response_time": "Next business day",
                    "resolution_time": "5 business days",
                    "channels": ["Email"],
                    "hours": "Mon-Fri 09:00-18:00 IST",
                },
                {
                    "tier": "Premium",
                    "response_time": "4 hours",
                    "resolution_time": "2 business days",
                    "channels": ["Email", "Phone"],
                    "hours": "Mon-Sat 08:00-20:00 IST",
                },
                {
                    "tier": "Enterprise",
                    "response_time": "1 hour (P1/P2), 4 hours (P3/P4)",
                    "resolution_time": "8 hours (P1), 24 hours (P2), 3 business days (P3/P4)",
                    "channels": ["Email", "Phone", "Dedicated Slack/Teams"],
                    "hours": "24/7 for P1/P2",
                },
            ],
            "sla_credits": {
                "99.0_to_99.5": "5% monthly fee credit",
                "95.0_to_99.0": "10% monthly fee credit",
                "below_95.0": "25% monthly fee credit",
                "claim_process": "Written request within 30 days of incident with evidence",
            },
            "exclusions": [
                "Force majeure events (natural disasters, government actions, war)",
                "Client-side network or infrastructure issues",
                "Third-party data source outages (BSE, NSE, SEBI APIs)",
                "Scheduled maintenance during announced windows",
                "Client-requested configuration changes causing downtime",
                "Exceeding agreed rate limits or usage quotas",
            ],
        }

    @staticmethod
    def _data_disclaimer() -> dict:
        return {
            "title": "Data Accuracy & Disclaimer",
            "general_disclaimer": (
                "All bond data, market prices, yields, ratings, and analytical outputs provided by "
                "the PF Trust Bond Investment Platform are furnished on an 'AS IS' basis. While we "
                "employ multiple validation layers including anomaly detection, cross-source verification, "
                "and mathematical validation, no guarantee of absolute accuracy is provided."
            ),
            "data_sources": {
                "primary": ["BSE India", "NSE India", "RBI", "SEBI EDGAR filings"],
                "secondary": ["CRISIL", "ICRA", "CARE", "India Ratings (Fitch)"],
                "disclaimer": (
                    "Data is sourced from publicly available feeds and may be subject to delays, "
                    "errors, or omissions at the source. The platform applies best-effort validation "
                    "but users must independently verify critical data points before investment decisions."
                ),
            },
            "calculation_disclaimer": (
                "Financial calculations including YTM, Duration, Convexity, VaR, and risk metrics "
                "are based on standard fixed-income mathematical models. Results may differ from "
                "Bloomberg, Reuters, or other terminals due to methodological differences in day-count "
                "conventions, settlement assumptions, or interpolation methods. Calculation breakdowns "
                "are available via the Governance API for full transparency."
            ),
            "rating_disclaimer": (
                "Credit ratings displayed are sourced from SEBI-registered rating agencies. The platform "
                "does not provide independent credit assessments. Rating changes may reflect in the system "
                "with a delay of up to 24 hours from publication."
            ),
            "not_investment_advice": (
                "Nothing in this platform constitutes investment advice, solicitation, or recommendation "
                "to buy, sell, or hold any securities. All investment decisions must be made by authorized "
                "PF Trust officers in accordance with the Trust's Investment Policy Statement (IPS) and "
                "applicable EPFO/RBI/SEBI guidelines."
            ),
        }

    @staticmethod
    def _liability() -> dict:
        return {
            "title": "Limitation of Liability",
            "maximum_liability": (
                "Total aggregate liability shall not exceed the fees paid by the client "
                "in the twelve (12) months preceding the claim."
            ),
            "excluded_damages": [
                "Indirect, incidental, special, or consequential damages",
                "Loss of profits, revenue, or anticipated savings",
                "Loss of data beyond the scope of backup/DR commitments",
                "Trading losses based on platform data or calculations",
                "Regulatory penalties arising from client's investment decisions",
                "Third-party claims against the client",
            ],
            "data_liability": (
                "The platform provider is not liable for inaccuracies in data sourced from "
                "third-party providers (BSE, NSE, SEBI, rating agencies). The client assumes "
                "responsibility for independent verification of all data used for investment decisions."
            ),
            "system_failure": (
                "In the event of system failure, the platform provider's obligation is limited to "
                "restoring service within the RTO specified in the SLA. No liability arises from "
                "inability to execute trades, generate reports, or access data during downtime."
            ),
            "indemnification": (
                "Client shall indemnify and hold harmless the platform provider against any third-party "
                "claims arising from the client's use of the platform, including but not limited to "
                "regulatory actions, investor lawsuits, or audit findings."
            ),
        }

    @staticmethod
    def _data_ownership() -> dict:
        return {
            "title": "Data Ownership & Intellectual Property",
            "client_data": {
                "ownership": "Client retains full ownership of all portfolio data, trade records, and configurations",
                "portability": "Client may export all data via API at any time in JSON/CSV format",
                "deletion": "Full data deletion within 30 days of contract termination upon written request",
                "backup_responsibility": "Platform maintains 30-day rolling backups; long-term archival is client's responsibility",
            },
            "platform_data": {
                "bond_master_data": "Sourced from public registries — no proprietary ownership claimed",
                "market_data": "Sourced from public feeds — subject to exchange data redistribution policies",
                "analytics_models": "Proprietary — mathematical models, algorithms, and scoring engines",
                "audit_logs": "Joint ownership — client may request copies; platform retains for compliance",
            },
            "intellectual_property": {
                "platform_ip": [
                    "Source code, algorithms, and software architecture",
                    "Risk models and scoring methodologies",
                    "Compliance rule engine and validation logic",
                    "UI/UX design and branding elements",
                ],
                "license_grant": (
                    "Client receives a non-exclusive, non-transferable, revocable license to use "
                    "the platform for the duration of the subscription period."
                ),
            },
        }

    @staticmethod
    def _termination() -> dict:
        return {
            "title": "Termination Terms",
            "termination_by_client": {
                "notice_period": "90 days written notice",
                "data_export": "Full data export available during notice period",
                "fees": "Pro-rated refund for unused subscription period (annual plans only)",
                "data_retention_post_termination": "30 days — then permanently deleted",
            },
            "termination_by_provider": {
                "for_cause": [
                    "Non-payment after 30-day cure period",
                    "Violation of acceptable use policy",
                    "Regulatory order requiring service cessation",
                    "Breach of confidentiality obligations",
                ],
                "notice_period": "60 days (without cause), immediate (for cause)",
                "data_handover": "Client data exported and delivered within 15 business days",
            },
            "termination_assistance": {
                "transition_period": "Up to 90 days post-termination",
                "services": [
                    "Data export in standard formats (JSON, CSV, PDF)",
                    "API access for migration scripts (read-only)",
                    "Technical documentation handover",
                    "Knowledge transfer sessions (2x included, additional billable)",
                ],
            },
            "survival_clauses": [
                "Confidentiality obligations (3 years post-termination)",
                "Limitation of liability",
                "Data protection obligations",
                "Indemnification",
                "Audit trail retention (regulatory minimum)",
            ],
        }

    @staticmethod
    def _regulatory() -> dict:
        return {
            "title": "Regulatory Compliance Commitments",
            "platform_commitments": [
                {
                    "area": "EPFO Investment Pattern Compliance",
                    "commitment": "37+ automated compliance checks against current EPFO guidelines",
                    "update_frequency": "Within 30 days of regulatory notification change",
                },
                {
                    "area": "Audit Trail Completeness",
                    "commitment": "Immutable, tamper-evident audit log of all material operations",
                    "retention": "Minimum 8 years (or as required by applicable regulation)",
                },
                {
                    "area": "Data Security Standards",
                    "commitment": "TLS 1.2+ for all data in transit, access controls, rate limiting",
                    "certification_target": "ISO 27001 within 12 months",
                },
                {
                    "area": "Regulatory Reporting",
                    "commitment": "Automated generation of trustee reports per EPFO format requirements",
                    "format": "PDF with digital signatures (planned)",
                },
            ],
            "client_responsibilities": [
                "Maintaining current Investment Policy Statement (IPS)",
                "Ensuring authorized personnel access only via proper role assignments",
                "Independent verification of data before investment decisions",
                "Timely reporting of security incidents or unauthorized access",
                "Compliance with applicable data protection laws",
            ],
        }

    @staticmethod
    def _confidentiality() -> dict:
        return {
            "title": "Confidentiality & Non-Disclosure",
            "scope": (
                "All data, configurations, reports, and analytical outputs generated by or stored "
                "in the platform constitute Confidential Information of the disclosing party."
            ),
            "obligations": [
                "Use confidential information solely for the purposes of the agreement",
                "Restrict access to personnel with need-to-know basis",
                "Implement reasonable security measures (at minimum, those described in Security Documentation)",
                "Promptly notify the other party of any unauthorized access or disclosure",
                "Return or destroy confidential information upon termination",
            ],
            "exceptions": [
                "Information already in the public domain",
                "Information independently developed without reference to confidential information",
                "Disclosure required by law, regulation, or court order (with prior notice where permitted)",
                "Disclosure to professional advisors under equivalent confidentiality obligations",
            ],
            "duration": "3 years from the date of disclosure or termination of agreement, whichever is later",
        }
