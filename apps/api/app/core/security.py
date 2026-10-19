"""Settings-bound compatibility wrappers for Identity credential primitives."""

from __future__ import annotations

from datetime import timedelta

from aap_identity.security import (
    ALGORITHM as _ALGORITHM,
    create_access_token as _create_access_token,
    decode_access_token as _decode_access_token,
    hash_password as _hash_password,
    verify_password as _verify_password,
)

from app.config import settings

ALGORITHM = _ALGORITHM
hash_password = _hash_password
verify_password = _verify_password


def create_access_token(
    subject: str,
    role: str,
    expires_delta: timedelta | None = None,
    gen: int = 0,
) -> str:
    return _create_access_token(
        subject,
        role,
        secret=settings.secret_key,
        expire_minutes=settings.access_token_expire_minutes,
        expires_delta=expires_delta,
        gen=gen,
    )


def decode_access_token(token: str, *, verify_exp: bool = True) -> dict:
    return _decode_access_token(
        token, secret=settings.secret_key, verify_exp=verify_exp
    )
