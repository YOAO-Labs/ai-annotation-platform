"""Portable contract for locked current-account facts in caller transactions."""

from __future__ import annotations

import uuid
from typing import Literal, TypeAlias

from pydantic import Field, ValidationError, model_validator

from .actor_contract import _json_bytes
from .contract import ClosedModel, IdentityUnavailable

ACCOUNT_LOCK_PROTOCOL = "aap-identity-account-locks/v1"
AccountLockMode: TypeAlias = Literal["share", "update", "key_share"]


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
