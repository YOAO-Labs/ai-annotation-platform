"""Closed wire/native contracts for administrator password reset."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Literal, TypeAlias

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
from .session_contract import SessionAuditContext

ADMIN_PASSWORD_PROTOCOL = "aap-identity-admin-passwords/v1"
NATIVE_ADMIN_PASSWORD_PROTOCOL = "aap-identity-admin-passwords-native/v1"
ADMIN_PASSWORD_POLICY_PROTOCOL = "aap-identity-admin-passwords-policy/v1"
ADMIN_PASSWORD_DERIVATION = "aap-identity-admin-passwords/v1"

_HEX_64 = r"^[0-9a-f]{64}$"
_BCRYPT12 = r"^\$2[aby]\$12\$[./A-Za-z0-9]{53}$"
_TEMP_PASSWORD_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*?"
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
_FAILURE_FIELDS: dict[str, tuple[int | None, str | None]] = {
    "invalid_request": (400, None),
    "manager_required": (403, "当前账号已无管理权限"),
    "target_not_found": (404, "用户不存在"),
    "self_reset": (400, "不能给自己重置密码（请用「修改密码」）"),
    "role_forbidden": (403, "只能重置等级低于你的用户的密码"),
    "target_out_of_scope": (403, "该用户不在你管理的项目内"),
    "account_busy": (409, "账号正在更新，请刷新后重试"),
    "operation_conflict": (409, None),
    "unavailable": (503, None),
}

AdminPasswordFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "manager_required",
        "target_not_found",
        "self_reset",
        "role_forbidden",
        "target_out_of_scope",
        "account_busy",
        "operation_conflict",
        "unavailable",
    ]
)


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
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
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


class AdminPasswordPayload(ClosedModel):
    target_user_id: uuid.UUID


class _WireRequestBase(ClosedModel):
    protocol: Literal["aap-identity-admin-passwords/v1"] = Field(alias="schema")
    operation: Literal["admin_reset_password"]
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext
    payload: AdminPasswordPayload


AdminPasswordRequest: TypeAlias = _WireRequestBase


def decode_admin_password_request(payload: dict | bytes) -> AdminPasswordRequest:
    return _WireRequestBase.model_validate_json(_json_bytes(payload))


def _valid_temp_password(value: str) -> bool:
    return (
        len(value) == 16
        and all(character in _TEMP_PASSWORD_ALPHABET for character in value)
        and any("A" <= character <= "Z" for character in value)
        and any("a" <= character <= "z" for character in value)
        and any("0" <= character <= "9" for character in value)
        and any(character in "!@#$%^&*?" for character in value)
    )


class AdminPasswordSuccessData(ClosedModel):
    target_user_id: uuid.UUID
    target_email: str = Field(strict=True)
    temp_password: str = Field(min_length=16, max_length=16, strict=True, repr=False)
    password_admin_reset_at: datetime

    @field_validator("temp_password")
    @classmethod
    def valid_temporary_password(cls, value: str) -> str:
        if not _valid_temp_password(value):
            raise ValueError("Temporary password does not meet its closed format")
        return value

    @field_validator("password_admin_reset_at")
    @classmethod
    def aware_reset_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Password reset timestamp must include a timezone")
        return value.astimezone(UTC)


class NativeAdminPasswordSuccessData(ClosedModel):
    target_user_id: uuid.UUID
    target_email: str = Field(strict=True)
    password_admin_reset_at: datetime

    @field_validator("password_admin_reset_at")
    @classmethod
    def aware_reset_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Password reset timestamp must include a timezone")
        return value.astimezone(UTC)


class _WireReplyBase(ClosedModel):
    protocol: Literal["aap-identity-admin-passwords/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: Literal["admin_reset_password"]
    operation_id: uuid.UUID
    principal: Principal
    data: AdminPasswordSuccessData


AdminPasswordReply: TypeAlias = _WireReplyBase
_ADMIN_PASSWORD_REPLIES = TypeAdapter(AdminPasswordReply)


class AdminPasswordFailureDetail(ClosedModel):
    code: AdminPasswordFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: str | None = Field(strict=True)

    @model_validator(mode="after")
    def fields_match_code(self) -> AdminPasswordFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Admin password failure requires status and detail")
        if self.code in _AUTH_FAILURES:
            if self.status_code is not None or self.detail is not None:
                raise ValueError(
                    "Authentication failures do not carry status or detail"
                )
            return self
        expected = _FAILURE_FIELDS.get(self.code)
        if expected != (self.status_code, self.detail):
            raise ValueError("Admin password failure fields do not match its code")
        return self


class AdminPasswordFailureReply(ClosedModel):
    protocol: Literal["aap-identity-admin-passwords/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: AdminPasswordFailureDetail


class AdminPasswordFailure(Exception):
    def __init__(
        self,
        code: AdminPasswordFailureCode,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        parsed = AdminPasswordFailureDetail(
            code=code, status_code=status_code, detail=detail
        )
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = parsed.detail
        super().__init__(code)


def admin_password_failure_reply(
    failure: AdminPasswordFailure | AdminPasswordFailureCode,
) -> dict:
    if isinstance(failure, AdminPasswordFailure):
        code, status_code, detail = failure.code, failure.status_code, failure.detail
    else:
        code = failure
        if code in _AUTH_FAILURES:
            status_code, detail = None, None
        else:
            status_code, detail = _FAILURE_FIELDS[code]
    return AdminPasswordFailureReply(
        schema=ADMIN_PASSWORD_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    ).model_dump(mode="json", by_alias=True)


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-admin-passwords-native/v1"] = Field(alias="schema")
    operation: Literal["admin_reset_password"]
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=_HEX_64, strict=True, repr=False)
    derivation_id: str = Field(pattern=_HEX_64, strict=True)
    audit_context: SessionAuditContext
    payload: AdminPasswordPayload
    new_password_hash: str = Field(pattern=_BCRYPT12, strict=True, repr=False)

    @model_validator(mode="after")
    def key_hash_matches_credential(self) -> _NativeCommandBase:
        if (self.actor.credential_type == "api_key") != (
            self.expected_key_hash is not None
        ):
            raise ValueError("API-key hash must match the credential kind")
        return self


NativeAdminPasswordCommand: TypeAlias = _NativeCommandBase


def native_admin_password_command(
    request: AdminPasswordRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
    new_password_hash: str,
) -> NativeAdminPasswordCommand:
    values = request.model_dump(mode="json", by_alias=True)
    values.update(
        {
            "schema": NATIVE_ADMIN_PASSWORD_PROTOCOL,
            "actor": actor.model_dump(mode="json"),
            "expected_key_hash": expected_key_hash,
            "request_fingerprint": request_fingerprint,
            "derivation_id": derivation_id,
            "new_password_hash": new_password_hash,
        }
    )
    values.pop("token", None)
    try:
        encoded = json.dumps(
            values, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        return _NativeCommandBase.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_admin_password_command(
    payload: dict | bytes | ClosedModel,
) -> NativeAdminPasswordCommand:
    try:
        return _NativeCommandBase.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class NativeAdminPasswordSuccess(ClosedModel):
    protocol: Literal["aap-identity-admin-passwords-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: Literal["admin_reset_password"]
    operation_id: uuid.UUID
    principal: Principal
    data: NativeAdminPasswordSuccessData
    replayed: bool

    @field_validator("replayed", mode="before")
    @classmethod
    def strict_replayed(cls, value):
        if type(value) is not bool:
            raise ValueError("replayed must be a boolean")
        return value


class NativeAdminPasswordFailureReply(ClosedModel):
    protocol: Literal["aap-identity-admin-passwords-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: AdminPasswordFailureDetail


NativeAdminPasswordReply: TypeAlias = (
    NativeAdminPasswordSuccess | NativeAdminPasswordFailureReply
)


class NativeAdminPasswordPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-admin-passwords-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-admin-passwords/v1"]


AdminPasswordPolicyReply = NativeAdminPasswordPolicyReply


def _strict_reply_payload(payload: dict | bytes | ClosedModel) -> tuple[bytes, dict]:
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None
    if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
        raise IdentityUnavailable()
    return encoded, raw


def _expected_failure(
    command: _WireRequestBase | _NativeCommandBase,
    failure: AdminPasswordFailureDetail,
    actor: Principal | None,
) -> bool:
    if failure.code in _AUTH_FAILURES:
        valid = failure.status_code is None and failure.detail is None
    else:
        valid = _FAILURE_FIELDS.get(failure.code) == (
            failure.status_code,
            failure.detail,
        )
    if not valid:
        return False
    if failure.code == "self_reset":
        return actor is not None and command.payload.target_user_id == actor.actor_id
    return True


def _validated_failure(
    encoded: bytes,
    *,
    command: _WireRequestBase | _NativeCommandBase,
    native: bool,
    expected_principal: Principal | None,
) -> AdminPasswordFailure:
    model = NativeAdminPasswordFailureReply if native else AdminPasswordFailureReply
    try:
        reply = model.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    actor = (
        command.actor
        if native and isinstance(command, _NativeCommandBase)
        else expected_principal
    )
    if not _expected_failure(command, reply.error, actor):
        raise IdentityUnavailable()
    if isinstance(command, _WireRequestBase) and reply.error.detail is not None:
        if command.token in reply.error.detail:
            raise IdentityUnavailable()
    return AdminPasswordFailure(
        reply.error.code, reply.error.status_code, reply.error.detail
    )


def decode_native_admin_password_reply(
    command: NativeAdminPasswordCommand,
    payload: dict | bytes | ClosedModel,
) -> NativeAdminPasswordSuccess:
    encoded, raw = _strict_reply_payload(payload)
    if raw["ok"] is False:
        raise _validated_failure(
            encoded,
            command=command,
            native=True,
            expected_principal=None,
        )
    try:
        reply = NativeAdminPasswordSuccess.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if (
        reply.operation != command.operation
        or reply.operation_id != command.operation_id
        or not _same_fenced_credential(reply.principal, command.actor)
        or reply.principal.role not in {"super_admin", "project_admin"}
        or reply.data.target_user_id != command.payload.target_user_id
        or reply.data.target_user_id == reply.principal.actor_id
    ):
        raise IdentityUnavailable()
    return reply


def decode_admin_password_reply(
    request: AdminPasswordRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal,
    expected_temp_password: str | None = None,
) -> AdminPasswordReply:
    encoded, raw = _strict_reply_payload(payload)
    if raw["ok"] is False:
        raise _validated_failure(
            encoded,
            command=request,
            native=False,
            expected_principal=expected_principal,
        )
    try:
        reply = _ADMIN_PASSWORD_REPLIES.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if (
        reply.operation != request.operation
        or reply.operation_id != request.operation_id
        or not _same_fenced_credential(reply.principal, expected_principal)
        or reply.principal.role not in {"super_admin", "project_admin"}
        or reply.data.target_user_id != request.payload.target_user_id
        or reply.data.target_user_id == reply.principal.actor_id
        or (
            expected_temp_password is not None
            and reply.data.temp_password != expected_temp_password
        )
    ):
        raise IdentityUnavailable()
    return reply


def decode_native_admin_password_policy(
    payload: dict | bytes | ClosedModel,
) -> NativeAdminPasswordPolicyReply:
    try:
        return NativeAdminPasswordPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_admin_password_policy(
    payload: dict | bytes | ClosedModel,
) -> NativeAdminPasswordPolicyReply:
    return decode_native_admin_password_policy(payload)
