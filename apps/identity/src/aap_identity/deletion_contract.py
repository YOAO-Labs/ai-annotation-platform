"""Closed wire and native contracts for administrative account deletion."""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal, TypeAlias

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

DELETION_PROTOCOL = "aap-identity-deletions/v1"
NATIVE_DELETION_PROTOCOL = "aap-identity-deletions-native/v1"
DELETION_POLICY_PROTOCOL = "aap-identity-deletions-policy/v1"
DELETION_DERIVATION = "aap-identity-deletions/v1"

DeletionOperation: TypeAlias = Literal["delete_account"]
DeletionFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "manager_required",
        "target_not_found",
        "self_delete",
        "delete_role_forbidden",
        "delete_scope_forbidden",
        "last_super_admin",
        "has_pending_tasks",
        "receiver_unavailable",
        "receiver_is_target",
        "receiver_role_invalid",
        "receiver_scope_forbidden",
        "account_busy",
        "project_busy",
        "operation_conflict",
        "unavailable",
    ]
)

_INT64_MAX = 2**63 - 1
_MANAGER_ROLES = {"super_admin", "project_admin"}
_AUTH_FAILURES = {
    "credential_invalid",
    "credential_revoked",
    "generation_outdated",
    "token_expired",
    "user_inactive",
    "invalid_account_role",
    "credential_busy",
}
_DETAILS: dict[str, tuple[int, str | None]] = {
    "manager_required": (403, "当前账号已无管理权限"),
    "target_not_found": (404, "用户不存在"),
    "self_delete": (400, "不能删除自己"),
    "delete_role_forbidden": (403, "项目管理员仅能删除员工账号"),
    "delete_scope_forbidden": (403, "该用户跨多个项目，须由超级管理员处理"),
    "last_super_admin": (400, "不能删除最后一名超级管理员"),
    "receiver_unavailable": (400, "转交目标用户不存在或已禁用"),
    "receiver_is_target": (400, "转交目标不能与被删用户相同"),
    "receiver_role_invalid": (400, "转交目标角色不合法"),
    "receiver_scope_forbidden": (403, "转交目标必须是待转交任务所在项目的成员"),
    "account_busy": (409, "账号正在更新，请刷新后重试"),
    "operation_conflict": (409, None),
    "unavailable": (503, None),
}


class DeletionPayload(ClosedModel):
    target_user_id: uuid.UUID
    # Required even when null; the public adapter normalizes a missing body/field.
    transfer_to_user_id: uuid.UUID | None


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-deletions/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class DeleteAccountRequest(_WireRequest):
    operation: Literal["delete_account"]
    payload: DeletionPayload


DeletionRequest: TypeAlias = DeleteAccountRequest
_DELETION_REQUESTS = TypeAdapter(DeletionRequest)


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
        value != value or value in (float("inf"), -float("inf"))
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


def decode_deletion_request(payload: dict | bytes) -> DeletionRequest:
    try:
        return _DELETION_REQUESTS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise


class DeletionData(ClosedModel):
    user: DirectoryUserRecord


class _WireSuccess(ClosedModel):
    protocol: Literal["aap-identity-deletions/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: Literal["delete_account"]
    operation_id: uuid.UUID
    principal: Principal
    data: DeletionData


DeletionReply: TypeAlias = _WireSuccess
_DELETION_REPLIES = TypeAdapter(DeletionReply)


class ProjectBusyDetail(ClosedModel):
    reason: Literal["project_busy"]


class PendingTasksDetail(ClosedModel):
    reason: Literal["has_pending_tasks"]
    pending_task_count: int = Field(ge=0, le=_INT64_MAX, strict=True)
    locked_task_count: int = Field(ge=0, le=_INT64_MAX, strict=True)
    sample_task_ids: list[uuid.UUID] = Field(max_length=5)
    message: str = Field(strict=True)

    @field_validator("pending_task_count", "locked_task_count", mode="before")
    @classmethod
    def strict_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Pending-work counts must be integers")
        return value

    @model_validator(mode="after")
    def detail_matches_counts(self) -> PendingTasksDetail:
        if self.pending_task_count == 0 and self.locked_task_count == 0:
            raise ValueError("Pending-work detail needs a blocker")
        if len(self.sample_task_ids) > min(5, self.pending_task_count):
            raise ValueError("Task samples exceed the pending-task count")
        if len(set(self.sample_task_ids)) != len(self.sample_task_ids):
            raise ValueError("Task samples must be unique")
        expected = (
            f"该用户当前有 {self.pending_task_count} 个未完成任务、"
            f"{self.locked_task_count} 个锁定任务，需先转交"
        )
        if self.message != expected:
            raise ValueError("Pending-work message does not match its counts")
        return self


DeletionFailureDetailValue: TypeAlias = (
    str | ProjectBusyDetail | PendingTasksDetail | None
)


class DeletionFailureDetail(ClosedModel):
    code: DeletionFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: DeletionFailureDetailValue

    @model_validator(mode="after")
    def fields_match_code(self) -> DeletionFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Deletion failure requires status_code and detail")
        if self.code in _AUTH_FAILURES:
            valid = self.status_code is None and self.detail is None
        elif self.code == "invalid_request":
            valid = self.status_code == 400 and self.detail is None
        elif self.code == "has_pending_tasks":
            valid = self.status_code == 409 and isinstance(
                self.detail, PendingTasksDetail
            )
        elif self.code == "project_busy":
            valid = self.status_code == 409 and isinstance(
                self.detail, ProjectBusyDetail
            )
        else:
            status, detail = _DETAILS.get(self.code, (None, object()))
            valid = self.status_code == status and self.detail == detail
        if not valid:
            raise ValueError("Deletion failure fields do not match its code")
        return self


class DeletionFailureReply(ClosedModel):
    protocol: Literal["aap-identity-deletions/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DeletionFailureDetail


class DeletionFailure(Exception):
    def __init__(
        self,
        code: DeletionFailureCode,
        status_code: int | None = None,
        detail: str | dict[str, Any] | ClosedModel | None = None,
    ):
        if isinstance(detail, ClosedModel):
            detail = detail.model_dump(mode="json", by_alias=True)
        try:
            parsed = DeletionFailureDetail.model_validate_json(
                json.dumps(
                    {"code": code, "status_code": status_code, "detail": detail},
                    ensure_ascii=True,
                    allow_nan=False,
                )
            )
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid deletion failure") from None
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = (
            parsed.detail.model_dump(mode="json", by_alias=True)
            if isinstance(parsed.detail, ClosedModel)
            else parsed.detail
        )
        super().__init__(code)


def deletion_failure_reply(
    failure: DeletionFailure | DeletionFailureCode,
) -> dict:
    if isinstance(failure, DeletionFailure):
        code, status_code, detail = failure.code, failure.status_code, failure.detail
    else:
        code = failure
        if code in _AUTH_FAILURES:
            status_code, detail = None, None
        elif code == "invalid_request":
            status_code, detail = 400, None
        elif code == "project_busy":
            status_code, detail = 409, {"reason": "project_busy"}
        elif code == "has_pending_tasks":
            raise ValueError("Pending-task failure requires its validated detail")
        else:
            try:
                status_code, detail = _DETAILS[code]
            except KeyError:
                raise ValueError("Failure requires status and detail") from None
    return DeletionFailureReply(
        schema=DELETION_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    ).model_dump(mode="json", by_alias=True)


class _NativeCommand(ClosedModel):
    protocol: Literal["aap-identity-deletions-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    operation: Literal["delete_account"]
    payload: DeletionPayload
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True, repr=False)
    derivation_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    audit_context: SessionAuditContext

    @model_validator(mode="after")
    def credential_hash_shape(self) -> _NativeCommand:
        if (self.actor.credential_type == "api_key") != (
            self.expected_key_hash is not None
        ):
            raise ValueError("API-key hash must match credential kind")
        return self


NativeDeletionCommand: TypeAlias = _NativeCommand
_NATIVE_COMMANDS = TypeAdapter(NativeDeletionCommand)


def native_deletion_command(
    request: DeletionRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativeDeletionCommand:
    values = request.model_dump(mode="json", by_alias=True)
    values.update(
        {
            "schema": NATIVE_DELETION_PROTOCOL,
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
        return _NATIVE_COMMANDS.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_deletion_command(
    payload: dict | bytes | ClosedModel,
) -> NativeDeletionCommand:
    try:
        return _NATIVE_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class _NativeSuccess(ClosedModel):
    protocol: Literal["aap-identity-deletions-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: Literal["delete_account"]
    operation_id: uuid.UUID
    principal: Principal
    data: DeletionData
    replayed: bool

    @field_validator("replayed", mode="before")
    @classmethod
    def strict_replayed(cls, value):
        if type(value) is not bool:
            raise ValueError("replayed must be a boolean")
        return value


NativeDeletionSuccess: TypeAlias = _NativeSuccess
_NATIVE_SUCCESSES = TypeAdapter(NativeDeletionSuccess)


class NativeDeletionFailureReply(ClosedModel):
    protocol: Literal["aap-identity-deletions-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DeletionFailureDetail


class DeletionPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-deletions-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-deletions/v1"]


def _expected_failure(
    request: DeletionRequest,
    error: DeletionFailureDetail,
    actor: Principal,
) -> bool:
    code, detail = error.code, error.detail
    target = request.payload.target_user_id
    receiver = request.payload.transfer_to_user_id
    if code in _AUTH_FAILURES:
        return error.status_code is None and detail is None
    if code == "invalid_request":
        return error.status_code == 400 and detail is None
    if code == "manager_required":
        return error.status_code == 403 and detail == _DETAILS[code][1]
    if code == "target_not_found":
        return (
            target != actor.actor_id
            and error.status_code == 404
            and detail == _DETAILS[code][1]
        )
    if code == "self_delete":
        return (
            target == actor.actor_id
            and error.status_code == 400
            and detail == _DETAILS[code][1]
        )
    if code == "delete_role_forbidden":
        return (
            actor.role == "project_admin"
            and target != actor.actor_id
            and error.status_code == 403
            and detail == _DETAILS[code][1]
        )
    if code == "delete_scope_forbidden":
        return (
            actor.role == "project_admin"
            and target != actor.actor_id
            and error.status_code == 403
            and detail == _DETAILS[code][1]
        )
    if code == "last_super_admin":
        return (
            target != actor.actor_id
            and error.status_code == 400
            and detail == _DETAILS[code][1]
        )
    if code == "has_pending_tasks":
        return (
            receiver is None
            and target != actor.actor_id
            and error.status_code == 409
            and isinstance(detail, PendingTasksDetail)
        )
    if code == "receiver_unavailable":
        return (
            receiver is not None
            and receiver != target
            and error.status_code == 400
            and detail == _DETAILS[code][1]
        )
    if code == "receiver_is_target":
        return (
            receiver == target
            and error.status_code == 400
            and detail == _DETAILS[code][1]
        )
    if code == "receiver_role_invalid":
        return (
            receiver is not None
            and receiver != target
            and error.status_code == 400
            and detail == _DETAILS[code][1]
        )
    if code == "receiver_scope_forbidden":
        return (
            actor.role == "project_admin"
            and receiver is not None
            and receiver != target
            and error.status_code == 403
            and detail == _DETAILS[code][1]
        )
    if code == "account_busy":
        return error.status_code == 409 and detail == _DETAILS[code][1]
    if code == "project_busy":
        return (
            target != actor.actor_id
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
    payload: bytes, *, request: DeletionRequest, actor: Principal, native: bool
) -> DeletionFailure:
    model = NativeDeletionFailureReply if native else DeletionFailureReply
    try:
        reply = model.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not _expected_failure(request, reply.error, actor):
        raise IdentityUnavailable()
    detail = reply.error.detail
    if isinstance(detail, ClosedModel):
        detail = detail.model_dump(mode="json", by_alias=True)
    return DeletionFailure(reply.error.code, reply.error.status_code, detail)


def _validate_success(request, reply) -> None:
    user = reply.data.user
    if (
        reply.operation != request.operation
        or reply.operation_id != request.operation_id
        or reply.principal.role not in _MANAGER_ROLES
        or user.id == reply.principal.actor_id
        or user.id != request.payload.target_user_id
        or user.is_active is not False
    ):
        raise IdentityUnavailable()


def _decode_reply(
    command, payload, *, native: bool, expected_principal: Principal | None
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
        raise ValueError("Expected an admitted Principal for deletion reply")
    if raw["ok"] is False:
        return _decode_failure(encoded, request=command, actor=actor, native=native)
    adapter = _NATIVE_SUCCESSES if native else _DELETION_REPLIES
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


def decode_native_deletion_reply(
    command: NativeDeletionCommand, payload: dict | bytes | ClosedModel
) -> NativeDeletionSuccess:
    reply = _decode_reply(
        command, payload, native=True, expected_principal=command.actor
    )
    if isinstance(reply, DeletionFailure):
        raise reply
    return reply


def decode_deletion_reply(
    request: DeletionRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal,
) -> DeletionReply:
    reply = _decode_reply(
        request, payload, native=False, expected_principal=expected_principal
    )
    if isinstance(reply, DeletionFailure):
        raise reply
    return reply


def decode_deletion_policy(payload: dict | bytes | ClosedModel) -> DeletionPolicyReply:
    try:
        return DeletionPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
