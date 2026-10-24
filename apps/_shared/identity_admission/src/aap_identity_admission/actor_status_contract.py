"""Closed wire messages for rechecking account activity after admission."""

from __future__ import annotations

import json
import uuid
from typing import Literal

from pydantic import Field, StrictBool, ValidationError

from .contract import ClosedModel, IdentityFailure, IdentityUnavailable

ACTOR_STATUS_PROTOCOL = "aap-identity-actor-status/v1"


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def _json_bytes(payload: dict | bytes | ClosedModel) -> bytes:
    if isinstance(payload, bytes):
        raw = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_protocol_alias(raw)
        return payload
    if isinstance(payload, ClosedModel):
        payload = payload.model_dump(mode="json", by_alias=True)
    if not isinstance(payload, dict):
        raise ValueError("Invalid Identity actor-status message")
    _reject_protocol_alias(payload)
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _reject_protocol_alias(value: object) -> None:
    if isinstance(value, dict) and "protocol" in value:
        raise ValueError("Use only the declared wire field schema")


class ActorStatusRequest(ClosedModel):
    protocol: Literal["aap-identity-actor-status/v1"] = Field(alias="schema")
    actor_id: uuid.UUID


class ActorStatus(ClosedModel):
    actor_id: uuid.UUID
    is_active: StrictBool


class ActorStatusData(ClosedModel):
    actor: ActorStatus | None


class ActorStatusSuccess(ClosedModel):
    protocol: Literal["aap-identity-actor-status/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: ActorStatusData


class ActorStatusFailureDetail(ClosedModel):
    code: Literal["invalid_request"]


class ActorStatusFailureReply(ClosedModel):
    protocol: Literal["aap-identity-actor-status/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: ActorStatusFailureDetail


def decode_actor_status_request(payload: dict | bytes) -> ActorStatusRequest:
    try:
        return ActorStatusRequest.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityFailure("invalid_request") from None


def decode_actor_status_reply(
    payload: dict | bytes | ClosedModel, request: ActorStatusRequest
) -> ActorStatus | None:
    if not isinstance(request, ActorStatusRequest):
        raise IdentityUnavailable()
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise ValueError("Invalid actor-status envelope")
        if not raw["ok"]:
            failure = ActorStatusFailureReply.model_validate_json(encoded)
            raise IdentityFailure(failure.error.code)
        reply = ActorStatusSuccess.model_validate_json(encoded)
        actor = reply.data.actor
        if actor is not None and actor.actor_id != request.actor_id:
            raise IdentityUnavailable()
        return actor
    except IdentityFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityUnavailable() from None


def actor_status_reply(actor: ActorStatus | None) -> dict:
    return ActorStatusSuccess(
        schema=ACTOR_STATUS_PROTOCOL,
        ok=True,
        data=ActorStatusData(actor=actor),
    ).model_dump(mode="json", by_alias=True)


def actor_status_failure_reply(code: str = "invalid_request") -> dict:
    if code != "invalid_request":
        raise ValueError("Unsupported actor-status failure code")
    return ActorStatusFailureReply(
        schema=ACTOR_STATUS_PROTOCOL,
        ok=False,
        error=ActorStatusFailureDetail(code="invalid_request"),
    ).model_dump(mode="json", by_alias=True)
