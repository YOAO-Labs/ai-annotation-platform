"""Closed public, native, and readiness contracts for account recovery."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .account_contract import AccountRecord
from .contract import ClosedModel, IdentityUnavailable, _reject_protocol_alias
from .password_policy import validate_password_strength
from .session_contract import SessionAuditContext, SessionClaims, _verify_jws_claims

RECOVERY_PROTOCOL = "aap-identity-recovery/v1"
NATIVE_RECOVERY_PROTOCOL = "aap-identity-recovery-native/v1"
RECOVERY_POLICY_PROTOCOL = "aap-identity-recovery-policy/v1"
_HEX_64 = r"^[0-9a-f]{64}$"
_BCRYPT12 = r"^\$2[aby]\$12\$[./A-Za-z0-9]{53}$"

RecoveryOperation = Literal[
    "register_open",
    "request_password_reset",
    "resend_email_verification",
    "consume_password_reset",
    "consume_email_verification",
]
RecoveryFailureCode = Literal[
    "registration_disabled",
    "email_exists",
    "captcha_required",
    "policy_rejected",
    "operation_conflict",
    "invalid_request",
    "unavailable",
]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def _assert_finite(value: object) -> None:
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if isinstance(value, ClosedModel):
        for item in value.__dict__.values():
            _assert_finite(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_finite(key)
            _assert_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_finite(item)


def _json_bytes(payload: dict | bytes | ClosedModel) -> bytes:
    if isinstance(payload, bytes):
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_protocol_alias(value)
        _assert_finite(value)
        return payload
    if isinstance(payload, ClosedModel):
        _assert_finite(payload)
        value = payload.model_dump(mode="json", by_alias=True)
    else:
        value = payload
    _reject_protocol_alias(value)
    _assert_finite(value)
    return json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _normalize_email(value: str) -> str:
    return value.strip().lower()


class RegisterOpenPayload(ClosedModel):
    email: str = Field(min_length=3, max_length=255, strict=True)
    name: str = Field(min_length=1, max_length=100, strict=True)
    password: str = Field(min_length=8, max_length=128, strict=True, repr=False)
    captcha_verified: bool

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        value = _normalize_email(value)
        if "@" not in value:
            raise ValueError("邮箱格式不正确")
        return value

    @field_validator("captcha_verified", mode="before")
    @classmethod
    def strict_captcha(cls, value):
        if type(value) is not bool:
            raise ValueError("captcha_verified must be a boolean")
        return value


class RequestPasswordResetPayload(ClosedModel):
    email: str = Field(min_length=3, max_length=255, strict=True)
    captcha_verified: bool

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("captcha_verified", mode="before")
    @classmethod
    def strict_captcha(cls, value):
        if type(value) is not bool:
            raise ValueError("captcha_verified must be a boolean")
        return value


class ResendEmailVerificationPayload(ClosedModel):
    email: str = Field(min_length=3, max_length=255, strict=True)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return _normalize_email(value)


class ConsumePasswordResetPayload(ClosedModel):
    token: str = Field(min_length=1, max_length=64, strict=True, repr=False)
    new_password: str = Field(min_length=8, max_length=128, strict=True, repr=False)


class ConsumeEmailVerificationPayload(ClosedModel):
    token: str = Field(min_length=1, max_length=64, strict=True, repr=False)


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-recovery/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    audit_context: SessionAuditContext


class RegisterOpenRequest(_WireRequest):
    operation: Literal["register_open"]
    payload: RegisterOpenPayload


class RequestPasswordResetRequest(_WireRequest):
    operation: Literal["request_password_reset"]
    payload: RequestPasswordResetPayload


class ResendEmailVerificationRequest(_WireRequest):
    operation: Literal["resend_email_verification"]
    payload: ResendEmailVerificationPayload


class ConsumePasswordResetRequest(_WireRequest):
    operation: Literal["consume_password_reset"]
    payload: ConsumePasswordResetPayload


class ConsumeEmailVerificationRequest(_WireRequest):
    operation: Literal["consume_email_verification"]
    payload: ConsumeEmailVerificationPayload


RecoveryRequest: TypeAlias = Annotated[
    RegisterOpenRequest
    | RequestPasswordResetRequest
    | ResendEmailVerificationRequest
    | ConsumePasswordResetRequest
    | ConsumeEmailVerificationRequest,
    Field(discriminator="operation"),
]
_RECOVERY_REQUESTS = TypeAdapter(RecoveryRequest)


def decode_recovery_request(payload: dict | bytes) -> RecoveryRequest:
    """Decode one exact recovery command, rejecting duplicate keys and aliases."""
    return _RECOVERY_REQUESTS.validate_json(_json_bytes(payload))


class RecoveryResultData(ClosedModel):
    account: AccountRecord | None
    claims: SessionClaims | None
    access_token: str | None = Field(min_length=1, strict=True, repr=False)
    email_verification_required: bool | None
    completed: bool | None

    @field_validator("email_verification_required", "completed", mode="before")
    @classmethod
    def strict_optional_boolean(cls, value):
        if value is not None and type(value) is not bool:
            raise ValueError("Recovery result flags must be booleans")
        return value


class RecoverySuccess(ClosedModel):
    protocol: Literal["aap-identity-recovery/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: RecoveryOperation
    operation_id: uuid.UUID
    data: RecoveryResultData

    @model_validator(mode="after")
    def result_shape(self) -> RecoverySuccess:
        data = self.data
        if self.operation == "register_open":
            if (
                data.account is None
                or data.email_verification_required is None
                or data.completed is not None
                or data.account.id != self.operation_id
                or data.account.role != "viewer"
                or data.account.group_id is not None
                or data.account.group_name is not None
                or not data.account.is_active
                or data.account.last_login_at is not None
            ):
                raise ValueError("Invalid registration result")
            if data.email_verification_required:
                if (
                    data.claims is not None
                    or data.access_token is not None
                    or data.account.email_verified_at is not None
                    or data.account.status != "offline"
                ):
                    raise ValueError("Verified registration must not issue a session")
            elif (
                data.claims is None
                or data.access_token is None
                or data.account.email_verified_at is None
                or data.account.status != "online"
            ):
                raise ValueError("Immediate registration must issue a verified session")
            if data.claims is not None and (
                data.claims.sub != data.account.id
                or data.claims.jti != self.operation_id
                or data.claims.role != data.account.role
                or data.claims.gen != 0
                or data.claims.iss != "aap-identity/v1"
            ):
                raise ValueError("Registration claims do not match the account")
        elif self.operation in {
            "request_password_reset",
            "resend_email_verification",
        }:
            if any(
                value is not None
                for value in (
                    data.account,
                    data.claims,
                    data.access_token,
                    data.email_verification_required,
                    data.completed,
                )
            ):
                raise ValueError("Mail request result must be empty")
        elif (
            data.account is not None
            or data.claims is not None
            or data.access_token is not None
            or data.email_verification_required is not None
            or data.completed is None
        ):
            raise ValueError("Token consumption result is malformed")
        if data.access_token is not None:
            if data.claims is None:
                raise ValueError("Session token has no frozen claims")
            _verify_jws_claims(data.access_token, data.claims)
        return self


RecoveryReply = RecoverySuccess


class RecoveryValidationIssue(ClosedModel):
    type: str = Field(min_length=1, strict=True)
    loc: list[str | int]
    msg: str = Field(strict=True)


RecoveryFailureDetail: TypeAlias = str | list[RecoveryValidationIssue] | None


class RecoveryFailureDetailModel(ClosedModel):
    code: RecoveryFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: RecoveryFailureDetail

    @model_validator(mode="after")
    def safe_failure_shape(self) -> RecoveryFailureDetailModel:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Recovery failure requires status_code and detail")
        expected = {
            "registration_disabled": 403,
            "email_exists": 409,
            "captcha_required": 400,
            "operation_conflict": 409,
            "invalid_request": 400,
            "unavailable": 503,
        }
        if self.code == "policy_rejected":
            if self.status_code != 422 or not isinstance(self.detail, list):
                raise ValueError("Password policy errors need a validation list")
        elif self.status_code != expected[self.code]:
            raise ValueError("Recovery failure status does not match its code")
        elif isinstance(self.detail, list):
            raise ValueError("Only policy failures carry validation lists")
        if isinstance(self.detail, str):
            safe_details = {
                "registration_disabled": {"开放注册未启用"},
                "email_exists": {"该邮箱已被注册"},
                "captcha_required": {"captcha_required", "captcha_failed"},
                "operation_conflict": {"operation_conflict"},
                "invalid_request": {
                    "重置链接无效或已过期",
                    "验证链接无效或已过期",
                    "operation_conflict",
                },
                "unavailable": {"身份恢复服务暂不可用"},
            }
            if self.detail not in safe_details.get(self.code, set()):
                raise ValueError("Recovery failure detail is not safe")
        return self


class RecoveryFailureReply(ClosedModel):
    protocol: Literal["aap-identity-recovery/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: RecoveryFailureDetailModel


class RecoveryFailure(Exception):
    def __init__(
        self,
        code: RecoveryFailureCode,
        status_code: int | None = None,
        detail: RecoveryFailureDetail = None,
    ):
        if status_code is None:
            status_code = {
                "registration_disabled": 403,
                "email_exists": 409,
                "captcha_required": 400,
                "policy_rejected": 422,
                "operation_conflict": 409,
                "invalid_request": 400,
                "unavailable": 503,
            }[code]
        try:
            parsed = RecoveryFailureDetailModel(
                code=code, status_code=status_code, detail=detail
            )
        except ValidationError:
            raise IdentityUnavailable() from None
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = parsed.detail
        super().__init__(code)


def recovery_failure_reply(
    failure: RecoveryFailure | RecoveryFailureCode,
) -> dict:
    if isinstance(failure, RecoveryFailure):
        code, status_code, detail = (
            failure.code,
            failure.status_code,
            failure.detail,
        )
    else:
        code = failure
        status_code = {
            "registration_disabled": 403,
            "email_exists": 409,
            "captcha_required": 400,
            "policy_rejected": 422,
            "operation_conflict": 409,
            "invalid_request": 400,
            "unavailable": 503,
        }[code]
        detail = [] if code == "policy_rejected" else None
    return RecoveryFailureReply(
        schema=RECOVERY_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    ).model_dump(mode="json", by_alias=True)


def decode_recovery_reply(
    request: RecoveryRequest, payload: dict | bytes
) -> RecoveryReply:
    """Validate a wire reply and bind it to the requested operation nonce."""
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"] is False:
            failure = RecoveryFailureReply.model_validate_json(encoded).error
            _raise_wire_failure(request, failure)
        reply = RecoverySuccess.model_validate_json(encoded)
        if (
            reply.operation != request.operation
            or reply.operation_id != request.operation_id
        ):
            raise IdentityUnavailable()
        if reply.data.account is not None and request.operation == "register_open":
            if reply.data.account.email != request.payload.email:
                raise IdentityUnavailable()
        return reply
    except RecoveryFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def _raise_wire_failure(
    request: RecoveryRequest, failure: RecoveryFailureDetailModel
) -> None:
    detail = failure.detail
    if not _safe_failure_for_operation(request.operation, failure, request=request):
        raise IdentityUnavailable()
    raise RecoveryFailure(failure.code, failure.status_code, detail)


def _safe_failure_for_operation(
    operation: str,
    failure: RecoveryFailureDetailModel,
    *,
    request: RecoveryRequest | None = None,
) -> bool:
    if failure.code == "registration_disabled":
        return operation == "register_open"
    if failure.code == "email_exists":
        return operation == "register_open"
    if failure.code == "captcha_required":
        return operation in {"register_open", "request_password_reset"}
    if failure.code == "policy_rejected":
        if request is None or operation not in {
            "register_open",
            "consume_password_reset",
        }:
            return False
        expected_field = "password" if operation == "register_open" else "new_password"
        password = (
            request.payload.password
            if isinstance(request, RegisterOpenRequest)
            else request.payload.new_password
        )
        errors = validate_password_strength(password)
        expected_message = "Value error, " + "; ".join(errors)
        return (
            bool(errors)
            and bool(failure.detail)
            and all(
                issue.type == "value_error"
                and issue.loc == ["body", expected_field]
                and issue.msg == expected_message
                for issue in failure.detail
            )
        )
    if failure.code == "operation_conflict":
        return True
    if failure.code == "invalid_request":
        return operation in {
            "consume_password_reset",
            "consume_email_verification",
        }
    return failure.code == "unavailable"


class RecoveryActor(ClosedModel):
    id: uuid.UUID
    email: str = Field(max_length=255, strict=True)
    role: Literal["super_admin", "project_admin", "employee", "viewer"]
    is_active: bool
    credential_generation: int = Field(ge=0, lt=2**63, strict=True)
    email_verified_at: datetime | None

    @field_validator("is_active", mode="before")
    @classmethod
    def strict_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Actor active state must be a boolean")
        return value

    @field_validator("email_verified_at")
    @classmethod
    def aware_verified_at(cls, value: datetime | None):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Verification time must include a timezone")
            return value.astimezone(UTC)
        return None


class RecoveryTokenRecord(ClosedModel):
    id: uuid.UUID
    token_hash: str = Field(pattern=_HEX_64, strict=True, repr=False)
    issued_email: str = Field(max_length=255, strict=True)
    expires_at: datetime
    used_at: datetime | None

    @field_validator("expires_at", "used_at")
    @classmethod
    def aware_token_times(cls, value: datetime | None):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Token timestamps must include a timezone")
            return value.astimezone(UTC)
        return None


class RecoveryPolicy(ClosedModel):
    allow_open_registration: bool
    email_verification_required: bool
    frontend_base_url: str = Field(min_length=1, max_length=2048, strict=True)
    expire_minutes: int = Field(ge=1, le=525600, strict=True)
    password_reset_ttl_seconds: Literal[3600]
    email_verification_ttl_seconds: Literal[86400]

    @field_validator(
        "allow_open_registration", "email_verification_required", mode="before"
    )
    @classmethod
    def strict_policy_bools(cls, value):
        if type(value) is not bool:
            raise ValueError("Recovery policy flags must be booleans")
        return value


class NativeRecoveryPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-recovery-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-recovery/v1"]


def decode_recovery_policy(payload: dict | bytes) -> NativeRecoveryPolicyReply:
    try:
        return NativeRecoveryPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class _NativeArgs(ClosedModel):
    pass


class NativeRegisterArgs(_NativeArgs):
    email: str = Field(min_length=3, max_length=255, strict=True)
    name: str = Field(min_length=1, max_length=100, strict=True)
    captcha_verified: bool

    @field_validator("captcha_verified", mode="before")
    @classmethod
    def strict_captcha(cls, value):
        if type(value) is not bool:
            raise ValueError("captcha_verified must be a boolean")
        return value


class NativeRequestPasswordResetArgs(_NativeArgs):
    email: str = Field(min_length=3, max_length=255, strict=True)
    captcha_verified: bool

    @field_validator("captcha_verified", mode="before")
    @classmethod
    def strict_captcha(cls, value):
        if type(value) is not bool:
            raise ValueError("captcha_verified must be a boolean")
        return value


class NativeResendVerificationArgs(_NativeArgs):
    email: str = Field(min_length=3, max_length=255, strict=True)


class NativeConsumeArgs(_NativeArgs):
    token_hash: str = Field(pattern=_HEX_64, strict=True, repr=False)


class NativeRecoveryConfig(ClosedModel):
    allow_open_registration: bool
    email_verification_required: bool
    frontend_base_url: str = Field(min_length=1, max_length=2048, strict=True)
    expire_minutes: int = Field(ge=1, le=525600, strict=True)

    @field_validator(
        "allow_open_registration", "email_verification_required", mode="before"
    )
    @classmethod
    def strict_config_bools(cls, value):
        if type(value) is not bool:
            raise ValueError("Recovery config flags must be booleans")
        return value


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-recovery-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    request_fingerprint: str = Field(pattern=_HEX_64, strict=True, repr=False)
    derivation_id: str = Field(pattern=_HEX_64, strict=True)
    audit_context: SessionAuditContext
    config: NativeRecoveryConfig


class NativeRegisterPrepareCommand(_NativeCommandBase):
    operation: Literal["register_open"]
    args: NativeRegisterArgs


class NativeRequestPrepareCommand(_NativeCommandBase):
    operation: Literal["request_password_reset"]
    args: NativeRequestPasswordResetArgs


class NativeResendPrepareCommand(_NativeCommandBase):
    operation: Literal["resend_email_verification"]
    args: NativeResendVerificationArgs


class NativeResetPrepareCommand(_NativeCommandBase):
    operation: Literal["consume_password_reset"]
    args: NativeConsumeArgs


class NativeVerifyPrepareCommand(_NativeCommandBase):
    operation: Literal["consume_email_verification"]
    args: NativeConsumeArgs


NativeRecoveryPrepareCommand: TypeAlias = Annotated[
    NativeRegisterPrepareCommand
    | NativeRequestPrepareCommand
    | NativeResendPrepareCommand
    | NativeResetPrepareCommand
    | NativeVerifyPrepareCommand,
    Field(discriminator="operation"),
]
_NATIVE_RECOVERY_PREPARE_COMMANDS = TypeAdapter(NativeRecoveryPrepareCommand)


class _NativeApplyBase(_NativeCommandBase):
    password_hash: str | None = Field(
        default=None, pattern=_BCRYPT12, strict=True, repr=False
    )
    token_hash: str | None = Field(
        default=None, pattern=_HEX_64, strict=True, repr=False
    )
    token_ciphertext: str | None = Field(
        default=None, min_length=1, max_length=8192, strict=True, repr=False
    )
    encryption_id: str | None = Field(default=None, pattern=_HEX_64, strict=True)

    @model_validator(mode="after")
    def encrypted_token_shape(self):
        if (self.token_ciphertext is None) != (self.encryption_id is None):
            raise ValueError("Recovery mail ciphertext requires its key identifier")
        if (self.token_ciphertext is None) != (
            self.token_hash is None
        ) and self.operation in {
            "register_open",
            "request_password_reset",
            "resend_email_verification",
        }:
            raise ValueError("Mail challenge hash and ciphertext must travel together")
        if self.operation in {"consume_password_reset", "consume_email_verification"}:
            if self.token_ciphertext is not None or self.encryption_id is not None:
                raise ValueError("Token consumption cannot carry mail ciphertext")
            args_hash = self.args.token_hash
            if self.token_hash != args_hash:
                raise ValueError("Consumed token hash must match prepared arguments")
        return self


class NativeRegisterApplyCommand(_NativeApplyBase):
    operation: Literal["register_open"]
    args: NativeRegisterArgs


class NativeRequestApplyCommand(_NativeApplyBase):
    operation: Literal["request_password_reset"]
    args: NativeRequestPasswordResetArgs


class NativeResendApplyCommand(_NativeApplyBase):
    operation: Literal["resend_email_verification"]
    args: NativeResendVerificationArgs


class NativeResetApplyCommand(_NativeApplyBase):
    operation: Literal["consume_password_reset"]
    args: NativeConsumeArgs


class NativeVerifyApplyCommand(_NativeApplyBase):
    operation: Literal["consume_email_verification"]
    args: NativeConsumeArgs


NativeRecoveryApplyCommand: TypeAlias = Annotated[
    NativeRegisterApplyCommand
    | NativeRequestApplyCommand
    | NativeResendApplyCommand
    | NativeResetApplyCommand
    | NativeVerifyApplyCommand,
    Field(discriminator="operation"),
]
_NATIVE_RECOVERY_APPLY_COMMANDS = TypeAdapter(NativeRecoveryApplyCommand)


def _decode_native_command(payload, adapter) -> Any:
    try:
        return adapter.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_recovery_prepare_command(
    payload: dict | bytes,
) -> NativeRecoveryPrepareCommand:
    return _decode_native_command(payload, _NATIVE_RECOVERY_PREPARE_COMMANDS)


def decode_native_recovery_apply_command(
    payload: dict | bytes,
) -> NativeRecoveryApplyCommand:
    return _decode_native_command(payload, _NATIVE_RECOVERY_APPLY_COMMANDS)


def native_recovery_prepare_command(
    request: RecoveryRequest,
    *,
    request_fingerprint: str,
    derivation_id: str,
    config: NativeRecoveryConfig,
):
    args = request.payload.model_dump(
        mode="json", by_alias=True, exclude={"password", "new_password", "token"}
    )
    if request.operation in {"consume_password_reset", "consume_email_verification"}:
        from .recovery_crypto import token_hash

        args = {"token_hash": token_hash(request.payload.token)}
    payload = {
        "schema": NATIVE_RECOVERY_PROTOCOL,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "request_fingerprint": request_fingerprint,
        "derivation_id": derivation_id,
        "audit_context": request.audit_context.model_dump(mode="json", by_alias=True),
        "config": (
            config.model_dump(mode="json", by_alias=True)
            if isinstance(config, ClosedModel)
            else config
        ),
        "args": args,
    }
    return decode_native_recovery_prepare_command(payload)


class NativeRecoveryResult(ClosedModel):
    account: AccountRecord | None
    claims: SessionClaims | None
    email_verification_required: bool | None
    completed: bool | None

    @field_validator("email_verification_required", "completed", mode="before")
    @classmethod
    def strict_optional_flags(cls, value):
        if value is not None and type(value) is not bool:
            raise ValueError("Native recovery flags must be booleans")
        return value

    def validate_for(self, operation: RecoveryOperation, operation_id: uuid.UUID):
        if operation == "register_open":
            if (
                self.account is None
                or self.email_verification_required is None
                or self.completed is not None
                or self.account.id != operation_id
                or self.account.role != "viewer"
                or self.account.group_id is not None
                or self.account.group_name is not None
                or not self.account.is_active
                or self.account.last_login_at is not None
            ):
                raise IdentityUnavailable()
            if self.email_verification_required:
                if (
                    self.claims is not None
                    or self.account.email_verified_at is not None
                    or self.account.status != "offline"
                ):
                    raise IdentityUnavailable()
            elif (
                self.claims is None
                or self.account.email_verified_at is None
                or self.account.status != "online"
            ):
                raise IdentityUnavailable()
            if self.claims is not None and (
                self.claims.sub != self.account.id
                or self.claims.jti != operation_id
                or self.claims.role != self.account.role
                or self.claims.gen != 0
                or self.claims.iss != "aap-identity/v1"
            ):
                raise IdentityUnavailable()
        elif operation in {"request_password_reset", "resend_email_verification"}:
            if any(
                value is not None
                for value in (
                    self.account,
                    self.claims,
                    self.email_verification_required,
                    self.completed,
                )
            ):
                raise IdentityUnavailable()
        elif (
            self.account is not None
            or self.claims is not None
            or self.email_verification_required is not None
            or self.completed is None
        ):
            raise IdentityUnavailable()
        return self


class NativeRecoveryPrepareData(ClosedModel):
    actor: RecoveryActor | None
    password_hash: str | None = Field(pattern=_BCRYPT12, strict=True, repr=False)
    token: RecoveryTokenRecord | None
    eligible: bool
    replay: NativeRecoveryResult | None
    policy: RecoveryPolicy

    @field_validator("eligible", mode="before")
    @classmethod
    def strict_eligible(cls, value):
        if type(value) is not bool:
            raise ValueError("Recovery eligibility must be a boolean")
        return value


class NativeRecoveryPrepareReply(ClosedModel):
    protocol: Literal["aap-identity-recovery-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: RecoveryOperation
    operation_id: uuid.UUID
    data: NativeRecoveryPrepareData


class NativeRecoveryApplyData(ClosedModel):
    result: NativeRecoveryResult
    actor: RecoveryActor | None
    replayed: bool
    password_changed: bool
    email_verified_changed: bool

    @field_validator(
        "replayed", "password_changed", "email_verified_changed", mode="before"
    )
    @classmethod
    def strict_apply_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("Native recovery apply flags must be booleans")
        return value


class NativeRecoveryApplyReply(ClosedModel):
    protocol: Literal["aap-identity-recovery-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: RecoveryOperation
    operation_id: uuid.UUID
    data: NativeRecoveryApplyData


class NativeRecoveryFailureDetail(ClosedModel):
    code: Literal[
        "registration_disabled",
        "email_exists",
        "captcha_required",
        "operation_conflict",
        "invalid_request",
        "unavailable",
    ]


class NativeRecoveryFailureReply(ClosedModel):
    protocol: Literal["aap-identity-recovery-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: NativeRecoveryFailureDetail


def _safe_native_failure(operation: RecoveryOperation, code: str) -> bool:
    if code in {"registration_disabled", "email_exists"}:
        return operation == "register_open"
    if code == "captcha_required":
        return operation in {"register_open", "request_password_reset"}
    if code == "invalid_request":
        return operation in {
            "consume_password_reset",
            "consume_email_verification",
        }
    return code in {"operation_conflict", "unavailable"}


def _native_failure(payload, command):
    encoded = _json_bytes(payload)
    raw = json.loads(encoded, object_pairs_hook=_unique_object)
    if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
        raise IdentityUnavailable()
    if raw["ok"]:
        try:
            reply = (
                NativeRecoveryApplyReply.model_validate_json(encoded)
                if isinstance(command, _NativeApplyBase)
                else NativeRecoveryPrepareReply.model_validate_json(encoded)
            )
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise IdentityUnavailable() from None
        return reply
    try:
        failure = NativeRecoveryFailureReply.model_validate_json(encoded).error
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not _safe_native_failure(command.operation, failure.code):
        raise IdentityUnavailable()
    raise RecoveryFailure(failure.code)


def _validate_prepare_reply(command, reply: NativeRecoveryPrepareReply):
    if (
        reply.operation != command.operation
        or reply.operation_id != command.operation_id
    ):
        raise IdentityUnavailable()
    data = reply.data
    if command.operation in {"consume_password_reset", "consume_email_verification"}:
        if data.token is not None and data.token.token_hash != command.args.token_hash:
            raise IdentityUnavailable()
    if data.password_hash is not None and command.operation != "register_open":
        raise IdentityUnavailable()
    if data.replay is not None:
        data.replay.validate_for(command.operation, command.operation_id)
        if data.password_hash is not None and data.replay.claims is None:
            raise IdentityUnavailable()
    elif data.password_hash is not None:
        raise IdentityUnavailable()
    if command.operation == "register_open" and data.replay is not None:
        if (
            data.actor is None
            or data.actor.id != command.operation_id
            or data.actor.email != command.args.email
        ):
            raise IdentityUnavailable()
        if not data.actor.is_active:
            raise IdentityUnavailable()
    elif command.operation == "register_open" and data.actor is not None:
        raise IdentityUnavailable()
    if (
        command.operation
        in {
            "request_password_reset",
            "resend_email_verification",
        }
        and data.actor is not None
    ):
        if data.actor.email != command.args.email:
            raise IdentityUnavailable()
    if (
        command.operation in {"consume_password_reset", "consume_email_verification"}
        and data.token is not None
        and data.actor is None
    ):
        raise IdentityUnavailable()
    if data.token is not None and data.actor is not None:
        if data.token.issued_email != data.actor.email:
            raise IdentityUnavailable()
    return reply


def decode_native_recovery_prepare_reply(
    command: NativeRecoveryPrepareCommand, payload: dict | bytes
) -> NativeRecoveryPrepareReply:
    try:
        reply = _native_failure(payload, command)
        if not isinstance(reply, NativeRecoveryPrepareReply):
            raise IdentityUnavailable()
        return _validate_prepare_reply(command, reply)
    except RecoveryFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def decode_native_recovery_reply(
    command: NativeRecoveryApplyCommand, payload: dict | bytes
) -> NativeRecoveryApplyReply:
    try:
        reply = _native_failure(payload, command)
        if not isinstance(reply, NativeRecoveryApplyReply):
            raise IdentityUnavailable()
        if (
            reply.operation != command.operation
            or reply.operation_id != command.operation_id
        ):
            raise IdentityUnavailable()
        reply.data.result.validate_for(command.operation, command.operation_id)
        if reply.data.replayed and (
            reply.data.password_changed or reply.data.email_verified_changed
        ):
            raise IdentityUnavailable()
        if command.operation == "register_open":
            if reply.data.result.account is None or reply.data.actor is None:
                raise IdentityUnavailable()
            if reply.data.result.account.email != command.args.email:
                raise IdentityUnavailable()
            if (
                reply.data.result.account.id != command.operation_id
                or reply.data.actor.id != command.operation_id
                or reply.data.actor.email != command.args.email
                or not reply.data.actor.is_active
            ):
                raise IdentityUnavailable()
            if (
                not reply.data.replayed
                and reply.data.actor.role != reply.data.result.account.role
            ):
                raise IdentityUnavailable()
        elif command.operation in {
            "consume_password_reset",
            "consume_email_verification",
        }:
            # A consumed inactive reset is valid with completed=false. The DB may
            # return a fresh actor summary or null for invalid/deleted targets.
            pass
        return reply
    except RecoveryFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def native_recovery_apply_command(
    prepared: NativeRecoveryPrepareCommand,
    *,
    password_hash: str | None,
    token_hash: str | None,
    token_ciphertext: str | None,
    encryption_id: str | None,
) -> NativeRecoveryApplyCommand:
    payload = {
        **prepared.model_dump(mode="json", by_alias=True),
        "password_hash": password_hash,
        "token_hash": token_hash,
        "token_ciphertext": token_ciphertext,
        "encryption_id": encryption_id,
    }
    return decode_native_recovery_apply_command(payload)
