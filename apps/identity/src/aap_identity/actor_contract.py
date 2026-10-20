"""Closed portable contract for hydrating an already admitted account."""

from __future__ import annotations

import json
import math
import uuid
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from .contract import (
    ClosedModel,
    IdentityFailure,
    IdentityUnavailable,
    _reject_protocol_alias,
)

ACTOR_PROTOCOL = "aap-identity-actor/v1"
ActorRole: TypeAlias = Literal["super_admin", "project_admin", "employee", "viewer"]
ActorFailureCode: TypeAlias = Literal["invalid_request", "credential_invalid"]
Int32: TypeAlias = Annotated[int, Field(strict=True, ge=-(2**31), le=2**31 - 1)]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def _json_ready(value: object) -> object:
    if isinstance(value, ClosedModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value


def _assert_json_value(value: object) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for child in value.values():
            _assert_json_value(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_json_value(child)
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    elif value is not None and type(value) not in (str, bool, int, float):
        raise ValueError("Invalid JSON value")


def _json_bytes(payload: dict | bytes | ClosedModel) -> bytes:
    if isinstance(payload, bytes):
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_protocol_alias(value)
        _assert_json_value(value)
        return payload
    value = _json_ready(payload)
    _reject_protocol_alias(value)
    _assert_json_value(value)
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


class ActorContextRequest(ClosedModel):
    protocol: Literal["aap-identity-actor/v1"] = Field(alias="schema")
    actor_id: uuid.UUID
    admitted_role: ActorRole


class ActorContext(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    email: str = Field(strict=True)
    role: ActorRole
    is_active: Literal[True]
    weekly_target_default: Int32 | None

    @field_validator("is_active", mode="before")
    @classmethod
    def boolean_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Active actor must be a boolean")
        return value


class ActorContextSuccess(ClosedModel):
    protocol: Literal["aap-identity-actor/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: ActorContext

    @field_validator("ok", mode="before")
    @classmethod
    def boolean_ok(cls, value):
        if type(value) is not bool:
            raise ValueError("Actor result must be a boolean")
        return value


class ActorContextFailureDetail(ClosedModel):
    code: ActorFailureCode


class ActorContextFailureReply(ClosedModel):
    protocol: Literal["aap-identity-actor/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: ActorContextFailureDetail

    @field_validator("ok", mode="before")
    @classmethod
    def boolean_ok(cls, value):
        if type(value) is not bool:
            raise ValueError("Actor result must be a boolean")
        return value


_ACTOR_REQUESTS = TypeAdapter(ActorContextRequest)


def decode_actor_request(payload: dict | bytes) -> ActorContextRequest:
    try:
        return _ACTOR_REQUESTS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityFailure("invalid_request") from None


def decode_actor_reply(
    payload: dict | bytes | ClosedModel, request: ActorContextRequest
) -> ActorContext:
    if not isinstance(request, ActorContextRequest):
        raise IdentityUnavailable()
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise ValueError("Invalid actor envelope")
        if not raw["ok"]:
            failure = ActorContextFailureReply.model_validate_json(encoded)
        else:
            reply = ActorContextSuccess.model_validate_json(encoded)
            actor = reply.data
            if (
                actor.id != request.actor_id
                or actor.role != request.admitted_role
                or actor.is_active is not True
            ):
                raise IdentityUnavailable()
            return actor
    except IdentityUnavailable:
        raise
    except ValidationError:
        raise IdentityUnavailable() from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityUnavailable() from None
    raise IdentityFailure(failure.error.code)


def actor_reply(actor: ActorContext) -> dict:
    return ActorContextSuccess(schema=ACTOR_PROTOCOL, ok=True, data=actor).model_dump(
        mode="json", by_alias=True
    )


def actor_failure_reply(code: ActorFailureCode) -> dict:
    return ActorContextFailureReply(
        schema=ACTOR_PROTOCOL,
        ok=False,
        error=ActorContextFailureDetail(code=code),
    ).model_dump(mode="json", by_alias=True)
