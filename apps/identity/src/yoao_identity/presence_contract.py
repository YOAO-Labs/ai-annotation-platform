"""Closed private HTTP contract for the offline-presence capability."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from .contract import ClosedModel, IdentityUnavailable
from .account_contract import _json_bytes

PRESENCE_PROTOCOL = "aap-identity-presence/v1"


class PresenceRequest(ClosedModel):
    protocol: Literal["aap-identity-presence/v1"] = Field(alias="schema")
    cutoff: datetime

    @field_validator("cutoff")
    @classmethod
    def aware_cutoff(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Presence cutoff must be timezone-aware")
        try:
            return value.astimezone(UTC)
        except (OverflowError, OSError, ValueError):
            raise ValueError("Presence cutoff is outside the supported range") from None


class PresenceReply(ClosedModel):
    protocol: Literal["aap-identity-presence/v1"] = Field(alias="schema")
    affected: int = Field(ge=0, lt=2**63, strict=True)


_PRESENCE_REQUESTS = TypeAdapter(PresenceRequest)
_PRESENCE_REPLIES = TypeAdapter(PresenceReply)


def decode_presence_request(payload: dict | bytes) -> PresenceRequest:
    try:
        return _PRESENCE_REQUESTS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_presence_reply(payload: dict | bytes) -> PresenceReply:
    try:
        return _PRESENCE_REPLIES.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def presence_reply(affected: int) -> PresenceReply:
    try:
        return PresenceReply(schema=PRESENCE_PROTOCOL, affected=affected)
    except (ValidationError, ValueError, TypeError):
        raise IdentityUnavailable() from None
