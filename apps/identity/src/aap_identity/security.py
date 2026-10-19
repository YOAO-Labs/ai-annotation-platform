"""Shared credential primitives; signing policy deliberately rejects legacy JWTs."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import jwt

ALGORITHM = "HS256"
ISSUER = "aap-identity/v1"


def signing_key(secret: str) -> str:
    return hmac.new(secret.encode(), ISSUER.encode(), hashlib.sha256).hexdigest()


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def hash_api_key(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError, UnicodeError):
        return False


def create_access_token(
    subject: str,
    role: str,
    *,
    secret: str,
    expire_minutes: int,
    expires_delta: timedelta | None = None,
    gen: int = 0,
) -> str:
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=expire_minutes))
    return jwt.encode(
        {
            "sub": subject,
            "role": role,
            "exp": expire,
            "jti": str(uuid.uuid4()),
            "gen": gen,
            "iss": ISSUER,
        },
        signing_key(secret),
        algorithm=ALGORITHM,
    )


def decode_access_token(
    token: str,
    *,
    secret: str,
    verify_exp: bool = True,
    verify_jti: bool = True,
) -> dict:
    return jwt.decode(
        token,
        signing_key(secret),
        algorithms=[ALGORITHM],
        issuer=ISSUER,
        options={
            "verify_exp": verify_exp,
            "verify_jti": verify_jti,
            "require_iss": True,
        },
    )
