"""Short-lived encrypted envelope for backend low-resolution Mask logits."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from yoao_protocol_v2 import MAX_LOW_RES_MASK_INPUT_CHARS, decode_low_res_mask
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

MASK_SESSION_TTL_SECONDS = 5 * 60
MAX_MASK_SESSION_TOKEN_CHARS = 1024 * 1024
_SESSION_VERSION = 1
_MAX_CLOCK_SKEW_SECONDS = 30
_RESERVED_CLAIMS = {"v", "iat", "exp", "raw"}


class AiMaskSessionError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _session_key() -> bytes:
    return hashlib.sha256(
        f"aap:ai-mask-session:v{_SESSION_VERSION}:{settings.secret_key}".encode()
    ).digest()


def _session_cipher() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_session_key()))


def issue_ai_mask_session(
    raw: str,
    claims: dict[str, Any],
    *,
    now: int | None = None,
) -> str:
    if _RESERVED_CLAIMS.intersection(claims):
        raise ValueError("Mask session claims cannot override reserved fields")
    if not isinstance(raw, str) or len(raw) > MAX_LOW_RES_MASK_INPUT_CHARS:
        raise ValueError("backend mask_input exceeds the encoded byte budget")
    decode_low_res_mask(raw)
    issued_at = int(time.time() if now is None else now)
    payload = {
        "v": _SESSION_VERSION,
        "iat": issued_at,
        "exp": issued_at + MASK_SESSION_TTL_SECONDS,
        "raw": raw,
        **claims,
    }
    token = (
        _session_cipher()
        .encrypt_at_time(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            current_time=issued_at,
        )
        .decode()
    )
    if len(token) > MAX_MASK_SESSION_TOKEN_CHARS:
        raise ValueError("Mask session token exceeds the byte budget")
    return token


def verify_ai_mask_session(token: str, *, now: int | None = None) -> dict[str, Any]:
    if not isinstance(token, str) or len(token) > MAX_MASK_SESSION_TOKEN_CHARS:
        raise AiMaskSessionError("invalid_mask_session", "Mask session is invalid")
    current = int(time.time() if now is None else now)
    try:
        payload = json.loads(
            _session_cipher().decrypt_at_time(
                token.encode(),
                ttl=MASK_SESSION_TTL_SECONDS,
                current_time=current,
            )
        )
    except (
        InvalidToken,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        try:
            payload = json.loads(_session_cipher().decrypt(token.encode()))
        except (InvalidToken, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            raise AiMaskSessionError(
                "invalid_mask_session", "Mask session is invalid"
            ) from exc
        expires_at = payload.get("exp") if isinstance(payload, dict) else None
        if type(expires_at) is int and expires_at <= current:
            raise AiMaskSessionError(
                "mask_session_expired", "Mask session has expired"
            ) from exc
        raise AiMaskSessionError(
            "invalid_mask_session", "Mask session is invalid"
        ) from exc
    if not isinstance(payload, dict) or payload.get("v") != _SESSION_VERSION:
        raise AiMaskSessionError("invalid_mask_session", "Mask session is invalid")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        type(issued_at) is not int
        or type(expires_at) is not int
        or expires_at - issued_at != MASK_SESSION_TTL_SECONDS
        or issued_at > current + _MAX_CLOCK_SKEW_SECONDS
    ):
        raise AiMaskSessionError("invalid_mask_session", "Mask session is invalid")
    if expires_at <= current:
        raise AiMaskSessionError("mask_session_expired", "Mask session has expired")
    raw = payload.get("raw")
    try:
        if not isinstance(raw, str) or len(raw) > MAX_LOW_RES_MASK_INPUT_CHARS:
            raise ValueError
        decode_low_res_mask(raw)
    except ValueError as exc:
        raise AiMaskSessionError(
            "invalid_mask_session", "Mask session is invalid"
        ) from exc
    return payload


__all__ = [
    "AiMaskSessionError",
    "MASK_SESSION_TTL_SECONDS",
    "MAX_MASK_SESSION_TOKEN_CHARS",
    "issue_ai_mask_session",
    "verify_ai_mask_session",
]
