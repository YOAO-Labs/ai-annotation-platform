"""Domain-separated token, receipt and mail crypto for invitation commands."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

from cryptography.fernet import Fernet

from .invitation_contract import INVITATION_DERIVATION, InvitationRequest
from .recovery_crypto import (
    decrypt_mail_token,
    mail_encryption_id,
    mail_key_from_secret,
    token_hash,
)


def _master(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise ValueError("Invitation secret must be non-empty text")
    return hmac.new(
        secret.encode("utf-8"), INVITATION_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()


def invitation_request_fingerprint(
    secret: str, request: InvitationRequest
) -> tuple[str, str]:
    """Bind an operation receipt to the complete request payload, including proofs."""
    master = _master(secret)
    canonical = json.dumps(
        {
            "operation": request.operation,
            "payload": request.payload.model_dump(mode="json", by_alias=True),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    fingerprint = hmac.new(master, canonical, hashlib.sha256).hexdigest()
    return fingerprint, hashlib.sha256(master).hexdigest()


def invitation_token(
    secret: str,
    operation_id: uuid.UUID,
    *,
    operation: str,
    item_index: int | None = None,
) -> str:
    """Derive a 43-character URL-safe token without a second plaintext receipt copy."""
    if item_index is not None and (type(item_index) is not int or item_index < 0):
        raise ValueError("Invitation item index must be a non-negative integer")
    identity = {
        "operation": operation,
        "operation_id": str(operation_id),
        "item_index": item_index,
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    raw = hmac.new(_master(secret), b"token\0" + canonical, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def invitation_id(
    secret: str,
    operation_id: uuid.UUID,
    *,
    item_index: int,
) -> uuid.UUID:
    """Derive stable per-item ids for preview/apply/replay without storing tokens."""
    if type(item_index) is not int or item_index < 0:
        raise ValueError("Invitation item index must be a non-negative integer")
    message = f"invitation-id:{operation_id}:{item_index}".encode("ascii")
    raw = bytearray(hmac.new(_master(secret), message, hashlib.sha256).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x50
    raw[8] = (raw[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(raw))


def encrypt_mail_token(token: str, key: str) -> tuple[str, str, str]:
    if not isinstance(token, str) or not 1 <= len(token) <= 64:
        raise ValueError("Invitation token must contain 1..64 characters")
    if not isinstance(key, str):
        raise TypeError("Invitation mail key must be text")
    ciphertext = (
        Fernet(key.encode("ascii")).encrypt(token.encode("utf-8")).decode("ascii")
    )
    return token_hash(token), ciphertext, mail_encryption_id(key)


__all__ = [
    "decrypt_mail_token",
    "encrypt_mail_token",
    "invitation_id",
    "invitation_request_fingerprint",
    "invitation_token",
    "mail_encryption_id",
    "mail_key_from_secret",
    "token_hash",
]
