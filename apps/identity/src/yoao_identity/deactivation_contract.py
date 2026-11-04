"""Closed wire/native contracts for self-deactivation and due processing."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .account_contract import AccountRecord, _same_fenced_credential
from .contract import (
    ClosedModel,
    FailureCode,
    IdentityUnavailable,
    Principal,
    _reject_protocol_alias,
)
from .session_contract import SessionAuditContext

DEACTIVATION_PROTOCOL = "aap-identity-deactivation/v1"
NATIVE_DEACTIVATION_PROTOCOL = "aap-identity-deactivation-native/v1"
DEACTIVATION_DERIVATION = "aap-identity-deactivation/v1"
DEACTIVATION_POLICY_PROTOCOL = "aap-identity-deactivation-policy/v1"
DUE_PROTOCOL = "aap-identity-deactivation-due/v1"
NATIVE_DUE_PROTOCOL = "aap-identity-deactivation-due-native/v1"
DUE_POLICY_PROTOCOL = "aap-identity-deactivation-due-policy/v1"

DeactivationOperation: TypeAlias = Literal[
    "request_self_deactivation", "cancel_self_deactivation"
]
DeactivationFailureCode: TypeAlias = (
    FailureCode
    | Literal["account_busy", "invariant_busy", "operation_conflict", "policy_rejected"]
)
DueFailureCode: TypeAlias = Literal[
    "invalid_request", "invariant_busy", "operation_conflict"
]

_HEX_64 = r"^[0-9a-f]{64}$"
_REQUEST_PENDING = "已有进行中的注销申请"
_CANCEL_NOT_PENDING = "当前无待生效的注销申请"
_LAST_SUPER_ADMIN = "您是最后一名超级管理员，请先指定继任者再申请注销"
_OTHER_ADMINS_RESERVED = (
    "其余超级管理员均已申请注销，请先让其他管理员撤销申请或指定继任者"
)
_POLICY_DETAILS = {
    _REQUEST_PENDING,
    _CANCEL_NOT_PENDING,
    _LAST_SUPER_ADMIN,
    _OTHER_ADMINS_RESERVED,
}


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
        value = json.loads(
            payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        _reject_protocol_alias(value)
        return payload
    value = (
        payload.model_dump(mode="json", by_alias=True)
        if isinstance(payload, ClosedModel)
        else payload
    )
    _reject_protocol_alias(value)
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


class RequestDeactivationPayload(ClosedModel):
    reason: str | None = Field(max_length=500, strict=True)


class CancelDeactivationPayload(ClosedModel):
    pass


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-deactivation/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class RequestDeactivationRequest(_WireRequest):
    operation: Literal["request_self_deactivation"]
    payload: RequestDeactivationPayload


class CancelDeactivationRequest(_WireRequest):
    operation: Literal["cancel_self_deactivation"]
    payload: CancelDeactivationPayload


DeactivationRequest: TypeAlias = Annotated[
    RequestDeactivationRequest | CancelDeactivationRequest,
    Field(discriminator="operation"),
]
_DEACTIVATION_REQUESTS = TypeAdapter(DeactivationRequest)


def decode_deactivation_request(payload: dict | bytes) -> DeactivationRequest:
    """Decode one private user lifecycle command with duplicate-key rejection."""
    return _DEACTIVATION_REQUESTS.validate_json(_json_bytes(payload))


class DeactivationResultData(ClosedModel):
    account: AccountRecord


class _DeactivationReplyBase(ClosedModel):
    protocol: Literal["aap-identity-deactivation/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class RequestDeactivationReply(_DeactivationReplyBase):
    operation: Literal["request_self_deactivation"]
    data: DeactivationResultData


class CancelDeactivationReply(_DeactivationReplyBase):
    operation: Literal["cancel_self_deactivation"]
    data: DeactivationResultData


DeactivationReply: TypeAlias = Annotated[
    RequestDeactivationReply | CancelDeactivationReply,
    Field(discriminator="operation"),
]
_DEACTIVATION_REPLIES = TypeAdapter(DeactivationReply)


class DeactivationFailureDetail(ClosedModel):
    code: DeactivationFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: str | None = Field(strict=True)

    @model_validator(mode="after")
    def fields_match_code(self) -> DeactivationFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Deactivation failure requires status_code and detail")
        if self.code == "policy_rejected":
            if self.status_code != 400 or self.detail not in _POLICY_DETAILS:
                raise ValueError("Invalid self-deactivation policy failure")
        elif self.status_code is not None or self.detail is not None:
            raise ValueError("Only policy rejections carry status and detail")
        return self


class DeactivationFailureReply(ClosedModel):
    protocol: Literal["aap-identity-deactivation/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DeactivationFailureDetail


class DeactivationFailure(Exception):
    def __init__(
        self,
        code: DeactivationFailureCode,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        parsed = DeactivationFailureDetail(
            code=code, status_code=status_code, detail=detail
        )
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = parsed.detail
        super().__init__(code)


def deactivation_failure_reply(
    failure: DeactivationFailure | DeactivationFailureCode,
) -> dict:
    if isinstance(failure, DeactivationFailure):
        code, status_code, detail = (
            failure.code,
            failure.status_code,
            failure.detail,
        )
    else:
        code, status_code, detail = failure, None, None
    return DeactivationFailureReply(
        schema=DEACTIVATION_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    ).model_dump(mode="json", by_alias=True)


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-deactivation-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=_HEX_64, strict=True, repr=False)
    derivation_id: str = Field(pattern=_HEX_64, strict=True)
    audit_context: SessionAuditContext

    @model_validator(mode="after")
    def credential_hash_shape(self):
        if (self.actor.credential_type == "api_key") != (
            self.expected_key_hash is not None
        ):
            raise ValueError("API-key hash must match the credential kind")
        return self


class NativeRequestDeactivationCommand(_NativeCommandBase):
    operation: Literal["request_self_deactivation"]
    payload: RequestDeactivationPayload


class NativeCancelDeactivationCommand(_NativeCommandBase):
    operation: Literal["cancel_self_deactivation"]
    payload: CancelDeactivationPayload


NativeDeactivationCommand: TypeAlias = Annotated[
    NativeRequestDeactivationCommand | NativeCancelDeactivationCommand,
    Field(discriminator="operation"),
]
_NATIVE_DEACTIVATION_COMMANDS = TypeAdapter(NativeDeactivationCommand)


class _NativeReplyBase(ClosedModel):
    protocol: Literal["aap-identity-deactivation-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class NativeRequestDeactivationReply(_NativeReplyBase):
    operation: Literal["request_self_deactivation"]
    data: DeactivationResultData


class NativeCancelDeactivationReply(_NativeReplyBase):
    operation: Literal["cancel_self_deactivation"]
    data: DeactivationResultData


NativeDeactivationSuccess: TypeAlias = Annotated[
    NativeRequestDeactivationReply | NativeCancelDeactivationReply,
    Field(discriminator="operation"),
]
_NATIVE_DEACTIVATION_REPLIES = TypeAdapter(NativeDeactivationSuccess)


class NativeDeactivationFailureReply(ClosedModel):
    protocol: Literal["aap-identity-deactivation-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DeactivationFailureDetail


class DeactivationPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-deactivation-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-deactivation/v1"]


def native_deactivation_command(
    request: DeactivationRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativeDeactivationCommand:
    values = request.model_dump(mode="json", by_alias=True)
    values.update(
        {
            "schema": NATIVE_DEACTIVATION_PROTOCOL,
            "actor": actor.model_dump(mode="json"),
            "expected_key_hash": expected_key_hash,
            "request_fingerprint": request_fingerprint,
            "derivation_id": derivation_id,
        }
    )
    values.pop("token", None)
    try:
        encoded = json.dumps(
            values, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        return _NATIVE_DEACTIVATION_COMMANDS.validate_json(encoded)
    except (ValidationError, ValueError, TypeError):
        raise IdentityUnavailable() from None


def decode_native_deactivation_command(
    payload: dict | bytes,
) -> NativeDeactivationCommand:
    try:
        return _NATIVE_DEACTIVATION_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def _policy_error_matches(command, error: DeactivationFailureDetail) -> bool:
    if error.code != "policy_rejected":
        return True
    if command.operation == "request_self_deactivation":
        return error.detail in {
            _REQUEST_PENDING,
            _LAST_SUPER_ADMIN,
            _OTHER_ADMINS_RESERVED,
        }
    return error.detail == _CANCEL_NOT_PENDING


def _decode_failure(
    payload: dict | bytes | ClosedModel,
    *,
    command: _WireRequest | _NativeCommandBase,
    native: bool,
) -> DeactivationFailure:
    model = NativeDeactivationFailureReply if native else DeactivationFailureReply
    try:
        reply = model.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not _policy_error_matches(command, reply.error):
        raise IdentityUnavailable()
    if isinstance(command, _WireRequest) and reply.error.detail is not None:
        if command.token in reply.error.detail:
            raise IdentityUnavailable()
    raise DeactivationFailure(
        reply.error.code, reply.error.status_code, reply.error.detail
    )


def _validate_deactivation_success(command, reply) -> None:
    if (
        reply.operation != command.operation
        or reply.operation_id != command.operation_id
    ):
        raise IdentityUnavailable()
    account = reply.data.account
    if account.id != reply.principal.actor_id or account.is_active is not True:
        raise IdentityUnavailable()
    requested = account.deactivation_requested_at
    scheduled = account.deactivation_scheduled_at
    if command.operation == "request_self_deactivation":
        if (
            requested is None
            or scheduled is None
            or scheduled - requested != timedelta(days=7)
        ):
            raise IdentityUnavailable()
    elif requested is not None or scheduled is not None:
        raise IdentityUnavailable()


def _decode_success(
    command: _WireRequest | _NativeCommandBase,
    payload: dict | bytes | ClosedModel,
    *,
    native: bool,
    expected_principal: Principal | None,
):
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
        raise IdentityUnavailable()
    if raw["ok"] is False:
        return _decode_failure(encoded, command=command, native=native)
    adapter = _NATIVE_DEACTIVATION_REPLIES if native else _DEACTIVATION_REPLIES
    try:
        reply = adapter.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    _validate_deactivation_success(command, reply)
    if expected_principal is not None and not _same_fenced_credential(
        reply.principal, expected_principal
    ):
        raise IdentityUnavailable()
    return reply


def decode_native_deactivation_reply(
    command: NativeDeactivationCommand, payload: dict | bytes | ClosedModel
) -> NativeDeactivationSuccess:
    reply = _decode_success(
        command, payload, native=True, expected_principal=command.actor
    )
    if isinstance(reply, DeactivationFailure):
        raise reply
    return reply


def decode_deactivation_reply(
    request: DeactivationRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal | None = None,
) -> DeactivationReply:
    reply = _decode_success(
        request, payload, native=False, expected_principal=expected_principal
    )
    if isinstance(reply, DeactivationFailure):
        raise reply
    return reply


class DeactivationDueRequest(ClosedModel):
    protocol: Literal["aap-identity-deactivation-due/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    limit: int = Field(ge=1, le=500, strict=True)


class DeactivationDueSuccess(ClosedModel):
    protocol: Literal["aap-identity-deactivation-due/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    processed: int = Field(ge=0, strict=True)
    examined: int = Field(ge=0, strict=True)
    blocked_last_super_admin: int = Field(ge=0, strict=True)
    has_more: bool

    @field_validator("has_more", mode="before")
    @classmethod
    def strict_has_more(cls, value):
        if type(value) is not bool:
            raise ValueError("has_more must be a boolean")
        return value

    @model_validator(mode="after")
    def counts_match(self) -> DeactivationDueSuccess:
        if self.processed + self.blocked_last_super_admin != self.examined:
            raise ValueError("Due counts do not reconcile")
        return self


class DeactivationDueReply(DeactivationDueSuccess):
    pass


class DeactivationDueFailureDetail(ClosedModel):
    code: DueFailureCode


class DeactivationDueFailureReply(ClosedModel):
    protocol: Literal["aap-identity-deactivation-due/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DeactivationDueFailureDetail


class DeactivationDueFailure(Exception):
    def __init__(self, code: DueFailureCode):
        self.code = DeactivationDueFailureDetail(code=code).code
        super().__init__(code)


def deactivation_due_failure_reply(
    failure: DeactivationDueFailure | DueFailureCode,
) -> dict:
    code = failure.code if isinstance(failure, DeactivationDueFailure) else failure
    return DeactivationDueFailureReply(
        schema=DUE_PROTOCOL, ok=False, error={"code": code}
    ).model_dump(mode="json", by_alias=True)


def decode_deactivation_due_request(
    payload: dict | bytes,
) -> DeactivationDueRequest:
    try:
        return DeactivationDueRequest.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise


class NativeDeactivationDueCommand(ClosedModel):
    protocol: Literal["aap-identity-deactivation-due-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    limit: int = Field(ge=1, le=500, strict=True)


class NativeDeactivationDueSuccess(ClosedModel):
    protocol: Literal["aap-identity-deactivation-due-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    processed: int = Field(ge=0, strict=True)
    examined: int = Field(ge=0, strict=True)
    blocked_last_super_admin: int = Field(ge=0, strict=True)
    has_more: bool

    @field_validator("has_more", mode="before")
    @classmethod
    def strict_native_has_more(cls, value):
        if type(value) is not bool:
            raise ValueError("has_more must be a boolean")
        return value

    @model_validator(mode="after")
    def native_counts_match(self) -> NativeDeactivationDueSuccess:
        if self.processed + self.blocked_last_super_admin != self.examined:
            raise ValueError("Due counts do not reconcile")
        return self


class NativeDeactivationDueFailureReply(ClosedModel):
    protocol: Literal["aap-identity-deactivation-due-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DeactivationDueFailureDetail


class DeactivationDuePolicyReply(ClosedModel):
    protocol: Literal["aap-identity-deactivation-due-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-deactivation-due/v1"]


def native_deactivation_due_command(
    request: DeactivationDueRequest,
) -> NativeDeactivationDueCommand:
    return NativeDeactivationDueCommand(
        schema=NATIVE_DUE_PROTOCOL,
        operation_id=request.operation_id,
        limit=request.limit,
    )


def decode_native_deactivation_due_reply(
    request: DeactivationDueRequest,
    payload: dict | bytes | ClosedModel,
) -> NativeDeactivationDueSuccess:
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"] is False:
            failure = NativeDeactivationDueFailureReply.model_validate_json(encoded)
            raise DeactivationDueFailure(failure.error.code)
        reply = NativeDeactivationDueSuccess.model_validate_json(encoded)
        if reply.operation_id != request.operation_id or reply.examined > request.limit:
            raise IdentityUnavailable()
        return reply
    except DeactivationDueFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def decode_deactivation_due_reply(
    request: DeactivationDueRequest,
    payload: dict | bytes | ClosedModel,
) -> DeactivationDueReply:
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"] is False:
            failure = DeactivationDueFailureReply.model_validate_json(encoded)
            raise DeactivationDueFailure(failure.error.code)
        reply = DeactivationDueReply.model_validate_json(encoded)
        if reply.operation_id != request.operation_id or reply.examined > request.limit:
            raise IdentityUnavailable()
        return reply
    except DeactivationDueFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def decode_deactivation_policy(
    payload: dict | bytes | ClosedModel,
) -> DeactivationPolicyReply:
    try:
        return DeactivationPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_deactivation_due_policy(
    payload: dict | bytes | ClosedModel,
) -> DeactivationDuePolicyReply:
    try:
        return DeactivationDuePolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


# Short aliases keep the operator names aligned with the user-facing API port.
decode_due_request = decode_deactivation_due_request
decode_due_reply = decode_deactivation_due_reply
decode_native_due_reply = decode_native_deactivation_due_reply
