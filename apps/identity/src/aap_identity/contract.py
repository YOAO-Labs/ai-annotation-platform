"""Closed, language-neutral admission messages; no ORM or runtime imports."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

PROTOCOL = "aap-identity/v1"
REFRESH_GRACE_SECONDS = 604800
FailureCode = Literal[
    "invalid_request",
    "credential_invalid",
    "credential_revoked",
    "generation_outdated",
    "token_expired",
    "user_inactive",
    "invalid_account_role",
    "credential_busy",
]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Principal(ClosedModel):
    actor_id: uuid.UUID
    name: str
    email: str
    role: Literal["super_admin", "project_admin", "employee", "viewer"]
    is_active: Literal[True]
    credential_type: Literal["jwt", "api_key"]
    credential_id: uuid.UUID
    generation: int | None = Field(ge=0, lt=2**63)
    scopes: list[str] | None
    expires_at: datetime | None

    @field_validator("is_active", mode="before")
    @classmethod
    def boolean_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Active identity must be a boolean")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Credential expiry must include a timezone")
            return value.astimezone(UTC)
        return None

    @model_validator(mode="after")
    def credential_shape(self) -> Principal:
        if self.credential_type == "jwt":
            if (
                type(self.generation) is not int
                or not 0 <= self.generation < 2**63
                or self.scopes is not None
                or self.expires_at is None
            ):
                raise ValueError("Invalid JWT principal shape")
        elif self.generation is not None or self.scopes is None:
            raise ValueError("Invalid API key principal shape")
        if type(self.is_active) is not bool:
            raise ValueError("Active identity must be a boolean")
        return self


class AuthenticateRequest(ClosedModel):
    token: str = Field(min_length=1, max_length=16384, repr=False)


class PrincipalReply(ClosedModel):
    protocol: Literal["aap-identity/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: Principal


class FailureDetail(ClosedModel):
    code: FailureCode


class FailureReply(ClosedModel):
    protocol: Literal["aap-identity/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: FailureDetail


class Candidate(ClosedModel):
    id: uuid.UUID
    key_hash: str = Field(repr=False)


class CandidatesPage(ClosedModel):
    protocol: Literal["aap-identity-candidates/v1"] = Field(alias="schema")
    items: list[Candidate]
    next_after: uuid.UUID | None


class IdentityFailure(Exception):
    def __init__(self, code: FailureCode):
        self.code = code
        super().__init__(code)


class IdentityUnavailable(Exception):
    def __init__(self):
        super().__init__("Identity authentication unavailable")


def _reject_protocol_alias(value: object) -> None:
    """Check the envelope before JSON-mode validation, preserving date semantics."""
    if isinstance(value, dict) and "protocol" in value:
        raise ValueError("Use only the declared wire field schema")


def decode_principal_reply(payload: dict | bytes) -> Principal:
    try:
        raw = json.loads(payload) if isinstance(payload, bytes) else payload
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise ValueError("Invalid identity envelope")
        _reject_protocol_alias(raw)
        encoded = json.dumps(raw)
        if raw["ok"]:
            return PrincipalReply.model_validate_json(encoded).data
        failure = FailureReply.model_validate_json(encoded)
    except (ValueError, TypeError, ValidationError, UnicodeError):
        raise IdentityUnavailable() from None
    raise IdentityFailure(failure.error.code)


def principal_reply(principal: Principal) -> dict:
    return PrincipalReply(schema=PROTOCOL, ok=True, data=principal).model_dump(
        mode="json", by_alias=True
    )


def failure_reply(code: FailureCode) -> dict:
    return FailureReply(
        schema=PROTOCOL, ok=False, error=FailureDetail(code=code)
    ).model_dump(mode="json", by_alias=True)


def require_jwt_scope(
    principal: Principal,
    actor_id: uuid.UUID,
    jti: uuid.UUID,
    generation: int,
    expires_at: datetime,
) -> None:
    if (
        principal.actor_id != actor_id
        or principal.credential_type != "jwt"
        or principal.credential_id != jti
        or principal.generation != generation
        or principal.expires_at != expires_at
    ):
        raise IdentityUnavailable()
