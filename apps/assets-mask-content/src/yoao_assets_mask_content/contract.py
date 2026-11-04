"""Frozen language-neutral value and HTTP envelope contract for Mask bytes."""

from __future__ import annotations

import json
from datetime import datetime
import re
from typing import Annotated, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

CONTENT_SCHEMA = "aap-assets-mask-content/v1"
MASK_PREFIX = "raster-masks/sha256"
MAX_RLE_OBJECT_BYTES = 4 * 1024 * 1024
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_EXPANSION_RATIO = 20
MAX_MASK_DIMENSION = 8192
MAX_MASK_PIXELS = 67_108_864
MAX_MASK_RUNS = 1_000_000
MAX_REQUEST_BYTES = MAX_RLE_OBJECT_BYTES + 16 * 1024
MAX_RESPONSE_BYTES = MAX_RLE_OBJECT_BYTES + 16 * 1024
MAX_LIST_PAGE = 1000

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Dimension = Annotated[StrictInt, Field(ge=1, le=MAX_MASK_DIMENSION)]
RunLength = Annotated[StrictInt, Field(ge=0)]
ObjectKey = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=255,
        pattern=(
            r"^raster-masks/sha256/[0-9a-f]{2}/[0-9a-f]{2}/"
            r"[0-9a-f]{64}\.json(?:\.gz)?$"
        ),
    ),
]
_OBJECT_KEY_RE = re.compile(
    r"^raster-masks/sha256/([0-9a-f]{2})/([0-9a-f]{2})/"
    r"([0-9a-f]{64})(\.json(?:\.gz)?)$"
)

ErrorCode = Literal[
    "invalid_request",
    "invalid_reference",
    "object_missing",
    "object_corrupt",
    "content_changed",
    "resource_limit",
    "storage_unavailable",
    "invalid_response",
    "permission_denied",
]


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


def _two_int_tuple(value, name: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be [height, width]")
    if any(type(part) is not int for part in value):
        raise ValueError(f"{name} must contain strict integers")
    return int(value[0]), int(value[1])


def validate_object_key(value: str) -> str:
    match = _OBJECT_KEY_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Mask object key is outside the fixed content namespace")
    first, second, digest, _suffix = match.groups()
    if (first, second) != (digest[:2], digest[2:4]):
        raise ValueError("Mask object key does not match its digest")
    return value


class RleContent(Value):
    """Canonical COCO uncompressed RLE object sent to/from the byte owner."""

    encoding: Literal["coco_rle"]
    size: tuple[Dimension, Dimension]
    counts: Annotated[
        tuple[RunLength, ...], Field(min_length=1, max_length=MAX_MASK_RUNS)
    ]

    @field_validator("size", mode="before")
    @classmethod
    def strict_size_tuple(cls, value):
        return _two_int_tuple(value, "size")

    @field_validator("counts", mode="before")
    @classmethod
    def strict_counts_tuple(cls, value):
        if not isinstance(value, (list, tuple)):
            raise ValueError("counts must be a non-empty integer array")
        if not 1 <= len(value) <= MAX_MASK_RUNS:
            raise ValueError(f"mask runs must be between 1 and {MAX_MASK_RUNS}")
        if any(type(part) is not int for part in value):
            raise ValueError("counts must contain strict integers")
        return tuple(int(part) for part in value)

    @model_validator(mode="after")
    def valid_rle(self):
        height, width = self.size
        if height <= 0 or width <= 0:
            raise ValueError("size dimensions must be positive")
        if height > MAX_MASK_DIMENSION or width > MAX_MASK_DIMENSION:
            raise ValueError(f"mask dimensions must be <= {MAX_MASK_DIMENSION}")
        pixels = height * width
        if pixels > MAX_MASK_PIXELS:
            raise ValueError(f"mask pixels must be <= {MAX_MASK_PIXELS}")
        if not self.counts:
            raise ValueError("counts must be a non-empty integer array")
        if len(self.counts) > MAX_MASK_RUNS:
            raise ValueError(f"mask runs must be <= {MAX_MASK_RUNS}")
        if any(count < 0 for count in self.counts):
            raise ValueError("counts must be non-negative")
        total = sum(self.counts)
        if total != pixels:
            raise ValueError("sum(counts) must equal height * width")
        return self


class MaskReference(Value):
    """Existing API-compatible content reference, including legacy gzip refs."""

    encoding: Literal["coco_rle_ref", "coco_rle_gzip"] = "coco_rle_ref"
    storage_encoding: Literal["identity", "gzip"] | None = None
    size: tuple[Dimension, Dimension]
    object_key: ObjectKey
    sha256: Digest
    runs: Annotated[StrictInt, Field(ge=1, le=MAX_MASK_RUNS)]
    bytes: Annotated[StrictInt, Field(ge=1, le=MAX_RLE_OBJECT_BYTES)]

    @field_validator("size", mode="before")
    @classmethod
    def strict_size_tuple(cls, value):
        return _two_int_tuple(value, "size")

    @field_validator("size")
    @classmethod
    def bounded_size(cls, value):
        height, width = value
        if height <= 0 or width <= 0:
            raise ValueError("mask size must contain positive [height, width]")
        if height > MAX_MASK_DIMENSION or width > MAX_MASK_DIMENSION:
            raise ValueError(f"mask width / height must be <= {MAX_MASK_DIMENSION}")
        if height * width > MAX_MASK_PIXELS:
            raise ValueError(f"mask pixels must be <= {MAX_MASK_PIXELS}")
        return value

    @model_validator(mode="after")
    def content_address_matches_digest(self):
        gzip_key = self.object_key.endswith(".json.gz")
        suffix = ".json.gz" if gzip_key else ".json"
        expected = (
            f"{MASK_PREFIX}/{self.sha256[:2]}/{self.sha256[2:4]}/{self.sha256}{suffix}"
        )
        if self.object_key != expected:
            raise ValueError("mask object_key must match the content digest")
        if self.storage_encoding == "gzip" and not gzip_key:
            raise ValueError("gzip mask reference must use .json.gz")
        if self.storage_encoding == "identity" and gzip_key:
            raise ValueError("identity mask reference must use .json")
        if self.encoding == "coco_rle_gzip" and not gzip_key:
            raise ValueError("legacy coco_rle_gzip reference must use .json.gz")
        if (
            gzip_key
            and self.encoding == "coco_rle_ref"
            and self.storage_encoding != "gzip"
        ):
            raise ValueError(
                ".json.gz mask reference must declare storage_encoding=gzip"
            )
        return self


class PutRequest(Value):
    content: RleContent
    storage_encoding: Literal["identity", "gzip"] = "identity"


class PutResult(Value):
    reference: MaskReference


class GetRequest(Value):
    reference: MaskReference


class GetResult(Value):
    reference: MaskReference
    content: RleContent


class ListRequest(Value):
    cursor: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE


class Candidate(Value):
    key: ObjectKey
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Annotated[StrictInt, Field(ge=0)]
    last_modified: AwareDatetime

    @field_validator("key")
    @classmethod
    def canonical_content_key(cls, value):
        return validate_object_key(value)

    @field_validator("last_modified", mode="before")
    @classmethod
    def parse_wire_timestamp(cls, value):
        if isinstance(value, str):
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            return datetime.fromisoformat(normalized)
        return value


class CandidatePage(Value):
    items: tuple[Candidate, ...]
    cursor: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = None

    @field_validator("items", mode="before")
    @classmethod
    def list_to_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def bounded_page(self):
        if len(self.items) > MAX_LIST_PAGE:
            raise ValueError("candidate page exceeds the maximum page size")
        return self


class DeleteRequest(Value):
    object_key: ObjectKey
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    last_modified: AwareDatetime

    @field_validator("object_key")
    @classmethod
    def canonical_content_key(cls, value):
        return validate_object_key(value)

    @field_validator("last_modified", mode="before")
    @classmethod
    def parse_wire_timestamp(cls, value):
        if isinstance(value, str):
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            return datetime.fromisoformat(normalized)
        return value


class DeleteResult(Value):
    object_key: ObjectKey
    deleted: bool


class MaskContentFailure(Exception):
    """Closed protocol error; provider details are never part of this value."""

    def __init__(self, code: ErrorCode):
        if code not in ErrorCode.__args__:
            code = "invalid_response"
        self.code = code
        super().__init__(code)


class _Error(Value):
    code: ErrorCode


class _SuccessEnvelope(Value):
    schema_: Literal["aap-assets-mask-content/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: dict


class _ErrorEnvelope(Value):
    schema_: Literal["aap-assets-mask-content/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: _Error


Request = PutRequest | GetRequest | ListRequest | DeleteRequest
Result = PutResult | GetResult | CandidatePage | DeleteResult
REQUEST_TYPES: dict[str, type[BaseModel]] = {
    "put": PutRequest,
    "get": GetRequest,
    "list": ListRequest,
    "delete": DeleteRequest,
}
RESULT_TYPES: dict[str, type[BaseModel]] = {
    "put": PutResult,
    "get": GetResult,
    "list": CandidatePage,
    "delete": DeleteResult,
}
OPERATION_ROLES: dict[str, Literal["content", "cleanup"]] = {
    "put": "content",
    "get": "content",
    "list": "cleanup",
    "delete": "cleanup",
}

_T = TypeVar("_T", bound=BaseModel)


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_json(raw: bytes):
    if not isinstance(raw, bytes):
        raise ValueError("JSON input must be bytes")
    return json.loads(
        raw.decode("utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )


def json_bytes(value) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def parse_reply(raw: bytes, result_type: type[_T]) -> _T:
    try:
        document = parse_json(raw)
        if not isinstance(document, dict) or document.get("schema") != CONTENT_SCHEMA:
            raise ValueError("wrong Mask content schema")
        if document.get("ok") is True:
            envelope = _SuccessEnvelope.model_validate(document)
            return result_type.model_validate(envelope.data)
        if document.get("ok") is False:
            envelope = _ErrorEnvelope.model_validate(document)
            raise MaskContentFailure(envelope.error.code)
        raise ValueError("invalid Mask content envelope")
    except MaskContentFailure:
        raise
    except (ValueError, TypeError, RecursionError):
        raise MaskContentFailure("invalid_response") from None


def error_envelope(code: ErrorCode) -> dict:
    return {
        "schema": CONTENT_SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


def success_envelope(result: BaseModel) -> dict:
    return {
        "schema": CONTENT_SCHEMA,
        "ok": True,
        "data": result.model_dump(mode="json", by_alias=True),
    }


def http_error_status(code: ErrorCode) -> int:
    return {
        "invalid_request": 422,
        "invalid_reference": 422,
        "object_missing": 404,
        "object_corrupt": 409,
        "content_changed": 409,
        "resource_limit": 413,
        "storage_unavailable": 503,
        "invalid_response": 502,
        "permission_denied": 403,
    }[code]


def contract_document() -> dict:
    models = (
        RleContent,
        MaskReference,
        PutRequest,
        PutResult,
        GetRequest,
        GetResult,
        ListRequest,
        Candidate,
        CandidatePage,
        DeleteRequest,
        DeleteResult,
        _Error,
    )
    definitions = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            definitions[name] = definition
        definitions[model.__name__] = schema
    requests = ["PutRequest", "GetRequest", "ListRequest", "DeleteRequest"]
    results = ["PutResult", "GetResult", "CandidatePage", "DeleteResult"]
    error_envelope = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "error"],
        "properties": {
            "schema": {"const": CONTENT_SCHEMA},
            "ok": {"const": False},
            "error": {"$ref": "#/$defs/_Error"},
        },
    }
    success_envelope = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "data"],
        "properties": {
            "schema": {"const": CONTENT_SCHEMA},
            "ok": {"const": True},
            "data": {"oneOf": [{"$ref": f"#/$defs/{name}"} for name in results]},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://schemas.aap.invalid/assets-mask-content/v1",
        "title": "AAP Mask content request and response messages",
        "oneOf": [
            *[{"$ref": f"#/$defs/{name}"} for name in requests],
            success_envelope,
            error_envelope,
        ],
        "x-aap-schema": CONTENT_SCHEMA,
        "x-operation-roles": OPERATION_ROLES,
        "x-budgets": {
            "request_bytes": MAX_REQUEST_BYTES,
            "response_bytes": MAX_RESPONSE_BYTES,
            "canonical_bytes": MAX_RLE_OBJECT_BYTES,
            "compressed_bytes": MAX_COMPRESSED_BYTES,
            "expansion_ratio": MAX_EXPANSION_RATIO,
            "max_runs": MAX_MASK_RUNS,
            "max_dimension": MAX_MASK_DIMENSION,
            "max_pixels": MAX_MASK_PIXELS,
            "max_list_page": MAX_LIST_PAGE,
        },
        "$defs": definitions,
    }


def openapi_document() -> dict:
    models = (
        RleContent,
        MaskReference,
        PutRequest,
        PutResult,
        GetRequest,
        GetResult,
        ListRequest,
        Candidate,
        CandidatePage,
        DeleteRequest,
        DeleteResult,
        _Error,
    )
    definitions = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("conflicting Mask content schema definition")
            definitions[name] = definition
        name = model.__name__
        if name in definitions and definitions[name] != schema:
            raise ValueError("conflicting Mask content schema definition")
        definitions[name] = schema
    error = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "error"],
        "properties": {
            "schema": {"const": CONTENT_SCHEMA},
            "ok": {"const": False},
            "error": {"$ref": "#/components/schemas/_Error"},
        },
    }

    def add(operation: str, request_name: str, result_name: str, role: str):
        paths[f"/v1/mask-content/{operation}"] = {
            "post": {
                "operationId": f"{operation}MaskContent",
                "x-required-process-role": role,
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
                        "description": "Mask content result",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["schema", "ok", "data"],
                                    "properties": {
                                        "schema": {"const": CONTENT_SCHEMA},
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
                        "description": "Mask content error",
                        "content": {"application/json": {"schema": error}},
                    },
                },
            }
        }

    paths = {}
    add("put", "PutRequest", "PutResult", "content")
    add("get", "GetRequest", "GetResult", "content")
    add("list", "ListRequest", "CandidatePage", "cleanup")
    add("delete", "DeleteRequest", "DeleteResult", "cleanup")
    return {
        "openapi": "3.1.0",
        "info": {"title": "AAP Mask content", "version": "1"},
        "security": [{"serviceToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": definitions,
        },
        "x-aap-schema": CONTENT_SCHEMA,
        "x-operation-roles": OPERATION_ROLES,
        "x-budgets": contract_document()["x-budgets"],
    }
