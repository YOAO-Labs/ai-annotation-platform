"""Closed wire/native contracts for batched UserBrief reads."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, ValidationError

from .contract import ClosedModel, IdentityUnavailable, _reject_protocol_alias

BRIEFS_PROTOCOL = "aap-identity-briefs/v1"
NATIVE_BRIEFS_PROTOCOL = "aap-identity-briefs-native/v1"
BRIEFS_POLICY_PROTOCOL = "aap-identity-briefs-policy/v1"

BriefsOperation: TypeAlias = Literal["get_user_briefs", "get_project_user_briefs"]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def _assert_json_value(value: object) -> None:
    if isinstance(value, ClosedModel):
        for child in value.__dict__.values():
            _assert_json_value(child)
    elif isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for child in value.values():
            _assert_json_value(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_json_value(child)
    elif type(value) is float and (
        value != value or value in (float("inf"), float("-inf"))
    ):
        raise ValueError("Non-finite JSON number")
    elif value is not None and type(value) not in (str, bool, int, float):
        raise ValueError("Invalid JSON value")


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


class GetUserBriefsPayload(ClosedModel):
    user_ids: list[uuid.UUID]


class GetProjectUserBriefsPayload(ClosedModel):
    project_id: uuid.UUID
    user_ids: list[uuid.UUID]


class _WireRequestBase(ClosedModel):
    protocol: Literal["aap-identity-briefs/v1"] = Field(alias="schema")


class GetUserBriefsRequest(_WireRequestBase):
    operation: Literal["get_user_briefs"]
    payload: GetUserBriefsPayload


class GetProjectUserBriefsRequest(_WireRequestBase):
    operation: Literal["get_project_user_briefs"]
    payload: GetProjectUserBriefsPayload


BriefsRequest: TypeAlias = Annotated[
    GetUserBriefsRequest | GetProjectUserBriefsRequest,
    Field(discriminator="operation"),
]
_BRIEFS_REQUESTS = TypeAdapter(BriefsRequest)


def decode_briefs_request(payload: dict | bytes) -> BriefsRequest:
    try:
        return _BRIEFS_REQUESTS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise BriefsFailure("invalid_request") from None


class NativeBriefsUserRecord(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    email: str = Field(strict=True)
    role: str | None = Field(strict=True)
    avatar_ref: str | None = Field(strict=True)


class BriefsUserRecord(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    email: str = Field(strict=True)
    role: str | None = Field(strict=True)
    avatar_initial: str = Field(strict=True)
    avatar_ref: str | None = Field(strict=True)


class BriefsReplyData(ClosedModel):
    project_id: uuid.UUID | None
    users: list[BriefsUserRecord]


class _WireSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-briefs/v1"] = Field(alias="schema")
    ok: Literal[True]


class GetUserBriefsReply(_WireSuccessBase):
    operation: Literal["get_user_briefs"]
    data: BriefsReplyData


class GetProjectUserBriefsReply(_WireSuccessBase):
    operation: Literal["get_project_user_briefs"]
    data: BriefsReplyData


BriefsReply: TypeAlias = Annotated[
    GetUserBriefsReply | GetProjectUserBriefsReply,
    Field(discriminator="operation"),
]
_BRIEFS_REPLIES = TypeAdapter(BriefsReply)


class BriefsFailureDetail(ClosedModel):
    code: Literal["invalid_request"]


class BriefsFailureReply(ClosedModel):
    protocol: Literal["aap-identity-briefs/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: BriefsFailureDetail


class BriefsFailure(Exception):
    def __init__(self, code: Literal["invalid_request"]):
        self.code = code
        super().__init__(code)


def briefs_failure_reply(
    failure: BriefsFailure | Literal["invalid_request"],
) -> dict:
    code = failure.code if isinstance(failure, BriefsFailure) else failure
    return BriefsFailureReply(
        schema=BRIEFS_PROTOCOL,
        ok=False,
        error={"code": code},
    ).model_dump(mode="json", by_alias=True)


class NativeBriefsCommandBase(ClosedModel):
    protocol: Literal["aap-identity-briefs-native/v1"] = Field(alias="schema")


class NativeGetUserBriefsCommand(NativeBriefsCommandBase):
    operation: Literal["get_user_briefs"]
    payload: GetUserBriefsPayload


class NativeGetProjectUserBriefsCommand(NativeBriefsCommandBase):
    operation: Literal["get_project_user_briefs"]
    payload: GetProjectUserBriefsPayload


NativeBriefsCommand: TypeAlias = Annotated[
    NativeGetUserBriefsCommand | NativeGetProjectUserBriefsCommand,
    Field(discriminator="operation"),
]
_NATIVE_BRIEFS_COMMANDS = TypeAdapter(NativeBriefsCommand)


def native_briefs_command(request: BriefsRequest) -> NativeBriefsCommand:
    values = request.model_dump(mode="json", by_alias=True)
    values["schema"] = NATIVE_BRIEFS_PROTOCOL
    try:
        command = _NATIVE_BRIEFS_COMMANDS.validate_json(
            json.dumps(values, separators=(",", ":"), ensure_ascii=True)
        )
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    return command


def decode_native_briefs_command(
    payload: dict | bytes | ClosedModel,
) -> NativeBriefsCommand:
    try:
        return _NATIVE_BRIEFS_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class NativeBriefsReplyData(ClosedModel):
    project_id: uuid.UUID | None
    users: list[NativeBriefsUserRecord]


class _NativeSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-briefs-native/v1"] = Field(alias="schema")
    ok: Literal[True]


class NativeGetUserBriefsReply(_NativeSuccessBase):
    operation: Literal["get_user_briefs"]
    data: NativeBriefsReplyData


class NativeGetProjectUserBriefsReply(_NativeSuccessBase):
    operation: Literal["get_project_user_briefs"]
    data: NativeBriefsReplyData


NativeBriefsSuccess: TypeAlias = Annotated[
    NativeGetUserBriefsReply | NativeGetProjectUserBriefsReply,
    Field(discriminator="operation"),
]
_NATIVE_BRIEFS_REPLIES = TypeAdapter(NativeBriefsSuccess)


class NativeBriefsFailureReply(ClosedModel):
    protocol: Literal["aap-identity-briefs-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: BriefsFailureDetail


class BriefsPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-briefs-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-briefs/v1"]


def avatar_initial(name: str, email: str) -> str:
    source = (name or email or "?").strip()
    return source[:1].upper() if source else "?"


def _request_scope(request: BriefsRequest) -> tuple[uuid.UUID | None, set[uuid.UUID]]:
    if request.operation == "get_user_briefs":
        return None, set(request.payload.user_ids)
    return request.payload.project_id, set(request.payload.user_ids)


def _validate_scope_and_records(request, reply, *, native: bool) -> None:
    if request.operation != reply.operation:
        raise IdentityUnavailable()
    expected_project_id, requested_ids = _request_scope(request)
    if reply.data.project_id != expected_project_id:
        raise IdentityUnavailable()
    ids = [str(item.id) for item in reply.data.users]
    if ids != sorted(set(ids)):
        raise IdentityUnavailable()
    if any(item.id not in requested_ids for item in reply.data.users):
        raise IdentityUnavailable()
    if not native and any(
        item.avatar_initial != avatar_initial(item.name, item.email)
        for item in reply.data.users
    ):
        raise IdentityUnavailable()


def decode_native_briefs_reply(
    command: NativeBriefsCommand, payload: dict | bytes | ClosedModel
) -> NativeBriefsSuccess:
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise ValueError("Invalid native briefs envelope")
        if raw["ok"] is False:
            failure = NativeBriefsFailureReply.model_validate_json(encoded)
            raise BriefsFailure(failure.error.code)
        reply = _NATIVE_BRIEFS_REPLIES.validate_json(encoded)
    except BriefsFailure:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    _validate_scope_and_records(command, reply, native=True)
    return reply


def decode_briefs_reply(
    request: BriefsRequest, payload: dict | bytes | ClosedModel
) -> BriefsReply:
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise ValueError("Invalid briefs envelope")
        if raw["ok"] is False:
            failure = BriefsFailureReply.model_validate_json(encoded)
            raise BriefsFailure(failure.error.code)
        reply = _BRIEFS_REPLIES.validate_json(encoded)
    except BriefsFailure:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    _validate_scope_and_records(request, reply, native=False)
    return reply


def decode_briefs_policy(payload: dict | bytes | ClosedModel) -> BriefsPolicyReply:
    try:
        return BriefsPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
