"""
JWT Authentication & Role-Based Access Control (RBAC)
======================================================
Roles (ordered, highest privilege first):
  ADMIN       — full system access, user management
  PORTFOLIO   — portfolio management, pre/post-trade
  COMPLIANCE  — read-only analytics, compliance review
  VIEWER      — read-only market data

Usage (FastAPI dependency injection):
  from .shared.auth import require_role, get_current_user, Roles

  @router.get("/sensitive")
  async def sensitive(user = Depends(require_role(Roles.COMPLIANCE))):
      ...
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
import bcrypt
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, field_validator, model_validator

from .database import execute_ddl, execute_query, fetch_one
from .deployment import mfa_required_for_all_roles
from .logger import get_logger
from .time_utils import utcnow

log = get_logger("auth")

# Import tamper-proof audit logger — deferred to avoid circular imports
def _audit(event: str, level: str = "INFO", **kw):
    try:
        from .secure_logger import audit as _a
        _a(event, level=level, **kw)
    except Exception:
        pass  # Never let audit failure block auth path


async def _audit_auth_event(
    *,
    action_name: str,
    username: str,
    role: str,
    ip: Optional[str] = None,
    user_id: Any = "system",
    description: Optional[str] = None,
    severity_name: str = "INFO",
) -> None:
    """Best-effort auth event mirror into AuditService canonical audit pipeline."""
    try:
        from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType

        action = AuditAction[action_name]
        severity = AuditSeverity[severity_name]
        await AuditService.record(
            user={"id": str(user_id), "username": username, "role": _normalize_role(role)},
            action=action,
            entity_type=EntityType.USER,
            entity_id=str(username),
            old_value=None,
            new_value={"username": username, "role": _normalize_role(role)},
            affected_module="auth",
            ip_address=ip,
            severity=severity,
            compliance_flag=True,
            description=description,
        )
    except Exception as exc:
        log.warning("auth_audit_event_failed", action=action_name, error=str(exc))

# ── Configuration ──────────────────────────────────────────────────────────────
_WEAK_SECRETS = {
    "",
    "CHANGE_ME_use_openssl_rand_hex_32_in_production",
    "changeme",
    "secret",
    "dev",
    "development",
}


def _get_jwt_secret() -> str:
    """
    Lazy-load and validate the JWT secret key.
    Called once at first use (after .env is loaded by main.py lifespan).
    Raises RuntimeError if the key is missing or weak — this propagates as a
    startup failure rather than a silent auto-generation.
    """
    val = os.getenv("JWT_SECRET_KEY", "")
    if val in _WEAK_SECRETS:
        raise RuntimeError(
            "FATAL: JWT_SECRET_KEY is not set or is a known weak/default value.\n"
            "Generate one with:  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "Then set it in your .env file as:  JWT_SECRET_KEY=<value>\n"
            "The gateway will NOT authenticate without a strong secret.\n"
        )
    if len(val) < 32:
        raise RuntimeError(
            f"FATAL: JWT_SECRET_KEY is too short ({len(val)} chars). "
            "Minimum 32 characters required."
        )
    return val


def _get_secret_key() -> str:
    try:
        return _get_jwt_secret()
    except RuntimeError as exc:
        log.error("jwt_secret_invalid", error=str(exc))
        raise


SECRET_KEY        = os.getenv("JWT_SECRET_KEY", "placeholder-replaced-at-first-use")
ALGORITHM         = "HS256"
ACCESS_TOKEN_TTL  = int(os.getenv("JWT_ACCESS_TTL_MINUTES",  "20"))
REFRESH_TOKEN_TTL = int(os.getenv("JWT_REFRESH_TTL_DAYS",    "7"))

# Workaround for bcrypt 4.0.0+ incompatibility with passlib
# We use bcrypt directly for hashing/verification instead of CryptContext
# pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


# ══════════════════════════════════════════════════════════════════════════════
# Token Blacklist (C2 Remediation — Server-Side Forced Logout)
# ══════════════════════════════════════════════════════════════════════════════
import hashlib
from typing import Set

class TokenBlacklist:
    """
    Server-side token blacklist for forced logout / session revocation.
    Uses Redis when available, falls back to in-memory set.
    In-memory fallback clears on restart — Redis is recommended for production.
    """

    _redis_client = None
    _memory_blacklist: Set[str] = set()
    _memory_user_revocations: dict[str, float] = {}
    _initialized = False

    @staticmethod
    def _is_production_environment() -> bool:
        return os.getenv("GATEWAY_ENV", "development").lower() in {"prod", "production"}

    @classmethod
    async def initialize(cls):
        """Try to connect to Redis; fall back to in-memory only in development."""
        if cls._initialized:
            return
        redis_url = os.getenv("REDIS_URL", "")
        redis_required = cls._is_production_environment() or os.getenv("REDIS_REQUIRED", "false").lower() in ("true", "1", "yes")
        if redis_url:
            try:
                import redis.asyncio as aioredis
                cls._redis_client = aioredis.from_url(redis_url, decode_responses=True)
                await cls._redis_client.ping()
                log.info("token_blacklist_redis_connected", url=redis_url.split("@")[-1])
            except Exception as e:
                cls._redis_client = None
                if redis_required:
                    raise RuntimeError(
                        f"FATAL: REDIS_REQUIRED=true but Redis connection failed: {e}\n"
                        "Token blacklist requires Redis in production for session persistence.\n"
                        "Fix Redis connectivity or set REDIS_REQUIRED=false for development."
                    )
                log.warning(
                    "token_blacklist_redis_failed",
                    error=str(e),
                    fallback="memory-only",
                    revocation_persists=False,
                )
        else:
            if redis_required:
                raise RuntimeError(
                    "FATAL: Redis-backed token revocation is required in production but REDIS_URL is not set.\n"
                    "Configure REDIS_URL in .env for production deployment."
                )
            log.info(
                "token_blacklist_mode",
                mode="memory-only",
                revocation_persists=False,
                note="Redis unset; blacklist entries clear on restart",
            )
        cls._initialized = True

    @classmethod
    def _token_hash(cls, token: str) -> str:
        """Hash token to avoid storing raw JWTs."""
        return hashlib.sha256(token.encode()).hexdigest()[:24]

    @staticmethod
    def _normalize_username(username: str) -> str:
        return str(username or "").strip().lower()

    @classmethod
    def _user_revocation_key(cls, username: str) -> str:
        return f"user_revoked:{cls._normalize_username(username)}"

    @classmethod
    async def blacklist_token(cls, token: str, ttl_seconds: int = None):
        """Add a token to the blacklist."""
        token_id = cls._token_hash(token)
        if ttl_seconds is None:
            ttl_seconds = ACCESS_TOKEN_TTL * 60  # default to access token TTL
        if cls._redis_client:
            try:
                await cls._redis_client.setex(f"bl:{token_id}", ttl_seconds, "1")
                return True
            except Exception:
                pass
        cls._memory_blacklist.add(token_id)
        return True

    @classmethod
    async def is_blacklisted(cls, token: str) -> bool:
        """Check if a token has been revoked."""
        token_id = cls._token_hash(token)
        if cls._redis_client:
            try:
                result = await cls._redis_client.get(f"bl:{token_id}")
                return result is not None
            except Exception:
                pass
        return token_id in cls._memory_blacklist

    @classmethod
    async def blacklist_all_user_tokens(cls, username: str):
        """Mark a user as requiring re-authentication (invalidates all sessions)."""
        username_key = cls._normalize_username(username)
        if not username_key:
            return True
        revoked_at = datetime.now(timezone.utc).timestamp()
        key = cls._user_revocation_key(username_key)
        if cls._redis_client:
            try:
                await cls._redis_client.set(key, str(revoked_at))
                return True
            except Exception:
                pass
        # In-memory: store revocation timestamp per user.
        cls._memory_user_revocations[username_key] = revoked_at
        return True

    @classmethod
    async def get_user_revoked_at(cls, username: str) -> Optional[float]:
        username_key = cls._normalize_username(username)
        if not username_key:
            return None
        key = cls._user_revocation_key(username_key)
        if cls._redis_client:
            try:
                result = await cls._redis_client.get(key)
                revoked_at = _coerce_timestamp(result)
                if revoked_at is not None:
                    return revoked_at
            except Exception:
                pass
        if username_key in cls._memory_user_revocations:
            return cls._memory_user_revocations[username_key]
        if key in cls._memory_blacklist:
            return datetime.now(timezone.utc).timestamp()
        return None

    @classmethod
    async def is_user_revoked(cls, username: str, issued_at: Optional[float]) -> bool:
        revoked_at = await cls.get_user_revoked_at(username)
        if revoked_at is None:
            return False
        token_issued_at = _coerce_timestamp(issued_at)
        if token_issued_at is None:
            return True
        return token_issued_at <= revoked_at


# ── Role Hierarchy (Institutional 7-Tier) ──────────────────────────────────────
class Roles:
    SUPER_ADMIN = "SUPER_ADMIN"   # Full system access, audit integrity, user mgmt
    ADMIN       = "ADMIN"         # System admin, user mgmt (excl. Super Admin ops)
    PORTFOLIO   = "PORTFOLIO"     # Portfolio management, trade execution
    TRUSTEE     = "TRUSTEE"       # Trustee oversight, reports, compliance view (no edit)
    COMPLIANCE  = "COMPLIANCE"    # Compliance monitoring, alert resolution
    AUDITOR     = "AUDITOR"       # Read-only access to all data incl. audit trail
    VIEWER      = "VIEWER"        # Read-only market data, portfolio summary
    TRUST_USER = "TRUST_USER"  # Alias -> TRUSTEE
    INVESTMENT_MANAGER = "INVESTMENT_MANAGER"  # Alias -> PORTFOLIO


_ROLE_ALIASES = {
    Roles.TRUST_USER: Roles.TRUSTEE,
    Roles.INVESTMENT_MANAGER: Roles.PORTFOLIO,
}


def _normalize_role(role: str) -> str:
    normalized = str(role or "").strip().upper()
    return _ROLE_ALIASES.get(normalized, normalized)

_ROLE_RANK = {
    Roles.SUPER_ADMIN: 7,
    Roles.ADMIN:       6,
    Roles.PORTFOLIO:   5,
    Roles.TRUSTEE:     4,
    Roles.COMPLIANCE:  3,
    Roles.AUDITOR:     2,
    Roles.VIEWER:      1,
    Roles.TRUST_USER: 4,
    Roles.INVESTMENT_MANAGER: 5,
}

# Permissions matrix per role (for fine-grained access control)
ROLE_PERMISSIONS = {
    Roles.SUPER_ADMIN: {
        "manage_users", "manage_roles", "view_audit", "verify_integrity",
        "manage_compliance", "manage_portfolio", "execute_trades",
        "manage_system_config", "override_valuations", "export_reports",
        "view_all", "manage_alerts", "generate_reports",
    },
    Roles.ADMIN: {
        "manage_users", "view_audit", "manage_compliance",
        "manage_portfolio", "execute_trades", "export_reports",
        "view_all", "manage_alerts", "generate_reports",
    },
    Roles.PORTFOLIO: {
        "manage_portfolio", "execute_trades", "pre_trade_check",
        "post_trade_monitor", "view_analytics", "export_reports",
        "view_market_data", "generate_reports",
    },
    Roles.TRUSTEE: {
        "view_portfolio", "view_compliance", "view_reports",
        "view_analytics", "view_audit_summary", "view_market_data",
    },
    Roles.COMPLIANCE: {
        "view_compliance", "resolve_alerts", "view_audit",
        "view_analytics", "generate_compliance_reports",
        "view_market_data", "view_portfolio",
    },
    Roles.AUDITOR: {
        "view_all", "view_audit", "view_compliance",
        "view_portfolio", "view_analytics", "view_market_data",
    },
    Roles.VIEWER: {
        "view_market_data", "view_portfolio_summary",
    },
    Roles.TRUST_USER: {
        "view_portfolio", "view_compliance", "view_reports",
        "view_analytics", "view_audit_summary", "view_market_data",
    },
    Roles.INVESTMENT_MANAGER: {
        "manage_portfolio", "execute_trades", "pre_trade_check",
        "post_trade_monitor", "view_analytics", "export_reports",
        "view_market_data", "generate_reports",
    },
}


# ── Pydantic schemas ───────────────────────────────────────────────────────────
class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    expires_in:    int              # seconds
    mfa_enrolled:  bool = False     # True if user has TOTP enrolled
    mfa_setup_url: Optional[str] = None  # present when MFA is enabled but not yet enrolled


class TokenData(BaseModel):
    username:  Optional[str] = None
    role:      str = Roles.VIEWER
    issued_at: Optional[float] = None


class UserCreate(BaseModel):
    model_config = {"strict": True}

    username: str
    email:    str
    password: str
    role:     str = Roles.VIEWER

    @field_validator("username")
    @classmethod
    def username_no_special(cls, v: str) -> str:
        if not re.match(r'^[a-zA-Z0-9_\-\.]{3,64}$', v):
            raise ValueError("Username must be 3-64 chars, alphanumeric, underscores, hyphens, or dots only.")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_format(cls, v: str) -> str:
        if not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', v):
            raise ValueError("Invalid email format.")
        return v.strip().lower()

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        normalized = _normalize_role(v)
        allowed = {Roles.SUPER_ADMIN, Roles.ADMIN, Roles.PORTFOLIO,
                   Roles.TRUSTEE, Roles.COMPLIANCE, Roles.AUDITOR, Roles.VIEWER,
                   Roles.TRUST_USER, Roles.INVESTMENT_MANAGER}
        if normalized not in allowed:
            raise ValueError(f"Role must be one of: {', '.join(sorted(allowed))}")
        return normalized


class UserOut(BaseModel):
    id:          int
    username:    str
    email:       str
    role:        str
    is_active:   int
    created_at:  str
    last_login:  Optional[str] = None


# ── Password Policy Enforcement ────────────────────────────────────────────────
import re as _re

_PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))


def _utcnow() -> datetime:
    return utcnow()


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _coerce_timestamp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value).timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

def validate_password_policy(password: str) -> tuple[bool, str]:
    """
    Enforce institutional password policy:
      - Minimum 12 characters (configurable via PASSWORD_MIN_LENGTH)
      - At least one uppercase letter
      - At least one lowercase letter
      - At least one digit
      - At least one special character (!@#$%^&*()_+-=[]{}|;:,.<>?)
      - Must not be a known weak/default password
    Returns (is_valid, error_message).
    """
    WEAK_PASSWORDS = {
        "admin@2026", "admin123", "password", "Password1!", "admin",
        "changeme", "letmein", "welcome1", "qwerty123", "abc123",
        "gw_secure_2026", "compliance",
    }
    if password in WEAK_PASSWORDS:
        return False, "Password is a known weak/default credential. Choose a unique password."
    if len(password) < _PASSWORD_MIN_LENGTH:
        return False, f"Password must be at least {_PASSWORD_MIN_LENGTH} characters."
    if not _re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter."
    if not _re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter."
    if not _re.search(r'\d', password):
        return False, "Password must contain at least one digit."
    if not _re.search(r'[!@#$%^&*()\-_+=\[\]{}|;:,.<>?/\\~`]', password):
        return False, "Password must contain at least one special character."
    return True, ""


# ── Account Lockout (Brute-Force Protection) ──────────────────────────────────
_LOCKOUT_MAX_ATTEMPTS = int(os.getenv("ACCOUNT_LOCKOUT_MAX_ATTEMPTS", "5"))
_LOCKOUT_DURATION_MIN = int(os.getenv("ACCOUNT_LOCKOUT_DURATION_MINUTES", "30"))


class AccountLockout:
    """
    Track failed login attempts and lock accounts after threshold.
    Uses DB-backed tracking for persistence across restarts.
    """
    _DDL_RUN = False

    @classmethod
    async def ensure_table(cls):
        if cls._DDL_RUN:
            return
        await execute_ddl(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                attempt_at TEXT NOT NULL,
                ip_address TEXT,
                success INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await execute_ddl("CREATE INDEX IF NOT EXISTS idx_login_user ON login_attempts(username)")
        cls._DDL_RUN = True

    @classmethod
    async def record_attempt(cls, username: str, success: bool, ip: str = None):
        await cls.ensure_table()
        await execute_query(
            """INSERT INTO login_attempts (username, attempt_at, ip_address, success)
               VALUES (:u, :at, :ip, :s)""",
            {"u": username, "at": _utcnow().isoformat(), "ip": ip, "s": 1 if success else 0},
        )
        if success:
            # Clear failed attempts on successful login
            await execute_query(
                "DELETE FROM login_attempts WHERE username = :u AND success = 0",
                {"u": username},
            )

    @classmethod
    async def is_locked(cls, username: str) -> tuple[bool, int]:
        """Check if account is locked. Returns (is_locked, remaining_minutes)."""
        await cls.ensure_table()
        cutoff = (_utcnow() - timedelta(minutes=_LOCKOUT_DURATION_MIN)).isoformat()
        row = await fetch_one(
            """SELECT COUNT(*) as cnt FROM login_attempts
               WHERE username = :u AND success = 0 AND attempt_at > :cutoff""",
            {"u": username, "cutoff": cutoff},
        )
        fail_count = row["cnt"] if row else 0
        if fail_count >= _LOCKOUT_MAX_ATTEMPTS:
            return True, _LOCKOUT_DURATION_MIN
        return False, 0


# ── Password helpers ───────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def verify_password(plain: str, hashed: str) -> bool:
    try:
        # If hashed is not encoded, bcrypt will handle it but we convert for safety
        hashed_bytes = hashed.encode('utf-8') if isinstance(hashed, str) else hashed
        return bcrypt.checkpw(plain.encode('utf-8'), hashed_bytes)
    except Exception:
        return False


# ── JWT helpers ────────────────────────────────────────────────────────────────
def _create_token(data: dict, expires_delta: timedelta) -> str:
    secret = _get_jwt_secret()
    payload = data.copy()
    now = _utcnow()
    payload["exp"] = now + expires_delta
    payload["iat"] = int(now.timestamp())
    payload["issued_at"] = now.timestamp()
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def create_access_token(username: str, role: str) -> str:
    return _create_token(
        {"sub": username, "role": _normalize_role(role), "type": "access"},
        timedelta(minutes=ACCESS_TOKEN_TTL),
    )


def create_refresh_token(username: str, role: str) -> str:
    return _create_token(
        {"sub": username, "role": _normalize_role(role), "type": "refresh"},
        timedelta(days=REFRESH_TOKEN_TTL),
    )


def decode_token(token: str) -> TokenData:
    try:
        secret = _get_jwt_secret()
    except RuntimeError as exc:
        log.error("jwt_secret_invalid", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is not configured. Contact support.",
        )
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role:     str = _normalize_role(payload.get("role", Roles.VIEWER))
        issued_at = _coerce_timestamp(payload.get("issued_at") or payload.get("iat"))
        if username is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return TokenData(username=username, role=role, issued_at=issued_at)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token validation failed: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def validate_token_session(token: str) -> tuple[TokenData, dict]:
    """Validate a JWT and return the active user record. Raises on revoked or stale tokens."""
    if await TokenBlacklist.is_blacklisted(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    data = decode_token(token)

    if await TokenBlacklist.is_user_revoked(data.username or "", data.issued_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user = await fetch_one(
            "SELECT * FROM users WHERE username = :u AND is_active = 1",
            {"u": data.username},
        )
    except Exception as exc:
        log.warning("auth_db_unavailable", username=data.username, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable.",
        )

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    password_changed_at = _coerce_timestamp(user.get("password_changed_at"))
    if password_changed_at is not None and data.issued_at is not None and data.issued_at <= password_changed_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is no longer valid. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return data, dict(user)


# ── FastAPI Dependencies ───────────────────────────────────────────────────────
async def get_current_user(request: Request, token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    """Validate token, check blacklist, and verify user is still active in DB."""
    if not token:
        path = request.url.path
        if path.startswith("/api/v1/im-checker/uploads/") and path.endswith("/file"):
            token = request.query_params.get("token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    _, user = await validate_token_session(token)
    return user


def require_role(minimum_role: str):
    """
    Dependency factory — only allows users whose role rank >= minimum_role.
    Example: Depends(require_role(Roles.PORTFOLIO))
    """
    async def _checker(user: dict = Depends(get_current_user)) -> dict:
        normalized_user_role = _normalize_role(user.get("role", Roles.VIEWER))
        normalized_required_role = _normalize_role(minimum_role)
        user_rank = _ROLE_RANK.get(normalized_user_role, 0)
        required_rank = _ROLE_RANK.get(normalized_required_role, 0)
        if user_rank < required_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{normalized_required_role}' or higher required. You have '{normalized_user_role}'.",
            )
        user["role"] = normalized_user_role
        return user
    return _checker


# ── Auth Router ────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/auth", tags=["Auth & IAM"])


@router.post("/token", response_model=TokenResponse)
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    """Authenticate and receive JWT access + refresh tokens. Requires MFA if enabled."""
    client_ip = request.client.host if request.client else None

    # ── Account Lockout Check ─────────────────────────────────────────
    locked, remaining = await AccountLockout.is_locked(form.username)
    if locked:
        log.warning("account_locked", username=form.username, remaining_min=remaining)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Account locked due to {_LOCKOUT_MAX_ATTEMPTS} failed attempts. "
                   f"Try again in {remaining} minutes.",
        )

    user = await fetch_one(
        "SELECT * FROM users WHERE username = :u AND is_active = 1",
        {"u": form.username},
    )

    # Extract actual password (before MFA code if appended)
    actual_password = form.password
    totp_code_from_password = None
    if ":" in form.password:
        parts = form.password.rsplit(":", 1)
        suffix = parts[1].strip()
        looks_like_totp = len(suffix) == 6 and suffix.isdigit()
        looks_like_backup = suffix.startswith("BACKUP-") or (
            len(suffix) == MFABackupCodes.CODE_LENGTH and not suffix.isdigit()
        )
        if looks_like_totp or looks_like_backup:
            actual_password = parts[0]
            totp_code_from_password = suffix

    if not user or not verify_password(actual_password, user["hashed_password"]):
        log.warning("login_failed", username=form.username)
        await AccountLockout.record_attempt(form.username, False)
        _audit("login_failed", level="WARNING", actor=form.username)
        await _audit_auth_event(
            action_name="USER_LOGIN_FAILED",
            username=form.username,
            role=Roles.VIEWER,
            ip=client_ip,
            user_id="unknown",
            description=f"Login failed for {form.username}",
            severity_name="WARNING",
        )
        # Tarpit: constant-time delay prevents timing-based username enumeration
        await asyncio.sleep(1.5)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect credentials")

    # ── MFA Check — MANDATORY for privileged roles ────────────────────────
    # MFA_ENABLED=true activates the system.
    # Privileged roles (ADMIN, SUPER_ADMIN, PORTFOLIO, COMPLIANCE) MUST
    # have MFA enrolled. If not enrolled, login is blocked with instructions.
    # Lower roles (VIEWER, AUDITOR) are prompted but not forced.
    mfa_enabled = os.getenv("MFA_ENABLED", "false").lower() == "true"
    user_role = _normalize_role(user.get("role", Roles.VIEWER))
    if mfa_required_for_all_roles():
        _MFA_MANDATORY_ROLES = {
            Roles.SUPER_ADMIN,
            Roles.ADMIN,
            Roles.PORTFOLIO,
            Roles.TRUSTEE,
            Roles.COMPLIANCE,
            Roles.AUDITOR,
            Roles.VIEWER,
        }
    else:
        _MFA_MANDATORY_ROLES = {Roles.SUPER_ADMIN, Roles.ADMIN, Roles.PORTFOLIO, Roles.COMPLIANCE, Roles.TRUSTEE}
    has_mfa_secret = bool(user.get("mfa_secret"))
    requires_mfa = mfa_enabled and has_mfa_secret
    mfa_mandatory_for_role = mfa_enabled and user_role in _MFA_MANDATORY_ROLES

    # Block login for privileged roles that haven't enrolled MFA
    if mfa_mandatory_for_role and not has_mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"MFA enrollment is MANDATORY for role '{user_role}'. "
                "Contact an administrator to set up MFA via POST /api/v1/auth/mfa/setup "
                "before you can log in."
            ),
        )

    if requires_mfa:

        totp_code = totp_code_from_password
        if not totp_code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA required. Append TOTP code to password as 'password:123456' or a backup code as 'password:BACKUP-XXXXXXXXXXXX'.",
            )

        # Try TOTP first (standard auth path)
        is_backup_code = totp_code.startswith("BACKUP-") or (len(totp_code) == MFABackupCodes.CODE_LENGTH and not totp_code.isdigit())
        mfa_verified = False

        if is_backup_code:
            # Backup code path — single-use recovery code
            clean_code = totp_code.removeprefix("BACKUP-")
            mfa_verified = await MFABackupCodes.verify_and_consume(form.username, clean_code)
            if not mfa_verified:
                log.warning("mfa_backup_code_invalid", username=form.username)
                await AccountLockout.record_attempt(form.username, False)
                _audit("mfa_backup_code_invalid", level="WARNING", actor=form.username)
                await asyncio.sleep(2.5)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid backup code.",
                )
        else:
            # TOTP path with anti-replay protection (RFC 4226 §7.2)
            mfa_verified = await MFAService.verify_totp_with_antireplay(
                user["mfa_secret"], totp_code, form.username
            )
            if not mfa_verified:
                log.warning("mfa_failed", username=form.username)
                await AccountLockout.record_attempt(form.username, False)
                _audit("mfa_failed", level="WARNING", actor=form.username)
                # Tarpit: artificial delay to mitigate TOTP brute-force (RFC 4226 §7.4)
                await asyncio.sleep(2.5)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid MFA code.",
                )

    # ── Successful login ──────────────────────────────────────────────
    await AccountLockout.record_attempt(form.username, True)

    # Check if forced password change is needed
    force_change = os.getenv("FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "false").lower() == "true"
    must_change = force_change and user.get("password_changed_at") is None

    await execute_query(
        "UPDATE users SET last_login = :now WHERE username = :u",
        {"now": _utcnow().isoformat(), "u": form.username},
    )
    access  = create_access_token(user["username"], user["role"])
    refresh = create_refresh_token(user["username"], user["role"])
    log.info("login_success", username=user["username"], role=user["role"])
    _audit("login_success", actor=user["username"], role=user["role"])
    await _audit_auth_event(
        action_name="USER_LOGIN",
        username=user["username"],
        role=user.get("role", Roles.VIEWER),
        ip=client_ip,
        user_id=user.get("id", "unknown"),
        description=f"User {user['username']} logged in",
        severity_name="INFO",
    )

    mfa_enrolled = bool(user.get("mfa_secret"))
    mfa_system_enabled = os.getenv("MFA_ENABLED", "false").lower() == "true"

    response = TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=ACCESS_TOKEN_TTL * 60,
    )
    base = {
        **response.model_dump(),
        "mfa_enrolled": mfa_enrolled,
        "mfa_setup_url": "/api/v1/auth/mfa/setup" if mfa_system_enabled and not mfa_enrolled else None,
    }
    if must_change:
        return {**base, "password_change_required": True,
                "message": "You must change your password before proceeding."}
    return base


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(token: str):
    """Exchange a refresh token for a new access token."""
    _, user = await validate_token_session(token)
    access  = create_access_token(user["username"], user["role"])
    refresh = create_refresh_token(user["username"], user["role"])
    return TokenResponse(access_token=access, refresh_token=refresh, expires_in=ACCESS_TOKEN_TTL * 60)


@router.post("/register", response_model=UserOut)
async def register_user(
    payload: UserCreate,
    _admin: dict = Depends(require_role(Roles.ADMIN)),
):
    """Create a new user. Requires ADMIN role. Password must meet complexity policy."""
    # Enforce password complexity
    valid, error_msg = validate_password_policy(payload.password)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Password policy violation: {error_msg}")

    existing = await fetch_one("SELECT id FROM users WHERE username = :u OR email = :e",
                               {"u": payload.username, "e": payload.email})
    if existing:
        raise HTTPException(status_code=409, detail="Username or email already exists")

    await execute_query(
        """INSERT INTO users (username, email, hashed_password, role, is_active, created_at, password_changed_at)
           VALUES (:u, :e, :hp, :r, 1, :now, NULL)""",
        {"u": payload.username, "e": payload.email, "hp": hash_password(payload.password),
            "r": payload.role.upper(), "now": _utcnow().isoformat()},
    )
    user = await fetch_one("SELECT * FROM users WHERE username = :u", {"u": payload.username})
    log.info("user_created", username=payload.username, role=payload.role)
    return UserOut(**dict(user))


@router.get("/me", response_model=UserOut)
async def get_me(user: dict = Depends(get_current_user)):
    """Return the currently-authenticated user's profile."""
    return UserOut(**user)


@router.get("/users", response_model=list[UserOut])
async def list_users(_admin: dict = Depends(require_role(Roles.ADMIN))):
    """List all users. ADMIN only."""
    from .database import fetch_all
    rows = await fetch_all("SELECT * FROM users ORDER BY created_at DESC")
    return [UserOut(**dict(r)) for r in rows]


@router.post("/logout")
async def logout(
    request: Request,
    token: str = Depends(oauth2_scheme),
    user: dict = Depends(get_current_user),
):
    """Revoke the current access token (server-side blacklist)."""
    await TokenBlacklist.blacklist_token(token)
    log.info("user_logout", username=user.get("username"))
    _audit("user_logout", actor=user.get("username"))
    await _audit_auth_event(
        action_name="USER_LOGOUT",
        username=user.get("username", "unknown"),
        role=user.get("role", Roles.VIEWER),
        ip=request.client.host if request.client else None,
        user_id=user.get("id", "unknown"),
        description=f"User {user.get('username', 'unknown')} logged out",
        severity_name="INFO",
    )
    return {"status": "success", "message": "Token revoked. Please discard the token."}


@router.post("/logout-all")
async def logout_all_sessions(request: Request, user: dict = Depends(get_current_user)):
    """Revoke ALL tokens for the current user (force re-authentication everywhere)."""
    await TokenBlacklist.blacklist_all_user_tokens(user.get("username"))
    log.info("user_logout_all_sessions", username=user.get("username"))
    _audit("logout_all_sessions", level="WARNING", actor=user.get("username"))
    await _audit_auth_event(
        action_name="USER_LOGOUT_ALL",
        username=user.get("username", "unknown"),
        role=user.get("role", Roles.VIEWER),
        ip=request.client.host if request.client else None,
        user_id=user.get("id", "unknown"),
        description=f"User {user.get('username', 'unknown')} revoked all sessions",
        severity_name="WARNING",
    )
    return {"status": "success", "message": "All sessions revoked. Re-authentication required."}


# ══════════════════════════════════════════════════════════════════════════════
# MFA / TOTP (M4 Remediation — Multi-Factor Authentication)
# ══════════════════════════════════════════════════════════════════════════════

class TOTPAntiReplay:
    """
    Prevent TOTP code replay attacks by tracking the last-used code per user.
    RFC 4226 §7.2 mandates that each OTP is used only once.
    Storage: Redis (preferred) or fallback in-process dict (NOT safe across workers).
    """
    _cache: dict[str, str] = {}  # username → last_used_code

    @classmethod
    async def _redis_client(cls):
        """Get Redis client if available."""
        redis_url = os.getenv("REDIS_URL", "")
        if not redis_url:
            return None
        try:
            import redis.asyncio as aioredis
            client = aioredis.from_url(redis_url, decode_responses=True)
            await client.ping()
            return client
        except Exception:
            return None

    @classmethod
    async def is_replay(cls, username: str, code: str) -> bool:
        """Return True if this code was already used in the current 30-second window."""
        key = f"totp_last:{username}"
        redis = await cls._redis_client()
        if redis:
            last = await redis.get(key)
            return last == code
        return cls._cache.get(username) == code

    @classmethod
    async def mark_used(cls, username: str, code: str) -> None:
        """Mark this code as used. TTL = 90s (3 TOTP windows for clock drift)."""
        key = f"totp_last:{username}"
        redis = await cls._redis_client()
        if redis:
            await redis.setex(key, 90, code)
        else:
            cls._cache[username] = code


class MFABackupCodes:
    """
    One-time backup codes for MFA recovery (NIST SP 800-63B §5.1.2).
    10 codes, 12 chars each, stored as bcrypt hashes — never plaintext.
    Single-use: each code is invalidated after use.
    """
    NUM_CODES = 10
    CODE_LENGTH = 12
    _DDL_RUN = False

    @classmethod
    async def ensure_table(cls):
        if cls._DDL_RUN:
            return
        await execute_ddl(
            """
            CREATE TABLE IF NOT EXISTS mfa_backup_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                used_at TEXT
            )
            """
        )
        await execute_ddl("CREATE INDEX IF NOT EXISTS idx_mfa_backup_username ON mfa_backup_codes(username)")
        await execute_ddl("CREATE INDEX IF NOT EXISTS idx_mfa_backup_used ON mfa_backup_codes(username, used)")
        cls._DDL_RUN = True

    @classmethod
    def _generate_codes(cls) -> list[str]:
        """Generate NUM_CODES random alphanumeric backup codes."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # Unambiguous chars
        return [
            "".join(secrets.choice(alphabet) for _ in range(cls.CODE_LENGTH))
            for _ in range(cls.NUM_CODES)
        ]

    @classmethod
    async def generate_and_store(cls, username: str) -> list[str]:
        """Generate fresh backup codes for a user. Invalidates old codes."""
        await cls.ensure_table()
        # Revoke all existing codes
        await execute_query(
            "DELETE FROM mfa_backup_codes WHERE username = :u",
            {"u": username},
        )
        codes = cls._generate_codes()
        for code in codes:
            code_hash = hash_password(code)  # bcrypt — each hash is unique
            await execute_query(
                "INSERT INTO mfa_backup_codes (username, code_hash, created_at) "
                "VALUES (:u, :ch, :now)",
                {"u": username, "ch": code_hash, "now": _utcnow().isoformat()},
            )
        log.info("mfa_backup_codes_generated", username=username, count=len(codes))
        return codes  # Return plaintext once — never stored

    @classmethod
    async def verify_and_consume(cls, username: str, code: str) -> bool:
        """Verify a backup code. Consumes it on success (single-use)."""
        await cls.ensure_table()
        from .database import fetch_all
        rows = await fetch_all(
            "SELECT id, code_hash FROM mfa_backup_codes WHERE username = :u AND used = 0",
            {"u": username},
        )
        for row in rows:
            if verify_password(code, row["code_hash"]):
                await execute_query(
                    "UPDATE mfa_backup_codes SET used = 1, used_at = :now WHERE id = :id",
                    {"id": row["id"], "now": _utcnow().isoformat()},
                )
                log.warning("mfa_backup_code_used", username=username)
                _audit("mfa_backup_code_used", level="WARNING", actor=username)
                return True
        return False

    @classmethod
    async def remaining_count(cls, username: str) -> int:
        """Count remaining unused backup codes."""
        await cls.ensure_table()
        row = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM mfa_backup_codes WHERE username = :u AND used = 0",
            {"u": username},
        )
        return row["cnt"] if row else 0


class MFAService:
    """TOTP-based MFA using pyotp (RFC 6238 / RFC 4226)."""

    @staticmethod
    def generate_secret() -> str:
        """Generate a new TOTP secret for a user."""
        try:
            import pyotp
            return pyotp.random_base32()
        except ImportError:
            import base64
            return base64.b32encode(os.urandom(20)).decode("utf-8")

    @staticmethod
    def get_provisioning_uri(secret: str, username: str) -> str:
        """Generate QR-code-compatible provisioning URI."""
        issuer = os.getenv("MFA_ISSUER", "PFTrust-BondPlatform")
        try:
            import pyotp
            totp = pyotp.TOTP(secret)
            return totp.provisioning_uri(name=username, issuer_name=issuer)
        except ImportError:
            return f"otpauth://totp/{issuer}:{username}?secret={secret}&issuer={issuer}"

    @staticmethod
    async def verify_totp_with_antireplay(secret: str, code: str, username: str) -> bool:
        """
        Verify a TOTP code against the secret with anti-replay protection.
        RFC 4226 §7.2: each OTP MUST be used at most once.
        Returns False if code is structurally invalid, cryptographically wrong,
        or has already been used in the current time window.
        """
        # Structural validation first
        if not code or not code.isdigit() or len(code) != 6:
            return False

        # Anti-replay: reject codes already used this window
        if await TOTPAntiReplay.is_replay(username, code):
            log.warning("totp_replay_blocked", username=username)
            _audit("totp_replay_blocked", level="WARNING", actor=username)
            return False

        try:
            import pyotp
            totp = pyotp.TOTP(secret)
            # valid_window=1 allows ±30s for clock skew (RFC 6238 recommendation)
            valid = totp.verify(code, valid_window=1)
        except ImportError:
            log.warning("pyotp_not_installed", hint="pip install pyotp")
            return False

        if valid:
            await TOTPAntiReplay.mark_used(username, code)
        return valid

    @staticmethod
    def verify_totp(secret: str, code: str) -> bool:
        """
        Synchronous TOTP verification (for MFA confirm during setup).
        Anti-replay NOT applied here — use verify_totp_with_antireplay for login.
        """
        try:
            import pyotp
            totp = pyotp.TOTP(secret)
            return totp.verify(code, valid_window=1)
        except ImportError:
            log.warning("pyotp_not_installed", hint="pip install pyotp")
            return False


class MFASetupRequest(BaseModel):
    model_config = {"strict": True}
    totp_code: str  # 6-digit code to confirm setup

    @field_validator("totp_code")
    @classmethod
    def totp_must_be_six_digits(cls, v: str) -> str:
        if not re.match(r'^\d{6}$', v):
            raise ValueError("TOTP code must be exactly 6 digits.")
        return v


@router.post("/mfa/setup")
async def setup_mfa(user: dict = Depends(get_current_user)):
    """Generate a new MFA secret for the current user. Returns provisioning URI for QR code."""
    secret = MFAService.generate_secret()
    uri = MFAService.get_provisioning_uri(secret, user.get("username", ""))

    # Store secret temporarily (user must confirm with a valid TOTP code)
    await execute_query(
        "UPDATE users SET mfa_secret_pending = :secret WHERE username = :u",
        {"secret": secret, "u": user.get("username")},
    )

    return {
        "secret": secret,
        "provisioning_uri": uri,
        "instructions": "Scan the QR code with your authenticator app, then confirm with POST /api/v1/auth/mfa/confirm",
    }


@router.post("/mfa/confirm")
async def confirm_mfa(req: MFASetupRequest, user: dict = Depends(get_current_user)):
    """Confirm MFA setup by providing a valid TOTP code from the authenticator app."""
    # Get pending secret
    db_user = await fetch_one(
        "SELECT mfa_secret_pending FROM users WHERE username = :u",
        {"u": user.get("username")},
    )
    pending_secret = db_user.get("mfa_secret_pending") if db_user else None
    if not pending_secret:
        raise HTTPException(400, "No pending MFA setup. Call POST /api/v1/auth/mfa/setup first.")

    if not MFAService.verify_totp(pending_secret, req.totp_code):
        raise HTTPException(400, "Invalid TOTP code. Check your authenticator app and try again.")

    # Activate MFA
    await execute_query(
        "UPDATE users SET mfa_secret = :secret, mfa_secret_pending = NULL WHERE username = :u",
        {"secret": pending_secret, "u": user.get("username")},
    )
    # Generate backup codes (NIST SP 800-63B §5.1.2 — single-use recovery codes)
    backup_codes = await MFABackupCodes.generate_and_store(user.get("username"))
    log.info("mfa_enabled", username=user.get("username"))
    _audit("mfa_enrolled", actor=user.get("username"))
    return {
        "status": "success",
        "message": "MFA enabled successfully.",
        "backup_codes": backup_codes,
        "backup_code_instructions": (
            "SAVE THESE CODES IN A SECURE LOCATION. Each code can only be used once. "
            f"You have {len(backup_codes)} recovery codes. "
            "Use them if you lose access to your authenticator app by appending "
            "BACKUP-<code> to your password during login."
        ),
    }


@router.post("/mfa/regenerate-backup-codes")
async def regenerate_backup_codes(user: dict = Depends(get_current_user)):
    """Regenerate MFA backup codes. Invalidates all existing backup codes."""
    db_user = await fetch_one("SELECT mfa_secret FROM users WHERE username = :u",
                              {"u": user.get("username")})
    if not db_user or not db_user.get("mfa_secret"):
        raise HTTPException(400, "MFA is not enabled. Enroll MFA first.")
    backup_codes = await MFABackupCodes.generate_and_store(user.get("username"))
    _audit("mfa_backup_codes_regenerated", level="WARNING", actor=user.get("username"))
    return {
        "status": "success",
        "backup_codes": backup_codes,
        "message": "Old backup codes have been invalidated. Store these new codes securely.",
    }


@router.get("/mfa/backup-codes/status")
async def backup_codes_status(user: dict = Depends(get_current_user)):
    """Check how many backup codes remain unused."""
    remaining = await MFABackupCodes.remaining_count(user.get("username"))
    return {
        "username": user.get("username"),
        "remaining_backup_codes": remaining,
        "warning": "Low backup codes. Regenerate soon." if remaining <= 2 else None,
    }


@router.delete("/mfa/disable")
async def disable_mfa(user: dict = Depends(require_role(Roles.ADMIN))):
    """Disable MFA for a user. ADMIN only."""
    username = user.get("username")
    await execute_query(
        "UPDATE users SET mfa_secret = NULL, mfa_secret_pending = NULL WHERE username = :u",
        {"u": username},
    )
    log.info("mfa_disabled", username=username)
    return {"status": "success", "message": f"MFA disabled for {username}."}


class PasswordChangeRequest(BaseModel):
    model_config = {"strict": True}
    current_password: str
    new_password: str

    @model_validator(mode="after")
    def passwords_must_differ(self) -> "PasswordChangeRequest":
        if self.current_password == self.new_password:
            raise ValueError("New password must differ from current password.")
        return self


@router.post("/change-password")
async def change_password(req: PasswordChangeRequest, user: dict = Depends(get_current_user)):
    """Change the current user's password. Enforces password policy."""
    if not verify_password(req.current_password, user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    valid, error_msg = validate_password_policy(req.new_password)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Password policy violation: {error_msg}")

    await execute_query(
        "UPDATE users SET hashed_password = :hp, password_changed_at = :now WHERE username = :u",
        {"hp": hash_password(req.new_password), "now": _utcnow().isoformat(), "u": user["username"]},
    )
    # Revoke all existing sessions
    await TokenBlacklist.blacklist_all_user_tokens(user["username"])
    log.info("password_changed", username=user["username"])
    _audit("password_changed", level="WARNING", actor=user["username"])
    return {"status": "success", "message": "Password changed. All sessions revoked — please re-authenticate."}


# ── Password Reset (Self-Service, Token-Based) ────────────────────────────────
_RESET_TOKEN_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "15"))


class PasswordResetTokenManager:
    """
    Manages secure, time-limited password reset tokens.
    Tokens are stored as SHA-256 hashes (never plaintext) in the DB.
    """
    _DDL_RUN = False

    @classmethod
    async def ensure_table(cls):
        if cls._DDL_RUN:
            return
        await execute_ddl(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                used_at TEXT,
                ip_address TEXT
            )
            """
        )
        await execute_ddl("CREATE INDEX IF NOT EXISTS idx_prt_username ON password_reset_tokens(username)")
        await execute_ddl("CREATE INDEX IF NOT EXISTS idx_prt_token_hash ON password_reset_tokens(token_hash)")
        await execute_ddl("CREATE INDEX IF NOT EXISTS idx_prt_used ON password_reset_tokens(username, used)")
        cls._DDL_RUN = True

    @classmethod
    async def create_token(cls, username: str, ip: str = None) -> str:
        """Generate a secure reset token. Returns the raw token (sent to user)."""
        await cls.ensure_table()
        # Invalidate any existing unused tokens for this user
        await execute_query(
            "UPDATE password_reset_tokens SET used = 1, used_at = :now WHERE username = :u AND used = 0",
            {"u": username, "now": _utcnow().isoformat()},
        )
        raw_token = secrets.token_urlsafe(48)  # 64-char URL-safe token
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _utcnow()
        expires = now + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES)
        await execute_query(
            """INSERT INTO password_reset_tokens
               (username, token_hash, created_at, expires_at, ip_address)
               VALUES (:u, :th, :ca, :ea, :ip)""",
            {
                "u": username,
                "th": token_hash,
                "ca": now.isoformat(),
                "ea": expires.isoformat(),
                "ip": ip,
            },
        )
        log.info("password_reset_token_created", username=username)
        return raw_token

    @classmethod
    async def validate_and_consume(cls, raw_token: str) -> Optional[str]:
        """
        Validate a reset token. Returns username if valid, None otherwise.
        Token is consumed (marked used) on success to prevent replay.
        """
        await cls.ensure_table()
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        row = await fetch_one(
            """SELECT id, username, expires_at, used
               FROM password_reset_tokens
               WHERE token_hash = :th""",
            {"th": token_hash},
        )
        if not row:
            return None
        if row["used"]:
            return None
        if _ensure_utc(datetime.fromisoformat(row["expires_at"])) < _utcnow():
            return None

        # Consume the token
        await execute_query(
            "UPDATE password_reset_tokens SET used = 1, used_at = :now WHERE id = :id",
            {"id": row["id"], "now": _utcnow().isoformat()},
        )
        return row["username"]


class PasswordResetRequest(BaseModel):
    model_config = {"strict": True}
    username: str


class PasswordResetConfirm(BaseModel):
    model_config = {"strict": True}
    token: str
    new_password: str


@router.post("/password-reset-request")
async def request_password_reset(req: PasswordResetRequest, request: Request):
    """
    Request a password reset token.
    In production, the token would be sent via a secure channel (email/SMS).
    For security, always returns 200 regardless of whether the user exists
    to prevent username enumeration.
    """
    ip = request.client.host if request.client else None
    user = await fetch_one(
        "SELECT username FROM users WHERE username = :u",
        {"u": req.username},
    )
    if user:
        token = await PasswordResetTokenManager.create_token(req.username, ip=ip)
        # In production: send token via email/SMS. NEVER log the raw token.
        # Here we log that a reset was requested (not the token itself).
        _audit("password_reset_requested", actor=req.username, ip=ip)
        log.info("password_reset_requested", username=req.username,
                 note="Token generated. Deliver via secure channel (email/SMS).")
        # Return token in response ONLY for initial deployment/testing.
        # TODO: Remove token from response body before public launch; deliver via email.
        return {
            "status": "success",
            "message": "If the account exists, a password reset token has been generated.",
            "reset_token": token,
            "expires_in_minutes": _RESET_TOKEN_TTL_MINUTES,
        }
    # Same response shape to prevent enumeration
    _audit("password_reset_unknown_user", actor=req.username, ip=ip)
    return {
        "status": "success",
        "message": "If the account exists, a password reset token has been generated.",
    }


@router.post("/password-reset-confirm")
async def confirm_password_reset(req: PasswordResetConfirm, request: Request):
    """
    Confirm a password reset using a valid token.
    Sets the new password and revokes all existing sessions.
    """
    ip = request.client.host if request.client else None
    username = await PasswordResetTokenManager.validate_and_consume(req.token)
    if not username:
        _audit("password_reset_invalid_token", ip=ip)
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset token. Please request a new one.",
        )

    valid, error_msg = validate_password_policy(req.new_password)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Password policy violation: {error_msg}")

    await execute_query(
        "UPDATE users SET hashed_password = :hp, password_changed_at = :now WHERE username = :u",
        {"hp": hash_password(req.new_password), "now": _utcnow().isoformat(), "u": username},
    )
    await TokenBlacklist.blacklist_all_user_tokens(username)
    log.info("password_reset_completed", username=username)
    _audit("password_reset_completed", level="WARNING", actor=username, ip=ip)
    return {
        "status": "success",
        "message": "Password has been reset. All sessions revoked — please log in with your new password.",
    }

