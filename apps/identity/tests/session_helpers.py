from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from jose import jwt

from aap_identity.contract import Principal
from aap_identity.security import ALGORITHM, ISSUER, create_access_token, signing_key
from aap_identity.session_contract import decode_session_request

SECRET = "identity-session-test-signing-secret"
CONTROL = "session-runtime-control-token"
ACTOR_ID = uuid.UUID("5c5633f5-0460-4c22-89ba-783af26c2cda")
KEY_ID = uuid.UUID("06fb7bce-aeef-445b-965a-3439c1f8a12b")


def principal(credential_type="jwt", **changes) -> Principal:
    value = {
        "actor_id": str(ACTOR_ID),
        "name": "Current account",
        "email": "person@example.test",
        "role": "employee",
        "is_active": True,
        "credential_type": credential_type,
        "credential_id": str(uuid.uuid4()),
        "generation": 7 if credential_type == "jwt" else None,
        "scopes": None if credential_type == "jwt" else ["datasets:read"],
        "expires_at": "2035-01-01T00:00:00Z" if credential_type == "jwt" else None,
    }
    value.update(changes)
    return Principal.model_validate_json(json.dumps(value, default=str))


def make_jwt(
    *,
    actor_id: uuid.UUID = ACTOR_ID,
    role: str = "employee",
    generation: int = 7,
    expires_at: datetime | None = None,
    token_id: uuid.UUID | None = None,
    secret: str = SECRET,
) -> str:
    exp = expires_at or datetime.now(UTC).replace(microsecond=0)
    if expires_at is None:
        from datetime import timedelta

        exp += timedelta(hours=1)
    return create_access_token(
        str(actor_id),
        role,
        secret=secret,
        expire_minutes=60,
        expires_delta=exp - datetime.now(UTC),
        gen=generation,
    )


def fixed_jwt(
    actor_id: uuid.UUID,
    role: str,
    exp: int,
    jti: uuid.UUID,
    generation: int,
    *,
    secret: str = SECRET,
) -> str:
    return jwt.encode(
        {
            "sub": str(actor_id),
            "role": role,
            "exp": exp,
            "jti": str(jti),
            "gen": generation,
            "iss": ISSUER,
        },
        signing_key(secret),
        algorithm=ALGORITHM,
        headers={"typ": "JWT"},
    )


def audit_context(**changes) -> dict:
    value = {
        "method": "POST",
        "path": "/api/v1/auth/session",
        "ip": "192.0.2.10",
        "request_id": str(uuid.uuid4()),
    }
    value.update(changes)
    return value


def session_request(
    operation: str,
    *,
    operation_id: uuid.UUID | None = None,
    token: str | None = None,
    payload: dict | None = None,
    audit: dict | None = None,
    user_agent: str = "session-test/1.0",
):
    if operation == "login":
        token = None
        payload = payload or {
            "email": "person@example.test",
            "password": "Password!2026",
            "captcha_verified": True,
            "client_ip": "192.0.2.10",
        }
    else:
        payload = {} if payload is None else payload
        token = token or make_jwt()
    return decode_session_request(
        {
            "schema": "aap-identity-sessions/v1",
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "payload": payload,
            "audit_context": audit or audit_context(),
            "user_agent": user_agent,
        }
    )
