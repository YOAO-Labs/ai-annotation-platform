"""Strict request and response values for private Project guide-object I/O."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import models_json_schema

GUIDE_OBJECT_SCHEMA = "aap-assets-guide-object/v1"
GuideObjectOperation = Literal["presign_put", "head", "delete", "presign_get"]
GuideObjectFailureCode = Literal[
    "storage_error",
    "invalid_request",
    "unavailable",
    "busy",
]

_FAILURE_CODES = frozenset(GuideObjectFailureCode.__args__)
_GUIDE_KEY_PATTERN = r"^projects/[0-9a-f-]{36}/guide/[0-9a-f-]{36}-[^/]+$"
_GUIDE_KEY_RE = re.compile(_GUIDE_KEY_PATTERN)

GuideObjectKey = Annotated[StrictStr, Field(pattern=_GUIDE_KEY_PATTERN)]
PutExpiry = Annotated[StrictInt, Field(ge=900, le=900)]
GetExpiry = Annotated[StrictInt, Field(ge=3600, le=3600)]
SignedUrl = Annotated[StrictStr, Field(min_length=1)]


class _Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class _ScopedValue(_Value):
    schema_version: Literal[GUIDE_OBJECT_SCHEMA] = Field(alias="schema")
    request_id: UUID
    operation: GuideObjectOperation
    project_id: UUID
    key: GuideObjectKey

    @field_validator("request_id", "project_id", mode="before")
    @classmethod
    def require_canonical_uuid(cls, value: object) -> UUID:
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("UUID values must be canonical UUID text")
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError):
            raise ValueError("UUID values must be canonical UUID text") from None
        if str(parsed) != value:
            raise ValueError("UUID values must be canonical UUID text")
        return parsed

    @model_validator(mode="after")
    def require_project_guide_key(self):
        if not _GUIDE_KEY_RE.fullmatch(self.key) or not self.key.startswith(
            f"projects/{self.project_id}/guide/"
        ):
            raise ValueError("Guide object key does not match its project scope")
        return self


class GuideObjectRequest(_ScopedValue):
    content_type: StrictStr | None

    @model_validator(mode="after")
    def require_operation_content_type(self):
        if self.operation == "presign_put":
            if self.content_type is None:
                raise ValueError("presign_put requires a content type")
        elif self.content_type is not None:
            raise ValueError("only presign_put accepts a content type")
        return self


class GuideObjectPresignPutResult(_Value):
    url: SignedUrl
    expires_in: PutExpiry


class GuideObjectHeadResult(_Value):
    content_length: StrictInt | None


class GuideObjectDeleteResult(_Value):
    deleted: Literal[True]

    @field_validator("deleted", mode="before")
    @classmethod
    def require_true_boolean(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("deleted must be the boolean true")
        return True


class GuideObjectPresignGetResult(_Value):
    url: SignedUrl
    expires_in: GetExpiry


class _GuideObjectSuccess(_ScopedValue):
    ok: Literal[True]

    @field_validator("ok", mode="before")
    @classmethod
    def require_true_boolean(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("ok must be the boolean true")
        return True


class GuideObjectPresignPutReply(_GuideObjectSuccess):
    operation: Literal["presign_put"]
    result: GuideObjectPresignPutResult


class GuideObjectHeadReply(_GuideObjectSuccess):
    operation: Literal["head"]
    result: GuideObjectHeadResult


class GuideObjectDeleteReply(_GuideObjectSuccess):
    operation: Literal["delete"]
    result: GuideObjectDeleteResult


class GuideObjectPresignGetReply(_GuideObjectSuccess):
    operation: Literal["presign_get"]
    result: GuideObjectPresignGetResult


class GuideObjectError(_Value):
    code: GuideObjectFailureCode


class GuideObjectFailureReply(_ScopedValue):
    ok: Literal[False]
    error: GuideObjectError

    @field_validator("ok", mode="before")
    @classmethod
    def require_false_boolean(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("ok must be the boolean false")
        return False


GuideObjectSuccessReply: TypeAlias = (
    GuideObjectPresignPutReply
    | GuideObjectHeadReply
    | GuideObjectDeleteReply
    | GuideObjectPresignGetReply
)
GuideObjectSuccess: TypeAlias = GuideObjectSuccessReply
GuideObjectReply: TypeAlias = GuideObjectSuccessReply | GuideObjectFailureReply
_REPLY_ADAPTER = TypeAdapter(GuideObjectReply)


class GuideObjectFailure(Exception):
    """A bounded private failure code without storage-provider details."""

    def __init__(self, code: GuideObjectFailureCode):
        if not isinstance(code, str) or code not in _FAILURE_CODES:
            raise ValueError("Unknown guide object failure code")
        super().__init__(code)
        self.code = code


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object member")
        result[key] = value
    return result


def _decode_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=True)
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(
                value,
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (ValueError, UnicodeError, TypeError):
            raise ValueError("Malformed guide object JSON") from None
        if isinstance(value, dict):
            return value
    raise ValueError("Guide object value must be an object")


def _validate_request(value: object) -> GuideObjectRequest:
    try:
        return GuideObjectRequest.model_validate(_decode_value(value))
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Invalid guide object request") from None


def decode_guide_object_request(value: object) -> GuideObjectRequest:
    """Decode a closed request, rejecting UUID coercion and cross-project keys."""
    return _validate_request(value)


def guide_object_success_reply(
    request: GuideObjectRequest, result: object
) -> GuideObjectSuccess:
    """Build a strict operation-specific success envelope for a valid request."""
    request = _validate_request(request)
    result_models = {
        "presign_put": (GuideObjectPresignPutResult, GuideObjectPresignPutReply),
        "head": (GuideObjectHeadResult, GuideObjectHeadReply),
        "delete": (GuideObjectDeleteResult, GuideObjectDeleteReply),
        "presign_get": (GuideObjectPresignGetResult, GuideObjectPresignGetReply),
    }
    result_type, reply_type = result_models[request.operation]
    try:
        typed_result = result_type.model_validate(result)
        reply = reply_type.model_validate(
            {
                "schema": GUIDE_OBJECT_SCHEMA,
                "request_id": request.request_id,
                "operation": request.operation,
                "project_id": request.project_id,
                "key": request.key,
                "ok": True,
                "result": typed_result,
            }
        )
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Invalid guide object success result") from None
    return reply


def guide_object_failure_reply(
    request: GuideObjectRequest, code: GuideObjectFailureCode
) -> GuideObjectFailureReply:
    """Build a scope-bound private failure envelope without provider details."""
    request = _validate_request(request)
    failure = GuideObjectFailure(code)
    reply = GuideObjectFailureReply(
        schema=GUIDE_OBJECT_SCHEMA,
        request_id=request.request_id,
        operation=request.operation,
        project_id=request.project_id,
        key=request.key,
        ok=False,
        error={"code": failure.code},
    )
    return reply


def decode_guide_object_reply(
    request: GuideObjectRequest, value: object
) -> GuideObjectSuccess:
    """Decode a reply, bind all request scope, and raise a typed remote failure."""
    request = _validate_request(request)
    payload = _decode_value(value)
    try:
        reply = _REPLY_ADAPTER.validate_python(payload)
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Invalid guide object response") from None

    if (
        reply.schema_version != GUIDE_OBJECT_SCHEMA
        or reply.request_id != request.request_id
        or reply.operation != request.operation
        or reply.project_id != request.project_id
        or reply.key != request.key
    ):
        raise ValueError("Guide object response escaped its request scope")
    if isinstance(reply, GuideObjectFailureReply):
        raise GuideObjectFailure(reply.error.code)
    return reply


def guide_object_json_schema() -> dict[str, Any]:
    """Return the distributable JSON Schema for the exact private wire types."""
    models = (
        GuideObjectRequest,
        GuideObjectPresignPutResult,
        GuideObjectHeadResult,
        GuideObjectDeleteResult,
        GuideObjectPresignGetResult,
        GuideObjectPresignPutReply,
        GuideObjectHeadReply,
        GuideObjectDeleteReply,
        GuideObjectPresignGetReply,
        GuideObjectError,
        GuideObjectFailureReply,
    )
    _, schema = models_json_schema([(model, "validation") for model in models])
    definitions = schema["$defs"]
    definitions["GuideObjectRequest"]["allOf"] = [
        {
            "if": {
                "properties": {"operation": {"const": "presign_put"}},
                "required": ["operation"],
            },
            "then": {"properties": {"content_type": {"type": "string"}}},
            "else": {"properties": {"content_type": {"type": "null"}}},
        }
    ]
    replies = (
        "GuideObjectPresignPutReply",
        "GuideObjectHeadReply",
        "GuideObjectDeleteReply",
        "GuideObjectPresignGetReply",
        "GuideObjectFailureReply",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:aap:assets-guide-object:v1",
        "title": GUIDE_OBJECT_SCHEMA,
        "schema": GUIDE_OBJECT_SCHEMA,
        "failure_codes": list(GuideObjectFailureCode.__args__),
        "key_policy": {
            "pattern": _GUIDE_KEY_PATTERN,
            "project_prefix": "projects/{project_id}/guide/",
            "uuid_text": "canonical-lowercase",
        },
        "request": {"$ref": "#/$defs/GuideObjectRequest"},
        "reply": {"oneOf": [{"$ref": "#/$defs/" + name} for name in replies]},
        "operations": {
            "presign_put": {
                "request": "GuideObjectRequest",
                "result": "GuideObjectPresignPutResult",
                "content_type": "required-string",
                "expires_in": 900,
            },
            "head": {
                "request": "GuideObjectRequest",
                "result": "GuideObjectHeadResult",
                "content_type": "null",
            },
            "delete": {
                "request": "GuideObjectRequest",
                "result": "GuideObjectDeleteResult",
                "content_type": "null",
            },
            "presign_get": {
                "request": "GuideObjectRequest",
                "result": "GuideObjectPresignGetResult",
                "content_type": "null",
                "expires_in": 3600,
            },
        },
        "$defs": definitions,
    }
