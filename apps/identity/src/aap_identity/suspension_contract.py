"""Closed wire/native contracts for administrator suspension and reactivation."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .account_contract import _same_fenced_credential
from .contract import (
    ClosedModel,
    FailureCode,
    IdentityUnavailable,
    Principal,
    _reject_protocol_alias,
)
from .directory_contract import DirectoryUserRecord
from .session_contract import SessionAuditContext

SUSPENSION_PROTOCOL = "aap-identity-suspensions/v1"
NATIVE_SUSPENSION_PROTOCOL = "aap-identity-suspensions-native/v1"
SUSPENSION_POLICY_PROTOCOL = "aap-identity-suspensions-policy/v1"
SUSPENSION_DERIVATION = "aap-identity-suspensions/v1"

SuspensionOperation: TypeAlias = Literal["suspend_account", "reactivate_account"]
SuspensionFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "manager_required",
        "target_not_found",
        "self_suspend",
        "self_reactivate",
        "suspend_role_forbidden",
        "reactivate_role_forbidden",
        "reactivate_not_managed",
        "suspend_scope_forbidden",
        "reactivate_scope_forbidden",
        "last_super_admin",
        "already_active",
        "not_reactivatable",
        "account_busy",
        "project_busy",
        "operation_conflict",
        "unavailable",
    ]
)

_AUTH_FAILURES = {
    "credential_invalid",
    "credential_revoked",
    "generation_outdated",
    "token_expired",
    "user_inactive",
    "invalid_account_role",
    "credential_busy",
}
_MANAGER_REQUIRED = "当前账号已无管理权限"
_TARGET_NOT_FOUND = "用户不存在"
_SELF_SUSPEND = "不能停用自己"
_SELF_REACTIVATE = "不能恢复自己"
_SUSPEND_ROLE_FORBIDDEN = "项目管理员仅能停用标注员/审核员账号"
_REACTIVATE_ROLE_FORBIDDEN = "项目管理员不能恢复该角色"
_REACTIVATE_NOT_MANAGED = "该用户不在你管理的项目内"
_SUSPEND_SCOPE_FORBIDDEN = "该用户跨多个项目，须由超级管理员处理"
_REACTIVATE_SCOPE_FORBIDDEN = "该用户存在不在你管理范围内的项目"
_LAST_SUPER_ADMIN = "不能停用最后一名超级管理员"
_ALREADY_ACTIVE = "账号当前已启用"
_NOT_REACTIVATABLE = "该停用状态不可恢复"
_ACCOUNT_BUSY = "账号正在更新，请刷新后重试"
_MANAGER_ROLES = {"super_admin", "project_admin"}


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
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
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
        return payload
    value = _json_ready(payload)
    _reject_protocol_alias(value)
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


class SuspendAccountPayload(ClosedModel):
    target_user_id: uuid.UUID


class ReactivateAccountPayload(ClosedModel):
    target_user_id: uuid.UUID
    reason: str | None = Field(max_length=500, strict=True)


class _WireRequestBase(ClosedModel):
    protocol: Literal["aap-identity-suspensions/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class SuspendAccountRequest(_WireRequestBase):
    operation: Literal["suspend_account"]
    payload: SuspendAccountPayload


class ReactivateAccountRequest(_WireRequestBase):
    operation: Literal["reactivate_account"]
    payload: ReactivateAccountPayload


SuspensionRequest: TypeAlias = Annotated[
    SuspendAccountRequest | ReactivateAccountRequest,
    Field(discriminator="operation"),
]
_SUSPENSION_REQUESTS = TypeAdapter(SuspensionRequest)


def decode_suspension_request(payload: dict | bytes) -> SuspensionRequest:
    return _SUSPENSION_REQUESTS.validate_json(_json_bytes(payload))


class SuspensionData(ClosedModel):
    user: DirectoryUserRecord


class _WireSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-suspensions/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class SuspendAccountReply(_WireSuccessBase):
    operation: Literal["suspend_account"]
    data: SuspensionData


class ReactivateAccountReply(_WireSuccessBase):
    operation: Literal["reactivate_account"]
    data: SuspensionData


SuspensionReply: TypeAlias = Annotated[
    SuspendAccountReply | ReactivateAccountReply,
    Field(discriminator="operation"),
]
_SUSPENSION_REPLIES = TypeAdapter(SuspensionReply)


class ProjectBusyDetail(ClosedModel):
    reason: Literal["project_busy"]


SuspensionFailureDetailValue: TypeAlias = str | ProjectBusyDetail | None


class SuspensionFailureDetail(ClosedModel):
    code: SuspensionFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: SuspensionFailureDetailValue

    @model_validator(mode="after")
    def fields_match_code(self) -> SuspensionFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Suspension failure requires status_code and detail")
        detail = self.detail
        fixed = {
            "manager_required": (403, _MANAGER_REQUIRED),
            "target_not_found": (404, _TARGET_NOT_FOUND),
            "self_suspend": (400, _SELF_SUSPEND),
            "self_reactivate": (400, _SELF_REACTIVATE),
            "suspend_role_forbidden": (403, _SUSPEND_ROLE_FORBIDDEN),
            "reactivate_role_forbidden": (403, _REACTIVATE_ROLE_FORBIDDEN),
            "reactivate_not_managed": (403, _REACTIVATE_NOT_MANAGED),
            "suspend_scope_forbidden": (403, _SUSPEND_SCOPE_FORBIDDEN),
            "reactivate_scope_forbidden": (403, _REACTIVATE_SCOPE_FORBIDDEN),
            "last_super_admin": (400, _LAST_SUPER_ADMIN),
            "already_active": (409, _ALREADY_ACTIVE),
            "not_reactivatable": (409, _NOT_REACTIVATABLE),
            "account_busy": (409, _ACCOUNT_BUSY),
            "operation_conflict": (409, None),
            "unavailable": (503, None),
        }
        if self.code in _AUTH_FAILURES:
            valid = self.status_code is None and detail is None
        elif self.code == "invalid_request":
            valid = self.status_code == 400 and detail is None
        elif self.code == "project_busy":
            valid = self.status_code == 409 and isinstance(detail, ProjectBusyDetail)
        else:
            status_code, expected_detail = fixed.get(self.code, (None, object()))
            valid = self.status_code == status_code and detail == expected_detail
        if not valid:
            raise ValueError("Suspension failure fields do not match its code")
        return self


class SuspensionFailureReply(ClosedModel):
    protocol: Literal["aap-identity-suspensions/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: SuspensionFailureDetail


class SuspensionFailure(Exception):
    def __init__(
        self,
        code: SuspensionFailureCode,
        status_code: int | None = None,
        detail: str | dict[str, Any] | ClosedModel | None = None,
    ):
        if isinstance(detail, ClosedModel):
            detail = detail.model_dump(mode="json", by_alias=True)
        try:
            parsed = SuspensionFailureDetail.model_validate_json(
                json.dumps(
                    {"code": code, "status_code": status_code, "detail": detail},
                    ensure_ascii=True,
                    allow_nan=False,
                )
            )
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid suspension failure") from None
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = (
            parsed.detail.model_dump(mode="json", by_alias=True)
            if isinstance(parsed.detail, ClosedModel)
            else parsed.detail
        )
        super().__init__(code)


def suspension_failure_reply(
    failure: SuspensionFailure | SuspensionFailureCode,
) -> dict:
    if isinstance(failure, SuspensionFailure):
        code, status_code, detail = (
            failure.code,
            failure.status_code,
            failure.detail,
        )
    else:
        code = failure
        if code in _AUTH_FAILURES:
            status_code, detail = None, None
        elif code == "invalid_request":
            status_code, detail = 400, None
        elif code == "project_busy":
            status_code, detail = 409, {"reason": "project_busy"}
        else:
            defaults = {
                "manager_required": (403, _MANAGER_REQUIRED),
                "target_not_found": (404, _TARGET_NOT_FOUND),
                "self_suspend": (400, _SELF_SUSPEND),
                "self_reactivate": (400, _SELF_REACTIVATE),
                "suspend_role_forbidden": (403, _SUSPEND_ROLE_FORBIDDEN),
                "reactivate_role_forbidden": (403, _REACTIVATE_ROLE_FORBIDDEN),
                "reactivate_not_managed": (403, _REACTIVATE_NOT_MANAGED),
                "suspend_scope_forbidden": (403, _SUSPEND_SCOPE_FORBIDDEN),
                "reactivate_scope_forbidden": (403, _REACTIVATE_SCOPE_FORBIDDEN),
                "last_super_admin": (400, _LAST_SUPER_ADMIN),
                "already_active": (409, _ALREADY_ACTIVE),
                "not_reactivatable": (409, _NOT_REACTIVATABLE),
                "account_busy": (409, _ACCOUNT_BUSY),
                "operation_conflict": (409, None),
                "unavailable": (503, None),
            }
            try:
                status_code, detail = defaults[code]
            except KeyError:
                raise ValueError("Failure requires status and detail") from None
    return SuspensionFailureReply(
        schema=SUSPENSION_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    ).model_dump(mode="json", by_alias=True)


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-suspensions-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True, repr=False)
    derivation_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    audit_context: SessionAuditContext

    @model_validator(mode="after")
    def credential_hash_shape(self) -> _NativeCommandBase:
        if (self.actor.credential_type == "api_key") != (
            self.expected_key_hash is not None
        ):
            raise ValueError("API-key hash must match the credential kind")
        return self


class NativeSuspendAccountCommand(_NativeCommandBase):
    operation: Literal["suspend_account"]
    payload: SuspendAccountPayload


class NativeReactivateAccountCommand(_NativeCommandBase):
    operation: Literal["reactivate_account"]
    payload: ReactivateAccountPayload


NativeSuspensionCommand: TypeAlias = Annotated[
    NativeSuspendAccountCommand | NativeReactivateAccountCommand,
    Field(discriminator="operation"),
]
_NATIVE_SUSPENSION_COMMANDS = TypeAdapter(NativeSuspensionCommand)


def native_suspension_command(
    request: SuspensionRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativeSuspensionCommand:
    values = request.model_dump(mode="json", by_alias=True)
    values.update(
        {
            "schema": NATIVE_SUSPENSION_PROTOCOL,
            "actor": actor.model_dump(mode="json"),
            "expected_key_hash": expected_key_hash,
            "request_fingerprint": request_fingerprint,
            "derivation_id": derivation_id,
        }
    )
    values.pop("token", None)
    try:
        return _NATIVE_SUSPENSION_COMMANDS.validate_json(
            json.dumps(
                values, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
        )
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_suspension_command(
    payload: dict | bytes | ClosedModel,
) -> NativeSuspensionCommand:
    try:
        return _NATIVE_SUSPENSION_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class _NativeSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-suspensions-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal
    replayed: bool

    @field_validator("replayed", mode="before")
    @classmethod
    def strict_replayed(cls, value):
        if type(value) is not bool:
            raise ValueError("replayed must be a boolean")
        return value


class NativeSuspendAccountReply(_NativeSuccessBase):
    operation: Literal["suspend_account"]
    data: SuspensionData


class NativeReactivateAccountReply(_NativeSuccessBase):
    operation: Literal["reactivate_account"]
    data: SuspensionData


NativeSuspensionSuccess: TypeAlias = Annotated[
    NativeSuspendAccountReply | NativeReactivateAccountReply,
    Field(discriminator="operation"),
]
_NATIVE_SUSPENSION_SUCCESSES = TypeAdapter(NativeSuspensionSuccess)


class NativeSuspensionFailureReply(ClosedModel):
    protocol: Literal["aap-identity-suspensions-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: SuspensionFailureDetail


class SuspensionPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-suspensions-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-suspensions/v1"]


def _failure_detail_json(value: SuspensionFailureDetailValue) -> object:
    return (
        value.model_dump(mode="json", by_alias=True)
        if isinstance(value, ClosedModel)
        else value
    )


def _expected_failure(
    request: SuspensionRequest,
    error: SuspensionFailureDetail,
    actor: Principal,
) -> bool:
    code, detail = error.code, error.detail
    if code in _AUTH_FAILURES:
        return error.status_code is None and detail is None
    if code == "invalid_request":
        return error.status_code == 400 and detail is None
    if code == "manager_required":
        return error.status_code == 403 and detail == _MANAGER_REQUIRED
    if code == "target_not_found":
        return (
            request.payload.target_user_id != actor.actor_id
            and error.status_code == 404
            and detail == _TARGET_NOT_FOUND
        )
    if code == "self_suspend":
        return (
            request.operation == "suspend_account"
            and request.payload.target_user_id == actor.actor_id
            and error.status_code == 400
            and detail == _SELF_SUSPEND
        )
    if code == "self_reactivate":
        return (
            request.operation == "reactivate_account"
            and request.payload.target_user_id == actor.actor_id
            and error.status_code == 400
            and detail == _SELF_REACTIVATE
        )
    if code == "suspend_role_forbidden":
        return (
            request.operation == "suspend_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 403
            and detail == _SUSPEND_ROLE_FORBIDDEN
        )
    if code == "reactivate_role_forbidden":
        return (
            request.operation == "reactivate_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 403
            and detail == _REACTIVATE_ROLE_FORBIDDEN
        )
    if code == "reactivate_not_managed":
        return (
            request.operation == "reactivate_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 403
            and detail == _REACTIVATE_NOT_MANAGED
        )
    if code == "suspend_scope_forbidden":
        return (
            request.operation == "suspend_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 403
            and detail == _SUSPEND_SCOPE_FORBIDDEN
        )
    if code == "reactivate_scope_forbidden":
        return (
            request.operation == "reactivate_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 403
            and detail == _REACTIVATE_SCOPE_FORBIDDEN
        )
    if code == "last_super_admin":
        return (
            request.operation == "suspend_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 400
            and detail == _LAST_SUPER_ADMIN
        )
    if code == "already_active":
        return (
            request.operation == "reactivate_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 409
            and detail == _ALREADY_ACTIVE
        )
    if code == "not_reactivatable":
        return (
            request.operation == "reactivate_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 409
            and detail == _NOT_REACTIVATABLE
        )
    if code == "account_busy":
        return error.status_code == 409 and detail == _ACCOUNT_BUSY
    if code == "project_busy":
        return (
            request.operation == "suspend_account"
            and request.payload.target_user_id != actor.actor_id
            and actor.role in _MANAGER_ROLES
            and error.status_code == 409
            and isinstance(detail, ProjectBusyDetail)
        )
    if code == "operation_conflict":
        return error.status_code == 409 and detail is None
    if code == "unavailable":
        return error.status_code == 503 and detail is None
    return False


def _decode_failure(
    payload: bytes,
    *,
    request: SuspensionRequest,
    actor: Principal,
    native: bool,
) -> SuspensionFailure:
    model = NativeSuspensionFailureReply if native else SuspensionFailureReply
    try:
        reply = model.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not _expected_failure(request, reply.error, actor):
        raise IdentityUnavailable()
    detail = _failure_detail_json(reply.error.detail)
    return SuspensionFailure(reply.error.code, reply.error.status_code, detail)


def _validate_success(request: SuspensionRequest, reply) -> None:
    user = reply.data.user
    expected_active = request.operation == "reactivate_account"
    if (
        reply.operation != request.operation
        or reply.operation_id != request.operation_id
        or reply.principal.role not in _MANAGER_ROLES
        or user.id == reply.principal.actor_id
        or user.id != request.payload.target_user_id
        or user.is_active is not expected_active
    ):
        raise IdentityUnavailable()


def _decode_reply(
    command: SuspensionRequest | NativeSuspensionCommand,
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
    actor = command.actor if native else expected_principal
    if actor is None:
        raise ValueError("Expected an admitted Principal for suspension replies")
    if raw["ok"] is False:
        return _decode_failure(encoded, request=command, actor=actor, native=native)
    adapter = _NATIVE_SUSPENSION_SUCCESSES if native else _SUSPENSION_REPLIES
    try:
        reply = adapter.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    _validate_success(command, reply)
    if native and not _same_fenced_credential(reply.principal, actor):
        raise IdentityUnavailable()
    if expected_principal is not None and not _same_fenced_credential(
        reply.principal, expected_principal
    ):
        raise IdentityUnavailable()
    return reply


def decode_native_suspension_reply(
    command: NativeSuspensionCommand,
    payload: dict | bytes | ClosedModel,
) -> NativeSuspensionSuccess:
    reply = _decode_reply(
        command, payload, native=True, expected_principal=command.actor
    )
    if isinstance(reply, SuspensionFailure):
        raise reply
    return reply


def decode_suspension_reply(
    request: SuspensionRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal,
) -> SuspensionReply:
    reply = _decode_reply(
        request, payload, native=False, expected_principal=expected_principal
    )
    if isinstance(reply, SuspensionFailure):
        raise reply
    return reply


def decode_suspension_policy(
    payload: dict | bytes | ClosedModel,
) -> SuspensionPolicyReply:
    try:
        return SuspensionPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
