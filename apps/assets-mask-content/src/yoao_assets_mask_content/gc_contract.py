"""Frozen wire contract for the durable Mask garbage-collection journal."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .contract import (
    MASK_PREFIX,
    ObjectKey,
    Value,
    _reject_constant,
    _reject_duplicate_keys,
)

GC_SCHEMA = "aap-assets-mask-gc/v1"
GC_JOURNAL_PREFIX = "raster-mask-gc/v1"
MAX_GC_REQUEST_BYTES = 1024 * 1024
MAX_GC_RESPONSE_BYTES = 1024 * 1024
MAX_GC_PAGE = 1000
MAX_CURSOR_BYTES = 1024
GcCursor = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=MAX_CURSOR_BYTES,
        pattern=r"^raster-masks/sha256/",
    ),
]
GcEtag = Annotated[
    StrictStr,
    Field(min_length=1, max_length=255, pattern=r"^[\x21\x23-\x7e]+$"),
]

GcOutcome = Literal["deleted", "missing", "content_changed"]
GcState = Literal["not_found", "pending", "indeterminate", "completed"]
GcErrorCode = Literal[
    "invalid_request",
    "idempotency_conflict",
    "resource_limit",
    "storage_unavailable",
    "invalid_response",
    "permission_denied",
]


def validate_gc_object_key(value: str) -> str:
    from .contract import validate_object_key

    return validate_object_key(value)


def _parse_timestamp(value):
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(normalized)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("last_modified must include a timezone")
    return value.astimezone(timezone.utc)


def _validate_etag(value: str) -> str:
    if not value or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in value
    ):
        raise ValueError("etag must be visible ASCII without whitespace")
    if '"' in value:
        raise ValueError("etag must not include HTTP quote delimiters")
    return value


def _parse_uuid(value):
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("operation_id must be a UUID")
    parsed = UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("operation_id must use canonical UUID form")
    return parsed


def _validate_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(MASK_PREFIX + "/"):
        raise ValueError("after_key must remain within the Mask namespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("after_key must be valid UTF-8") from None
    if len(encoded) > MAX_CURSOR_BYTES:
        raise ValueError("after_key exceeds the cursor byte limit")
    return value


def _same_utf8_order(left: str, right: str) -> bool:
    return left.encode("utf-8") < right.encode("utf-8")


class GcValue(Value):
    """Separate base lets this schema evolve without changing content/v1."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class GcCandidate(GcValue):
    object_key: ObjectKey
    etag: GcEtag
    last_modified: AwareDatetime
    byte_size: Annotated[StrictInt, Field(ge=0)]

    @field_validator("object_key")
    @classmethod
    def canonical_mask_key(cls, value):
        return validate_gc_object_key(value)

    @field_validator("etag")
    @classmethod
    def safe_etag(cls, value):
        return _validate_etag(value)

    @field_validator("last_modified", mode="before")
    @classmethod
    def parse_wire_timestamp(cls, value):
        return _parse_timestamp(value)


class GcScanRequest(GcValue):
    after_key: GcCursor | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=MAX_GC_PAGE)] = MAX_GC_PAGE

    @field_validator("after_key")
    @classmethod
    def bounded_physical_cursor(cls, value):
        return _validate_cursor(value)


class GcScanPage(GcValue):
    items: Annotated[tuple[GcCandidate, ...], Field(max_length=MAX_GC_PAGE)]
    after_key: GcCursor | None
    complete: StrictBool

    @field_validator("items", mode="before")
    @classmethod
    def list_to_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("after_key")
    @classmethod
    def bounded_physical_cursor(cls, value):
        return _validate_cursor(value)

    @model_validator(mode="after")
    def valid_scan_page(self):
        if len(self.items) > MAX_GC_PAGE:
            raise ValueError("GC scan page exceeds the maximum page size")
        if not self.complete and self.after_key is None:
            raise ValueError("a nonfinal page must advance its physical cursor")
        if self.items and self.after_key is None:
            raise ValueError("a page with candidates requires a physical cursor")
        for previous, current in zip(self.items, self.items[1:]):
            if not _same_utf8_order(previous.object_key, current.object_key):
                raise ValueError("GC candidates must be ordered and unique")
        if self.items and _same_utf8_order(self.after_key, self.items[-1].object_key):
            raise ValueError("physical cursor must be at or after the last candidate")
        return self


class DeleteOperationRequest(GcValue):
    operation_id: UUID
    object_key: ObjectKey
    etag: GcEtag
    last_modified: AwareDatetime

    @field_validator("operation_id", mode="before")
    @classmethod
    def strict_operation_id(cls, value):
        return _parse_uuid(value)

    @field_validator("object_key")
    @classmethod
    def canonical_mask_key(cls, value):
        return validate_gc_object_key(value)

    @field_validator("etag")
    @classmethod
    def safe_etag(cls, value):
        return _validate_etag(value)

    @field_validator("last_modified", mode="before")
    @classmethod
    def parse_wire_timestamp(cls, value):
        return _parse_timestamp(value)


class DeleteStatusRequest(GcValue):
    operation_id: UUID

    @field_validator("operation_id", mode="before")
    @classmethod
    def strict_operation_id(cls, value):
        return _parse_uuid(value)


class DeleteOperationStatus(GcValue):
    operation_id: UUID
    request: DeleteOperationRequest | None
    state: GcState
    outcome: GcOutcome | None

    @field_validator("operation_id", mode="before")
    @classmethod
    def strict_operation_id(cls, value):
        return _parse_uuid(value)

    @field_validator("request", mode="before")
    @classmethod
    def request_mapping_to_model(cls, value):
        if isinstance(value, dict):
            return DeleteOperationRequest.model_validate(value)
        return value

    @model_validator(mode="after")
    def valid_operation_state(self):
        if self.state == "not_found":
            if self.request is not None or self.outcome is not None:
                raise ValueError("not_found status must omit request and outcome")
        elif self.request is None or self.request.operation_id != self.operation_id:
            raise ValueError("operation status requires its matching request")
        elif self.state in {"pending", "indeterminate"}:
            if self.outcome is not None:
                raise ValueError("nonterminal status cannot include an outcome")
        elif self.state == "completed" and self.outcome is None:
            raise ValueError("completed status requires a terminal outcome")
        return self


class _GcError(GcValue):
    code: GcErrorCode


class _GcSuccessEnvelope(GcValue):
    schema_: Literal["aap-assets-mask-gc/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: dict


class _GcErrorEnvelope(GcValue):
    schema_: Literal["aap-assets-mask-gc/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: _GcError


class MaskGcFailure(Exception):
    """Closed GC protocol error that excludes provider details and request data."""

    _CODES = frozenset(GcErrorCode.__args__)

    def __init__(self, code: GcErrorCode):
        if code not in self._CODES:
            code = "invalid_response"
        self.code = code
        super().__init__(code)


_T = TypeVar("_T", bound=BaseModel)


def json_bytes(value) -> bytes:
    from .contract import json_bytes as content_json_bytes

    return content_json_bytes(value)


def parse_json(raw: bytes):
    if not isinstance(raw, bytes):
        raise ValueError("JSON input must be bytes")
    return json.loads(
        raw.decode("utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )


def parse_reply(raw: bytes, result_type: type[_T]) -> _T:
    try:
        document = parse_json(raw)
        if not isinstance(document, dict) or document.get("schema") != GC_SCHEMA:
            raise ValueError("wrong Mask GC schema")
        if document.get("ok") is True:
            envelope = _GcSuccessEnvelope.model_validate(document)
            return result_type.model_validate(envelope.data)
        if document.get("ok") is False:
            envelope = _GcErrorEnvelope.model_validate(document)
            raise MaskGcFailure(envelope.error.code)
        raise ValueError("invalid Mask GC envelope")
    except MaskGcFailure:
        raise
    except (ValueError, TypeError, RecursionError):
        raise MaskGcFailure("invalid_response") from None


def success_envelope(result: BaseModel) -> dict:
    return {
        "schema": GC_SCHEMA,
        "ok": True,
        "data": result.model_dump(mode="json", by_alias=True),
    }


def error_envelope(code: GcErrorCode) -> dict:
    return {"schema": GC_SCHEMA, "ok": False, "error": {"code": code}}


def http_error_status(code: GcErrorCode) -> int:
    return {
        "invalid_request": 422,
        "idempotency_conflict": 409,
        "resource_limit": 413,
        "storage_unavailable": 503,
        "invalid_response": 502,
        "permission_denied": 403,
    }[code]


def request_type(operation: str):
    return {
        "scan": GcScanRequest,
        "execute": DeleteOperationRequest,
        "status": DeleteStatusRequest,
    }.get(operation)


def result_type(operation: str):
    return {
        "scan": GcScanPage,
        "execute": DeleteOperationStatus,
        "status": DeleteOperationStatus,
    }.get(operation)


def contract_document() -> dict:
    models = (
        GcCandidate,
        GcScanRequest,
        GcScanPage,
        DeleteOperationRequest,
        DeleteStatusRequest,
        DeleteOperationStatus,
        _GcError,
    )
    definitions: dict[str, dict] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            definitions[name] = definition
        definitions[model.__name__] = schema
    request_names = ("GcScanRequest", "DeleteOperationRequest", "DeleteStatusRequest")
    result_names = ("GcScanPage", "DeleteOperationStatus")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://schemas.aap.invalid/assets-mask-gc/v1",
        "title": "AAP Mask garbage collection request and response messages",
        "oneOf": [
            *[{"$ref": f"#/$defs/{name}"} for name in request_names],
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "ok", "data"],
                "properties": {
                    "schema": {"const": GC_SCHEMA},
                    "ok": {"const": True},
                    "data": {
                        "oneOf": [{"$ref": f"#/$defs/{name}"} for name in result_names]
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "ok", "error"],
                "properties": {
                    "schema": {"const": GC_SCHEMA},
                    "ok": {"const": False},
                    "error": {"$ref": "#/$defs/_GcError"},
                },
            },
        ],
        "x-aap-schema": GC_SCHEMA,
        "x-operation-roles": {
            "scan": "cleanup",
            "execute": "cleanup",
            "status": "cleanup",
        },
        "x-budgets": {
            "request_bytes": MAX_GC_REQUEST_BYTES,
            "response_bytes": MAX_GC_RESPONSE_BYTES,
            "max_page": MAX_GC_PAGE,
            "max_cursor_bytes": MAX_CURSOR_BYTES,
        },
        "$defs": definitions,
    }


def openapi_document() -> dict:
    models = (
        GcCandidate,
        GcScanRequest,
        GcScanPage,
        DeleteOperationRequest,
        DeleteStatusRequest,
        DeleteOperationStatus,
        _GcError,
    )
    definitions: dict[str, dict] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("conflicting Mask GC schema definition")
            definitions[name] = definition
        name = model.__name__
        if name in definitions and definitions[name] != schema:
            raise ValueError("conflicting Mask GC schema definition")
        definitions[name] = schema

    paths = {}
    for operation, request_name, result_name in (
        ("scan", "GcScanRequest", "GcScanPage"),
        ("execute", "DeleteOperationRequest", "DeleteOperationStatus"),
        ("status", "DeleteStatusRequest", "DeleteOperationStatus"),
    ):
        paths[f"/v1/mask-gc/{operation}"] = {
            "post": {
                "operationId": f"{operation}MaskGc",
                "x-required-process-role": "cleanup",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{request_name}"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Mask garbage collection result",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["schema", "ok", "data"],
                                    "properties": {
                                        "schema": {"const": GC_SCHEMA},
                                        "ok": {"const": True},
                                        "data": {
                                            "$ref": f"#/components/schemas/{result_name}"
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "default": {
                        "description": "Mask garbage collection error",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["schema", "ok", "error"],
                                    "properties": {
                                        "schema": {"const": GC_SCHEMA},
                                        "ok": {"const": False},
                                        "error": {
                                            "$ref": "#/components/schemas/_GcError"
                                        },
                                    },
                                }
                            }
                        },
                    },
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "AAP Mask garbage collection", "version": "1"},
        "security": [{"serviceToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": definitions,
        },
        "x-aap-schema": GC_SCHEMA,
        "x-operation-roles": {
            "scan": "cleanup",
            "execute": "cleanup",
            "status": "cleanup",
        },
        "x-budgets": {
            "request_bytes": MAX_GC_REQUEST_BYTES,
            "response_bytes": MAX_GC_RESPONSE_BYTES,
            "max_page": MAX_GC_PAGE,
            "max_cursor_bytes": MAX_CURSOR_BYTES,
        },
    }
