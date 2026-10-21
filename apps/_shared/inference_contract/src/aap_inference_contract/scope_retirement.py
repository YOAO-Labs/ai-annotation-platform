"""Strict wire contracts for retiring one durable Inference scope."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    model_validator,
)

SCOPE_RETIREMENT_PROTOCOL = "aap-inference-scope-retirement/v1"
SCOPE_RETIREMENT_RESULT_PROTOCOL = "aap-inference-scope-retirement-result/v1"
SCOPE_RETIREMENT_DATA_PROTOCOL = "aap-inference-scope-retirement-data/v1"
SCOPE_RETIREMENT_NATIVE_FUNCTIONS = (
    "aap_inference_routing_v1.routing_policy()",
    "aap_inference_routing_v1.routing_step(text,text)",
    "aap_inference_routing_v1.scope_retirement_step(text,text)",
    "aap_inference_routing_v1.video_execution_scope_step(text,text)",
)
SCOPE_RETIREMENT_REQUEST_MAX_BYTES = 65_536
SCOPE_RETIREMENT_REPLY_MAX_BYTES = 65_536
SCOPE_RETIREMENT_ACTIONS = frozenset({"policy", "prepare", "status", "finish"})

_UUID_FIELDS = frozenset(
    {
        "retirement_id",
        "scope_id",
        "registry_id",
        "authority_epoch",
        "backend_incarnation",
    }
)


class ScopeRetirementUnavailable(Exception):
    """The selected scope-retirement owner or its response is unavailable."""

    def __init__(self) -> None:
        super().__init__("Inference scope-retirement owner unavailable")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_uuid(value: object, *, nullable: bool = False) -> uuid.UUID | None:
    if nullable and value is None:
        return None
    if type(value) is uuid.UUID:
        return value
    if type(value) is not str:
        raise ValueError("Invalid scope-retirement UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("Invalid scope-retirement UUID") from None
    if str(parsed) != value:
        raise ValueError("Invalid scope-retirement UUID")
    return parsed


def _check_canonical_uuid_strings(value: object, fields: frozenset[str]) -> object:
    if not isinstance(value, Mapping):
        return value
    for name in fields.intersection(value):
        item = value[name]
        if type(item) is uuid.UUID:
            continue
        _canonical_uuid(item)
    return value


class ScopeRetirementRequest(_StrictModel):
    schema_: Literal["aap-inference-scope-retirement/v1"] = Field(alias="schema")
    retirement_id: uuid.UUID
    scope_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16_384, repr=False)

    @model_validator(mode="before")
    @classmethod
    def canonical_ids(cls, value: object) -> object:
        return _check_canonical_uuid_strings(
            value, frozenset({"retirement_id", "scope_id"})
        )


class ScopeRetirementView(_StrictModel):
    retirement_id: uuid.UUID
    scope_id: uuid.UUID
    registry_id: uuid.UUID
    authority_epoch: uuid.UUID
    backend_incarnation: uuid.UUID
    phase: Literal["prepared", "draining", "completed"]
    closed: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def canonical_ids(cls, value: object) -> object:
        if isinstance(value, Mapping) and value.get("closed") is not True:
            raise ValueError("Retired scope must be explicitly closed")
        return _check_canonical_uuid_strings(value, _UUID_FIELDS)


class ScopeRetirementRejected(_StrictModel):
    reason: Literal[
        "scope_unavailable",
        "backend_not_ready",
        "transport_changed",
        "management_busy",
        "invalid_command",
        "retirement_conflict",
    ]


class ScopeRetirementResult(_StrictModel):
    schema_: Literal["aap-inference-scope-retirement-result/v1"] = Field(alias="schema")
    retirement_id: uuid.UUID
    scope_id: uuid.UUID
    state: Literal["committed", "unknown"]
    result: ScopeRetirementView | ScopeRetirementRejected | None = Field(repr=False)

    @model_validator(mode="before")
    @classmethod
    def canonical_ids(cls, value: object) -> object:
        return _check_canonical_uuid_strings(
            value, frozenset({"retirement_id", "scope_id"})
        )

    @model_validator(mode="after")
    def bound_result(self) -> ScopeRetirementResult:
        if self.state == "unknown":
            if self.result is not None:
                raise ValueError("Unknown scope-retirement result must be null")
        elif self.result is None:
            raise ValueError("Committed scope-retirement result must be present")
        if isinstance(self.result, ScopeRetirementView) and (
            self.result.retirement_id != self.retirement_id
            or self.result.scope_id != self.scope_id
        ):
            raise ValueError("Scope-retirement result identity mismatch")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate scope-retirement field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _decode_object(value: object, *, max_bytes: int) -> dict[str, Any]:
    try:
        if type(value) is bytes:
            raw = value
        elif type(value) is str:
            raw = value.encode("utf-8", errors="strict")
        elif isinstance(value, BaseModel):
            raw = json.dumps(
                value.model_dump(mode="json", by_alias=True),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        elif isinstance(value, Mapping):
            raw = json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            raise ScopeRetirementUnavailable()
        if len(raw) > max_bytes:
            raise ScopeRetirementUnavailable()
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ScopeRetirementUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise ScopeRetirementUnavailable() from None
    if type(parsed) is not dict:
        raise ScopeRetirementUnavailable()
    return parsed


def _encode(value: BaseModel, *, max_bytes: int) -> bytes:
    try:
        raw = json.dumps(
            value.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise ScopeRetirementUnavailable() from None
    if len(raw) > max_bytes:
        raise ScopeRetirementUnavailable()
    return raw


def decode_scope_retirement_request(value: object) -> ScopeRetirementRequest:
    raw = _decode_object(value, max_bytes=SCOPE_RETIREMENT_REQUEST_MAX_BYTES)
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return ScopeRetirementRequest.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError):
        raise ScopeRetirementUnavailable() from None


def make_scope_retirement_request(
    retirement_id: uuid.UUID | str,
    scope_id: uuid.UUID | str,
    capability: str,
) -> ScopeRetirementRequest:
    return decode_scope_retirement_request(
        {
            "schema": SCOPE_RETIREMENT_PROTOCOL,
            "retirement_id": str(retirement_id),
            "scope_id": str(scope_id),
            "capability": capability,
        }
    )


def scope_retirement_request_bytes(value: ScopeRetirementRequest | object) -> bytes:
    return _encode(
        decode_scope_retirement_request(value),
        max_bytes=SCOPE_RETIREMENT_REQUEST_MAX_BYTES,
    )


def decode_scope_retirement_result(
    request: ScopeRetirementRequest | object, value: object
) -> ScopeRetirementResult:
    typed_request = decode_scope_retirement_request(request)
    raw = _decode_object(value, max_bytes=SCOPE_RETIREMENT_REPLY_MAX_BYTES)
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
        result = ScopeRetirementResult.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError):
        raise ScopeRetirementUnavailable() from None
    if (
        result.retirement_id != typed_request.retirement_id
        or result.scope_id != typed_request.scope_id
    ):
        raise ScopeRetirementUnavailable()
    return result


def scope_retirement_result_bytes(value: ScopeRetirementResult | object) -> bytes:
    try:
        if isinstance(value, ScopeRetirementResult):
            result = value
        else:
            raw = _decode_object(value, max_bytes=SCOPE_RETIREMENT_REPLY_MAX_BYTES)
            encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode(
                "utf-8"
            )
            result = ScopeRetirementResult.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError):
        raise ScopeRetirementUnavailable() from None
    return _encode(result, max_bytes=SCOPE_RETIREMENT_REPLY_MAX_BYTES)


__all__ = [
    "SCOPE_RETIREMENT_ACTIONS",
    "SCOPE_RETIREMENT_DATA_PROTOCOL",
    "SCOPE_RETIREMENT_NATIVE_FUNCTIONS",
    "SCOPE_RETIREMENT_PROTOCOL",
    "SCOPE_RETIREMENT_REPLY_MAX_BYTES",
    "SCOPE_RETIREMENT_REQUEST_MAX_BYTES",
    "SCOPE_RETIREMENT_RESULT_PROTOCOL",
    "ScopeRetirementRejected",
    "ScopeRetirementRequest",
    "ScopeRetirementResult",
    "ScopeRetirementUnavailable",
    "ScopeRetirementView",
    "decode_scope_retirement_request",
    "decode_scope_retirement_result",
    "make_scope_retirement_request",
    "scope_retirement_request_bytes",
    "scope_retirement_result_bytes",
]
