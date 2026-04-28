"""
Application-Layer Encryption (ALE) for PII & Financial Fields
==============================================================
Sensitive data is encrypted BEFORE it reaches the database using AES-256-GCM.
The encryption key is NEVER stored in the DB — it lives only in the process
environment, achieving Separation of Duties between the app and the storage layer.

Key rotation is supported: old ciphertexts can be re-encrypted with the new key
without touching unencrypted fields.

Environment variables:
  ALE_ENCRYPTION_KEY   — 32-byte hex-encoded key (64 hex chars). REQUIRED.
  ALE_KEY_VERSION      — Integer label for the current key (default: 1).
                         Increment when rotating keys.

Usage:
  from .shared.crypto import encrypt_field, decrypt_field, FieldEncryptor

  # Encrypt at write time:
  encrypted = encrypt_field("John Smith")

  # Decrypt at read time:
  plain = decrypt_field(encrypted)

  # Convenience class for bulk encrypt/decrypt:
  enc = FieldEncryptor()
  row = enc.encrypt_dict(row, fields=["client_name", "pan_number"])
  plain_row = enc.decrypt_dict(row, fields=["client_name", "pan_number"])
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any, Optional

# ── Key Bootstrap ─────────────────────────────────────────────────────────────
_WEAK_KEYS = {
    "",
    "CHANGE_ME",
    "0" * 64,
    "a" * 64,
}

_MASTER_KEY: Optional[bytes] = None
_FIELD_KEY_CACHE: Optional[bytes] = None
_INTEGRITY_KEY_CACHE: Optional[bytes] = None


def _get_master_key() -> bytes:
    """
    Lazy-load the ALE master key (called at first encrypt/decrypt use, after .env loads).
    Raises RuntimeError on failure so the request fails fast rather than the process.
    """
    global _MASTER_KEY
    if _MASTER_KEY is not None:
        return _MASTER_KEY
    raw = os.getenv("ALE_ENCRYPTION_KEY", "")
    if raw in _WEAK_KEYS or len(raw) < 64:
        raise RuntimeError(
            "ALE_ENCRYPTION_KEY is not set or is too short (must be 64 hex chars = 32 bytes). "
            "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    try:
        key = bytes.fromhex(raw)
        if len(key) != 32:
            raise ValueError("Key must decode to exactly 32 bytes.")
    except Exception as exc:
        raise RuntimeError(f"ALE_ENCRYPTION_KEY is invalid hex: {exc}") from exc
    _MASTER_KEY = key
    return _MASTER_KEY


def _get_field_key() -> bytes:
    """Lazy-load the derived field encryption key."""
    global _FIELD_KEY_CACHE
    if _FIELD_KEY_CACHE is None:
        _FIELD_KEY_CACHE = _derive_field_key_from("field_encryption_v1")
    return _FIELD_KEY_CACHE


def _get_integrity_key() -> bytes:
    """Lazy-load the derived integrity signing key."""
    global _INTEGRITY_KEY_CACHE
    if _INTEGRITY_KEY_CACHE is None:
        _INTEGRITY_KEY_CACHE = _derive_field_key_from("integrity_signing_v1")
    return _INTEGRITY_KEY_CACHE


def _get_key_version() -> int:
    return int(os.getenv("ALE_KEY_VERSION", "1"))

# ── Encryption (AES-256-GCM) ──────────────────────────────────────────────────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    _CRYPTOGRAPHY_AVAILABLE = False


def _derive_field_key(purpose: str = "field_encryption") -> bytes:
    """
    Derive a sub-key from the master key using HKDF-SHA256.
    Each "purpose" gets a distinct key — prevents cross-context decryption.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        _get_master_key(),
        purpose.encode("utf-8"),
        iterations=100_000,
        dklen=32,
    )


def _derive_field_key_from(purpose: str) -> bytes:
    """Alias used by lazy loaders to avoid recursion."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        _get_master_key(),
        purpose.encode("utf-8"),
        iterations=100_000,
        dklen=32,
    )


def encrypt_field(plaintext: str) -> Optional[str]:
    """
    Encrypt a string field with AES-256-GCM.
    Returns a versioned base64-encoded ciphertext string.
    Prefix format: "v{version}:{b64(nonce+ciphertext+tag)}"

    Returns None if plaintext is None.
    Raises RuntimeError if cryptography is not installed.
    """
    if plaintext is None:
        return None
    if not _CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError(
            "cryptography package not installed. Run: pip install cryptography"
        )
    aesgcm = AESGCM(_get_field_key())
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # Prepend nonce to ciphertext for storage
    blob = nonce + ct
    encoded = base64.b64encode(blob).decode("ascii")
    return f"v{_get_key_version()}:{encoded}"


def decrypt_field(ciphertext: str) -> Optional[str]:
    """
    Decrypt a versioned AES-256-GCM ciphertext produced by encrypt_field().
    Transparently handles legacy key versions (if _FIELD_KEY is updated, old
    data must be re-encrypted using rotate_field_key()).

    Returns None if ciphertext is None.
    """
    if ciphertext is None:
        return None
    if not _CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError(
            "cryptography package not installed. Run: pip install cryptography"
        )
    try:
        prefix, b64_blob = ciphertext.split(":", 1)
    except ValueError:
        raise ValueError(f"Malformed ciphertext — missing version prefix: {ciphertext[:20]!r}")

    blob = base64.b64decode(b64_blob)
    nonce, ct = blob[:12], blob[12:]
    aesgcm = AESGCM(_get_field_key())
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def is_encrypted(value: str) -> bool:
    """Check whether a value looks like an ALE-encrypted field."""
    if not isinstance(value, str):
        return False
    return value.startswith("v") and ":" in value and len(value) > 20


# ── Bulk encrypt/decrypt helpers ──────────────────────────────────────────────
class FieldEncryptor:
    """
    Convenience wrapper for encrypting/decrypting multiple fields in a dict.

    Usage:
        enc = FieldEncryptor()
        record = {"client_name": "Ravi Kumar", "pan_number": "ABCDE1234F", "amount": 500000}
        encrypted = enc.encrypt_dict(record, fields=["client_name", "pan_number"])
        # {"client_name": "v1:...", "pan_number": "v1:...", "amount": 500000}

        plain = enc.decrypt_dict(encrypted, fields=["client_name", "pan_number"])
        # {"client_name": "Ravi Kumar", "pan_number": "ABCDE1234F", "amount": 500000}
    """

    def encrypt_dict(self, record: dict, fields: list[str]) -> dict:
        """Return a copy of record with the specified fields encrypted."""
        result = dict(record)
        for f in fields:
            if f in result and result[f] is not None and not is_encrypted(str(result[f])):
                result[f] = encrypt_field(str(result[f]))
        return result

    def decrypt_dict(self, record: dict, fields: list[str]) -> dict:
        """Return a copy of record with the specified fields decrypted."""
        result = dict(record)
        for f in fields:
            if f in result and result[f] is not None and is_encrypted(str(result[f])):
                result[f] = decrypt_field(str(result[f]))
        return result

    def decrypt_list(self, rows: list[dict], fields: list[str]) -> list[dict]:
        """Decrypt fields across a list of records."""
        return [self.decrypt_dict(row, fields) for row in rows]


# ── HMAC Integrity Check ──────────────────────────────────────────────────────

def sign_record(data: dict) -> str:
    """
    Generate an HMAC-SHA256 signature of a canonical JSON representation of data.
    Used for tamper-evident audit records.
    """
    canonical = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return hmac.new(_get_integrity_key(), canonical, hashlib.sha256).hexdigest()


def verify_record(data: dict, signature: str) -> bool:
    """Verify a record has not been tampered with since it was signed."""
    expected = sign_record(data)
    return hmac.compare_digest(expected, signature)


# ── PII Field Registry ─────────────────────────────────────────────────────────
# Centralised list of fields that must always be encrypted at rest.
# Reference this in services before writing to the DB.
PII_FIELDS = {
    "portfolio": ["client_name", "pan_number", "account_number", "email"],
    "compliance": ["client_name", "risk_notes"],
    "users": ["email"],
    "audit_trail": [],  # audit records are signed, not encrypted (for forensics)
}
