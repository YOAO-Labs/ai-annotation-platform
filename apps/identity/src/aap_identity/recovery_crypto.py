"""Portable hashing and authenticated encryption for Identity recovery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken

RECOVERY_DERIVATION = "aap-identity-recovery/v1"
RECOVERY_MAIL_DERIVATION = "aap-identity-recovery-mail/v1"


def token_hash(token: str) -> str:
    """Return a stable digest for a one-time token without retaining its value."""
    if not isinstance(token, str):
        raise TypeError("Recovery token must be text")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _master(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise ValueError("Recovery secret must be non-empty text")
    return hmac.new(
        secret.encode("utf-8"), RECOVERY_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()


def request_fingerprint(secret: str, request) -> tuple[str, str]:
    """Return the request HMAC and derivation epoch for one closed request."""
    master = _master(secret)
    payload = request.payload.model_dump(mode="json", by_alias=True)
    canonical = json.dumps(
        {"operation": request.operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    fingerprint = hmac.new(master, canonical, hashlib.sha256).hexdigest()
    return fingerprint, hashlib.sha256(master).hexdigest()


def mail_key_from_secret(secret: str) -> str:
    """Derive the embedded fallback Fernet key in a separate key domain."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("Recovery secret must be non-empty text")
    key = hmac.new(
        secret.encode("utf-8"), RECOVERY_MAIL_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(key).decode("ascii")


def mail_encryption_id(key: str) -> str:
    """Identify a Fernet key without exposing it to the database or logs."""
    if not isinstance(key, str):
        raise TypeError("Mail key must be text")
    try:
        decoded = base64.urlsafe_b64decode(key.encode("ascii"))
    except (UnicodeError, ValueError):
        raise ValueError("Invalid mail encryption key") from None
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode("ascii") != key:
        raise ValueError("Invalid mail encryption key")
    return hashlib.sha256(decoded).hexdigest()


def decrypt_mail_token(ciphertext: str, key: str, encryption_id: str) -> str:
    """Decrypt one outbox token only when its key epoch matches the ciphertext."""
    if not isinstance(ciphertext, str) or not isinstance(encryption_id, str):
        raise ValueError("Invalid encrypted recovery token")
    if not hmac.compare_digest(mail_encryption_id(key), encryption_id):
        raise ValueError("Recovery mail key epoch mismatch")
    try:
        value = Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii"))
        token = value.decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise ValueError("Invalid encrypted recovery token") from None
    return token
