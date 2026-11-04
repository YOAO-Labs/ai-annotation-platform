"""Portable contract for locked current-account facts in caller transactions."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, StrictBool, TypeAdapter, ValidationError, model_validator

from .actor_contract import _json_bytes
from .contract import ClosedModel, IdentityUnavailable

ACCOUNT_LOCK_PROTOCOL = "aap-identity-account-locks/v1"
AccountLockMode: TypeAlias = Literal["share", "update", "key_share"]
ACCEPTED_API_KEY_PROTOCOL = "aap-identity-accepted-api-key/v1"
ACCEPTED_API_KEY_FACTS_PROTOCOL = "aap-identity-accepted-api-key-facts/v1"


def accepted_api_key_facts_schema() -> dict:
    """Non-secret current facts of an already accepted key."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ACCEPTED_API_KEY_FACTS_PROTOCOL,
        "$comment": (
            "Confirms an already accepted key ID under its SHARE fence and returns "
            "current scopes, expiry and a digest revision of the stored verifier. "
            "The verifier itself is never returned. The caller keeps its actor "
            "lock; no transaction is committed or rolled back."
        ),
        "x-postgresql-functions": [
            "aap_identity_v1.read_accepted_api_key_facts(uuid, uuid)"
        ],
        "x-postgresql-result": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["scopes", "expires_at", "revision"],
                "additionalProperties": False,
                "properties": {
                    "scopes": {"type": "array", "items": {"type": "string"}},
                    "expires_at": {"type": ["string", "null"]},
                    "revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
        },
    }


class AcceptedApiKeyRequest(ClosedModel):
    """Current key authority for accepted work, inside its caller transaction."""

    protocol: Literal["aap-identity-accepted-api-key/v1"] = Field(alias="schema")
    actor_id: uuid.UUID
    api_key_id: uuid.UUID
    required_scopes: list[Annotated[str, Field(strict=True)]]


def accepted_api_key_schema() -> dict:
    schema = TypeAdapter(AcceptedApiKeyRequest | StrictBool).json_schema(by_alias=True)
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": ACCEPTED_API_KEY_PROTOCOL,
            "$comment": (
                "Confirms an already accepted key ID, not credential possession. "
                "The caller retains account/project authority locks. The key FOR "
                "SHARE lock waits and remains in the caller transaction. Expiry "
                "is checked against the real clock after any lock wait. Invalid "
                "arguments raise SQLSTATE 22023; database errors propagate."
            ),
            "x-postgresql-functions": [
                "aap_identity_v1.check_accepted_api_key(uuid, uuid, text[])"
            ],
            "x-postgresql-result": {"type": "boolean"},
        }
    )
    return schema


class AccountLockRequest(ClosedModel):
    protocol: Literal["aap-identity-account-locks/v1"] = Field(alias="schema")
    account_ids: list[uuid.UUID]
    mode: AccountLockMode
    nowait: bool = Field(strict=True)

    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class AccountLockFacts(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    email: str = Field(strict=True)
    role: str = Field(strict=True)
    is_active: bool = Field(strict=True)


class AccountLockReply(ClosedModel):
    protocol: Literal["aap-identity-account-locks/v1"] = Field(alias="schema")
    accounts: list[AccountLockFacts]


def decode_account_lock_reply(
    payload: dict | bytes | ClosedModel, request: AccountLockRequest
) -> list[AccountLockFacts]:
    if not isinstance(request, AccountLockRequest):
        raise IdentityUnavailable()
    try:
        request = AccountLockRequest.model_validate_json(
            request.model_dump_json(by_alias=True)
        )
        encoded = _json_bytes(payload)
        reply = AccountLockReply.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityUnavailable() from None

    requested_ids = {str(account_id) for account_id in request.account_ids}
    reply_ids = [str(account.id) for account in reply.accounts]
    if (
        len(reply_ids) != len(set(reply_ids))
        or reply_ids != sorted(reply_ids)
        or not set(reply_ids) <= requested_ids
    ):
        raise IdentityUnavailable()
    return reply.accounts
