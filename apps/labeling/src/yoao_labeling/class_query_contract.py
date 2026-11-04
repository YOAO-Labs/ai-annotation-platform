"""Strict private request/reply contract for Labeling class usage."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Literal

from pydantic import ConfigDict, Field, StrictStr, TypeAdapter, ValidationError
from pydantic import BaseModel, model_validator

LABELING_CLASS_QUERY_PROTOCOL = "aap-labeling-class-query/v1"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _WireModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class LabelingClassQueryRequest(_WireModel):
    protocol: Literal["aap-labeling-class-query/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    operation: Literal["get_project_class_usage"]
    project_id: uuid.UUID


class LabelingClassQueryPolicy(_WireModel):
    protocol: Literal["aap-labeling-class-query/v1"] = Field(alias="schema")


class LabelingClassQuerySuccess(_WireModel):
    protocol: Literal["aap-labeling-class-query/v1"] = Field(alias="schema")
    ok: Literal[True]
    request: LabelingClassQueryRequest
    data_json: StrictStr


LabelingClassQueryReply = LabelingClassQuerySuccess


class LabelingClassQueryUnavailable(Exception):
    """The selected Labeling owner or its private reply is unavailable."""

    def __init__(self):
        super().__init__("Labeling class-query owner unavailable")


class LabelingClassQueryInvalidRequest(ValueError):
    """A private class-query request does not match the closed protocol."""

    def __init__(self):
        super().__init__("Invalid Labeling class-query request")


_REQUEST_ADAPTER = TypeAdapter(LabelingClassQueryRequest)
_POLICY_ADAPTER = TypeAdapter(LabelingClassQueryPolicy)
_REPLY_ADAPTER = TypeAdapter(LabelingClassQueryReply)


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
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value


def _json_bytes(payload: object) -> bytes:
    if isinstance(payload, bytes):
        raw = payload
    elif isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = json.dumps(
            _json_ready(payload),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def decode_labeling_class_query_request(payload: object) -> LabelingClassQueryRequest:
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingClassQueryInvalidRequest() from None


def _validated_request(payload: object) -> LabelingClassQueryRequest:
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingClassQueryUnavailable() from None


def decode_labeling_class_query_policy(payload: object) -> LabelingClassQueryPolicy:
    try:
        return _POLICY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingClassQueryUnavailable() from None


def labeling_class_query_request(
    project_id: uuid.UUID, *, request_id: uuid.UUID | None = None
) -> LabelingClassQueryRequest:
    try:
        return LabelingClassQueryRequest(
            schema=LABELING_CLASS_QUERY_PROTOCOL,
            request_id=request_id or uuid.uuid4(),
            operation="get_project_class_usage",
            project_id=project_id,
        )
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingClassQueryInvalidRequest() from None


def _decode_data_json(data_json: str) -> dict[str, dict[str, int]]:
    if type(data_json) is not str or not data_json.isascii():
        raise LabelingClassQueryUnavailable()
    try:
        value = json.loads(
            data_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingClassQueryUnavailable() from None
    if type(value) is not dict or set(value) != {"classes", "attributes"}:
        raise LabelingClassQueryUnavailable()
    for field, allow_empty in (("classes", False), ("attributes", True)):
        counts = value[field]
        if type(counts) is not dict:
            raise LabelingClassQueryUnavailable()
        for key, count in counts.items():
            if (
                type(key) is not str
                or (not allow_empty and key == "")
                or type(count) is not int
                or count <= 0
            ):
                raise LabelingClassQueryUnavailable()
    return value


def decode_labeling_class_query_reply(
    request: object, payload: object
) -> LabelingClassQueryReply:
    request = _validated_request(request)
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingClassQueryUnavailable() from None
    if reply.request != request:
        raise LabelingClassQueryUnavailable()
    _decode_data_json(reply.data_json)
    return reply


def labeling_class_query_schema() -> dict:
    """Return the closed request/reply schema and the opaque JSON data shape."""

    schema = TypeAdapter(
        LabelingClassQueryRequest | LabelingClassQueryPolicy | LabelingClassQueryReply
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = LABELING_CLASS_QUERY_PROTOCOL
    schema["x-data-json-schema"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["classes", "attributes"],
        "properties": {
            "classes": {
                "type": "object",
                "propertyNames": {"minLength": 1},
                "additionalProperties": {"type": "integer", "minimum": 1},
            },
            "attributes": {
                "type": "object",
                "additionalProperties": {"type": "integer", "minimum": 1},
            },
        },
    }
    return schema
