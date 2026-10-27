"""Closed wire/native contracts for self-account command ownership."""

from __future__ import annotations

import json
import math
import re
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

from .contract import (
    ClosedModel,
    FailureCode,
    IdentityUnavailable,
    Principal,
    _reject_protocol_alias,
)
from .password_policy import validate_password_strength
from .session_contract import SessionAuditContext

ACCOUNT_PROTOCOL = "aap-identity-accounts/v1"
NATIVE_ACCOUNT_PROTOCOL = "aap-identity-accounts-native/v1"
ACCOUNT_POLICY_PROTOCOL = "aap-identity-accounts-policy/v1"
ACCOUNT_DERIVATION = "aap-identity-accounts/v1"
_HEX_64 = r"^[0-9a-f]{64}$"
_UPLOAD_REF = r"^upload:[0-9a-f]{32}$"
_PRESET_REF = r"^preset:[a-z0-9-]{1,40}$"
_BCRYPT12 = r"^\$2[aby]\$12\$[./A-Za-z0-9]{53}$"

AccountOperation = Literal[
    "read_self_account",
    "get_preferences",
    "heartbeat",
    "update_name",
    "patch_preferences",
    "change_password",
    "set_avatar_ref",
]
AccountFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "account_busy",
        "operation_conflict",
        "policy_rejected",
        "avatar_reference_changed",
        "avatar_receipt_invalid",
        "storage_maintenance",
        "storage_maintenance_changed",
        "storage_maintenance_busy",
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
            payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        _reject_protocol_alias(value)
        _assert_finite(value)
        return payload
    elif isinstance(payload, ClosedModel):
        _assert_finite(payload)
        value = payload.model_dump(mode="json", by_alias=True)
    else:
        value = payload
    _reject_protocol_alias(value)
    _assert_finite(value)
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


class EmptyPayload(ClosedModel):
    pass


class UpdateNamePayload(ClosedModel):
    name: str = Field(min_length=1, max_length=100, strict=True)


class PatchPreferencesPayload(ClosedModel):
    patch: dict[str, Any]


class ChangePasswordPayload(ClosedModel):
    old_password: str = Field(min_length=1, max_length=128, strict=True, repr=False)
    new_password: str = Field(min_length=8, max_length=128, strict=True, repr=False)


_AVATAR_STORAGE_REF_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "location_id",
        "config_revision",
        "binding_generation",
        "endpoint_url",
        "bucket",
        "object_key",
        "storage_identity",
    }
)
_LEGACY_AVATAR_LOCATION = "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7"
_AVATAR_STORAGE_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_AVATAR_LOCATION = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_AVATAR_ENDPOINT = re.compile(r"^https?://[^\s/?#@\\]+$")


def _avatar_ref_shape(value: str | None) -> bool:
    return value is None or (
        re.fullmatch(_UPLOAD_REF, value) is not None
        or re.fullmatch(_PRESET_REF, value) is not None
    )


class AvatarUploadReceipt(ClosedModel):
    """Provider-confirmed facts for one pending normalized avatar object.

    The Identity owner compares this receipt with its own pending row before it
    publishes the account reference. The API cannot mint a ready receipt for a
    token it does not own, and cannot publish bytes it never read back.
    """

    token: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True, repr=False)
    sha256: str = Field(pattern=_HEX_64, strict=True, repr=False)
    size_bytes: int = Field(ge=1, le=2097152, strict=True)
    object_version_id: str | None = Field(
        default=None, min_length=1, max_length=1024, strict=True, repr=False
    )
    maintenance_revision: int = Field(ge=1, le=2**63 - 1, strict=True, repr=False)
    storage_ref: dict[str, Any] = Field(repr=False)

    @field_validator("object_version_id")
    @classmethod
    def printable_version(cls, value: str | None) -> str | None:
        if value is not None and any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("Avatar provider version must be printable")
        return value

    @model_validator(mode="after")
    def exact_storage_identity(self) -> AvatarUploadReceipt:
        ref = self.storage_ref
        if (
            type(ref) is not dict
            or set(ref) != _AVATAR_STORAGE_REF_FIELDS
            or type(ref["schema"]) is not int
            or ref["schema"] not in {1, 2}
            or ref["purpose"] != "avatars"
            or not isinstance(ref["location_id"], str)
            or _AVATAR_LOCATION.fullmatch(ref["location_id"]) is None
            or type(ref["config_revision"]) is not int
            or ref["config_revision"] < 1
            or type(ref["binding_generation"]) is not int
            or ref["binding_generation"] < 1
            or not isinstance(ref["endpoint_url"], str)
            or _AVATAR_ENDPOINT.fullmatch(ref["endpoint_url"]) is None
            or not isinstance(ref["bucket"], str)
            or not 0 < len(ref["bucket"]) <= 255
            or any(char in ref["bucket"] for char in "/\\?#@ ")
            or not isinstance(ref["object_key"], str)
            or not ref["object_key"]
            or len(ref["object_key"]) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in ref["object_key"])
            or not isinstance(ref["storage_identity"], str)
            or _AVATAR_STORAGE_IDENTITY.fullmatch(ref["storage_identity"]) is None
        ):
            raise ValueError("Avatar storage receipt is not canonical")
        key = ref["object_key"]
        basename = key.rsplit("/", 1)[-1]
        if basename != self.token + ".webp":
            raise ValueError("Avatar object key does not match its token")
        if ref["schema"] == 1:
            if (
                ref["location_id"] != _LEGACY_AVATAR_LOCATION
                or ref["config_revision"] != 1
                or ref["binding_generation"] != 1
                or key != basename
            ):
                raise ValueError("Legacy avatar receipt has a non-legacy shape")
        else:
            if ref["binding_generation"] < 2:
                raise ValueError("Avatar binding generation must advance")
            if ref["location_id"] == _LEGACY_AVATAR_LOCATION:
                if ref["config_revision"] != 1 or key != basename:
                    raise ValueError("Legacy location keeps the root object key")
            elif not key.endswith("_aap_managed/avatars/" + basename):
                raise ValueError("Managed avatar object key has no managed prefix")
        return self


class AvatarAcceptedKeyProof(ClosedModel):
    """Exact frozen facts of the accepted API key that authorized publication.

    The Identity owner compares this proof with the key's current non-secret
    facts inside its own publication transaction. ``revision`` is a digest of
    the stored verifier, never the verifier itself, and ``scopes`` is the whole
    ordered snapshot so a rewritten key cannot inherit this operation.
    """

    scopes: list[Annotated[str, Field(strict=True, min_length=1, max_length=128)]]
    expires_at: datetime | None
    revision: str = Field(pattern=_HEX_64, strict=True, repr=False)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Accepted key expiry must include a timezone")
            return value.astimezone(UTC)
        return None


class SetAvatarRefPayload(ClosedModel):
    avatar_ref: str | None = Field(default=None, max_length=80, strict=True)
    kind: Literal["upload", "preset_or_clear"]
    bytes: int | None = Field(default=None, ge=0, strict=True)
    expected_previous_avatar_ref: str | None = Field(
        default=None, max_length=80, strict=True
    )
    receipt: AvatarUploadReceipt | None = Field(default=None, repr=False)
    accepted_api_key_proof: AvatarAcceptedKeyProof | None = Field(
        default=None, repr=False
    )

    @model_validator(mode="after")
    def avatar_shape(self) -> SetAvatarRefPayload:
        if not _avatar_ref_shape(self.expected_previous_avatar_ref):
            raise ValueError("Expected previous avatar reference is malformed")
        if self.kind == "upload":
            if (
                self.avatar_ref is None
                or re.fullmatch(_UPLOAD_REF, self.avatar_ref) is None
                or type(self.bytes) is not int
                or "expected_previous_avatar_ref" not in self.model_fields_set
                or self.receipt is None
                or self.receipt.token != self.avatar_ref[len("upload:") :]
                or self.receipt.size_bytes != self.bytes
                or self.expected_previous_avatar_ref == self.avatar_ref
            ):
                raise ValueError("Upload avatar needs its verified owned receipt")
        elif (
            self.bytes is not None
            or self.receipt is not None
            or (
                self.avatar_ref is not None
                and re.fullmatch(_PRESET_REF, self.avatar_ref) is None
            )
        ):
            raise ValueError("Preset avatar accepts only a preset ref or clear")
        return self


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-accounts/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class ReadSelfAccountRequest(_WireRequest):
    operation: Literal["read_self_account"]
    payload: EmptyPayload


class GetPreferencesRequest(_WireRequest):
    operation: Literal["get_preferences"]
    payload: EmptyPayload


class HeartbeatRequest(_WireRequest):
    operation: Literal["heartbeat"]
    payload: EmptyPayload


class UpdateNameRequest(_WireRequest):
    operation: Literal["update_name"]
    payload: UpdateNamePayload


class PatchPreferencesRequest(_WireRequest):
    operation: Literal["patch_preferences"]
    payload: PatchPreferencesPayload


class ChangePasswordRequest(_WireRequest):
    operation: Literal["change_password"]
    payload: ChangePasswordPayload


class SetAvatarRefRequest(_WireRequest):
    operation: Literal["set_avatar_ref"]
    payload: SetAvatarRefPayload


AccountRequest: TypeAlias = Annotated[
    ReadSelfAccountRequest
    | GetPreferencesRequest
    | HeartbeatRequest
    | UpdateNameRequest
    | PatchPreferencesRequest
    | ChangePasswordRequest
    | SetAvatarRefRequest,
    Field(discriminator="operation"),
]
_ACCOUNT_REQUESTS = TypeAdapter(AccountRequest)


def decode_account_request(payload: dict | bytes) -> AccountRequest:
    """Decode one exact account command; duplicate keys and non-JSON numbers fail."""
    return _ACCOUNT_REQUESTS.validate_json(_json_bytes(payload))


class AccountRecord(ClosedModel):
    id: uuid.UUID
    email: str = Field(max_length=255, strict=True)
    name: str = Field(max_length=100, strict=True)
    role: Literal["super_admin", "project_admin", "employee", "viewer"]
    group_name: str | None = Field(max_length=100, strict=True)
    group_id: uuid.UUID | None
    status: str = Field(max_length=20, strict=True)
    is_active: bool
    disabled_kind: str | None = Field(max_length=32, strict=True)
    disabled_at: datetime | None
    disabled_by: uuid.UUID | None
    disabled_reason: str | None = Field(max_length=500, strict=True)
    email_verified_at: datetime | None
    last_login_at: datetime | None
    last_seen_at: datetime | None
    password_admin_reset_at: datetime | None
    deactivation_requested_at: datetime | None
    deactivation_scheduled_at: datetime | None
    preferences: dict[str, Any]
    avatar_ref: str | None = Field(max_length=80, strict=True)
    created_at: datetime

    @field_validator(
        "is_active",
        mode="before",
    )
    @classmethod
    def strict_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Account active flag must be a boolean")
        return value

    @field_validator(
        "disabled_at",
        "email_verified_at",
        "last_login_at",
        "last_seen_at",
        "password_admin_reset_at",
        "deactivation_requested_at",
        "deactivation_scheduled_at",
        "created_at",
    )
    @classmethod
    def aware_times(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Account timestamps must include a timezone")
            return value.astimezone(UTC)
        return None


class AccountResultData(ClosedModel):
    account: AccountRecord | None
    preferences: dict[str, Any] | None
    previous_avatar_ref: str | None = Field(max_length=80, strict=True)
    changed: bool | None

    @field_validator("changed", mode="before")
    @classmethod
    def strict_changed(cls, value):
        if value is not None and type(value) is not bool:
            raise ValueError("Account changed flag must be a boolean")
        return value


def _validate_result(operation: str, data: AccountResultData) -> None:
    account = data.account is not None
    preferences = data.preferences is not None
    previous = data.previous_avatar_ref is not None
    changed = data.changed is not None
    valid = {
        "read_self_account": (
            account and not preferences and not previous and not changed
        ),
        "get_preferences": (
            not account and preferences and not previous and not changed
        ),
        "heartbeat": (not account and not preferences and not previous and not changed),
        "update_name": (account and not preferences and not previous and changed),
        "patch_preferences": (
            not account and preferences and not previous and not changed
        ),
        "change_password": (
            not account and not preferences and not previous and not changed
        ),
        "set_avatar_ref": (
            account
            and not preferences
            and changed
            and (data.changed is True or data.previous_avatar_ref is None)
        ),
    }
    if operation not in valid or not valid[operation]:
        raise IdentityUnavailable()


class AccountReply(ClosedModel):
    protocol: Literal["aap-identity-accounts/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: AccountOperation
    operation_id: uuid.UUID
    principal: Principal
    data: AccountResultData

    @model_validator(mode="after")
    def operation_data_shape(self) -> AccountReply:
        _validate_result(self.operation, self.data)
        if (
            self.data.account is not None
            and self.data.account.id != self.principal.actor_id
        ):
            raise ValueError("Account record does not match the principal")
        return self


class AccountValidationIssue(ClosedModel):
    type: str = Field(min_length=1, strict=True)
    loc: list[str | int]
    msg: str = Field(strict=True)


class NamedPresetsConflictDetail(ClosedModel):
    code: Literal["named_presets_conflict"]
    message: Literal["命名布局预设已在其他会话更新，请刷新后重试"]
    currentRevision: str | None = Field(default=None, strict=True)


AccountPolicyDetail: TypeAlias = (
    str | list[AccountValidationIssue] | NamedPresetsConflictDetail
)


class AccountFailureDetail(ClosedModel):
    code: AccountFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: AccountPolicyDetail | None

    @model_validator(mode="after")
    def failure_detail_matches_code(self) -> AccountFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Account failure requires status_code and detail fields")
        if self.code == "policy_rejected":
            if self.status_code not in {400, 409, 422} or self.detail is None:
                raise ValueError("Policy rejection needs a supported status and detail")
        elif self.status_code is not None or self.detail is not None:
            raise ValueError("Only policy rejections carry status and detail")
        return self


class AccountFailureReply(ClosedModel):
    protocol: Literal["aap-identity-accounts/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: AccountFailureDetail


class AccountFailure(Exception):
    def __init__(
        self,
        code: AccountFailureCode,
        status_code: int | None = None,
        detail: AccountPolicyDetail | dict | None = None,
    ):
        parsed = AccountFailureDetail(code=code, status_code=status_code, detail=detail)
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = _detail_value(parsed.detail)
        super().__init__(code)


def _detail_value(detail: AccountPolicyDetail | None):
    if isinstance(detail, NamedPresetsConflictDetail):
        return detail.model_dump(mode="json", exclude_none=True)
    if isinstance(detail, list):
        return [item.model_dump(mode="json", by_alias=True) for item in detail]
    return detail


def account_failure_reply(failure: AccountFailure | AccountFailureCode) -> dict:
    if isinstance(failure, AccountFailure):
        code, status_code, detail = (
            failure.code,
            failure.status_code,
            failure.detail,
        )
    else:
        code, status_code, detail = failure, None, None
    parsed = AccountFailureReply(
        schema=ACCOUNT_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    )
    return parsed.model_dump(mode="json", by_alias=True)


_ACCOUNT_REPLIES = TypeAdapter(AccountReply)


def _same_fenced_credential(actual: Principal, expected: Principal) -> bool:
    same = (
        actual.actor_id == expected.actor_id
        and actual.credential_type == expected.credential_type
        and actual.credential_id == expected.credential_id
        and actual.generation == expected.generation
    )
    return same and (
        expected.credential_type != "jwt" or actual.expires_at == expected.expires_at
    )


def _safe_error_for_operation(
    operation: str,
    error: AccountFailureDetail,
    *,
    request: AccountRequest | None = None,
) -> bool:
    if error.code != "policy_rejected":
        return True
    detail = _detail_value(error.detail)
    if operation == "update_name":
        return error.status_code == 400 and detail == "姓名不能为空"
    if operation == "change_password":
        if error.status_code == 400:
            return detail in {"原密码不正确", "新密码不能与原密码相同"}
        if error.status_code != 422 or not isinstance(detail, list) or len(detail) != 1:
            return False
        issue = detail[0]
        if (
            issue.get("type") != "value_error"
            or issue.get("loc") != ["body", "new_password"]
            or not isinstance(issue.get("msg"), str)
        ):
            return False
        if isinstance(request, ChangePasswordRequest):
            errors = validate_password_strength(request.payload.new_password)
            expected = "Value error, " + "; ".join(errors) if errors else None
            return expected is not None and issue["msg"] == expected
        return issue["msg"] in _allowed_password_strength_messages()
    if operation == "patch_preferences":
        if error.status_code == 422:
            if detail == "named_presets_revision_read_only":
                return True
            return (
                isinstance(detail, list)
                and bool(detail)
                and all(item.get("loc", [])[:1] == ["body"] for item in detail)
            )
        if error.status_code == 409:
            return (
                isinstance(detail, str)
                and detail
                in {
                    "layout_engine_downgrade",
                    "layout_schema_downgrade",
                }
            ) or (
                isinstance(detail, dict)
                and detail.get("code") == "named_presets_conflict"
                and detail.get("message")
                == "命名布局预设已在其他会话更新，请刷新后重试"
                and (
                    "currentRevision" not in detail
                    or isinstance(detail["currentRevision"], str)
                )
            )
    return False


def _allowed_password_strength_messages() -> set[str]:
    messages = [
        "密码需包含大写字母",
        "密码需包含小写字母",
        "密码需包含数字",
    ]
    values = set()
    for mask in range(1, 1 << len(messages)):
        selected = [
            message for index, message in enumerate(messages) if mask & (1 << index)
        ]
        values.add("Value error, " + "; ".join(selected))
    return values


def _failure_text(error: AccountFailureDetail) -> str:
    return json.dumps(
        _detail_value(error.detail), ensure_ascii=True, separators=(",", ":")
    )


def _reject_secret_echo(error: AccountFailureDetail, secrets: list[str | None]) -> None:
    text = _failure_text(error)
    if any(secret and secret in text for secret in secrets):
        raise IdentityUnavailable()


def _raise_wire_failure(request: AccountRequest, encoded: bytes) -> None:
    try:
        failure = AccountFailureReply.model_validate_json(encoded).error
    except (ValidationError, ValueError, TypeError):
        raise IdentityUnavailable() from None
    if not _safe_error_for_operation(request.operation, failure, request=request):
        raise IdentityUnavailable()
    secrets = [request.token]
    if isinstance(request, ChangePasswordRequest):
        secrets.extend([request.payload.old_password, request.payload.new_password])
    _reject_secret_echo(failure, secrets)
    raise AccountFailure(
        failure.code, failure.status_code, _detail_value(failure.detail)
    )


def decode_account_reply(
    request: AccountRequest,
    payload: dict | bytes,
    *,
    expected_principal: Principal | None = None,
) -> AccountReply:
    """Validate a closed account reply and bind it to the command/caller."""
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"] is False:
            _raise_wire_failure(request, encoded)
        reply = _ACCOUNT_REPLIES.validate_json(encoded)
        if (
            reply.operation != request.operation
            or reply.operation_id != request.operation_id
        ):
            raise IdentityUnavailable()
        if expected_principal is not None and not _same_fenced_credential(
            reply.principal, expected_principal
        ):
            raise IdentityUnavailable()
        if reply.data.account is not None:
            _assert_finite(reply.data.account.preferences)
        if reply.data.preferences is not None:
            _assert_finite(reply.data.preferences)
        return reply
    except AccountFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


class NativeAccountPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-accounts-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-accounts/v1"]


class NativePrepareCommand(ClosedModel):
    protocol: Literal["aap-identity-accounts-native/v1"] = Field(alias="schema")
    operation: AccountOperation
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=_HEX_64, strict=True)
    derivation_id: str = Field(pattern=_HEX_64, strict=True)
    audit_context: SessionAuditContext

    @model_validator(mode="after")
    def hash_matches_credential_kind(self) -> NativePrepareCommand:
        if self.actor.credential_type == "jwt":
            if self.expected_key_hash is not None:
                raise ValueError("JWT command cannot include an API-key hash")
        elif self.expected_key_hash is None:
            raise ValueError("API-key command needs its verified hash")
        return self


class _NativeApplyBase(ClosedModel):
    protocol: Literal["aap-identity-accounts-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=_HEX_64, strict=True)
    derivation_id: str = Field(pattern=_HEX_64, strict=True)
    audit_context: SessionAuditContext

    @model_validator(mode="after")
    def hash_matches_credential_kind(self):
        if self.actor.credential_type == "jwt":
            if self.expected_key_hash is not None:
                raise ValueError("JWT command cannot include an API-key hash")
        elif self.expected_key_hash is None:
            raise ValueError("API-key command needs its verified hash")
        return self


class NativeUpdateNameApplyCommand(_NativeApplyBase):
    operation: Literal["update_name"]
    base_value: str = Field(max_length=100, strict=True, repr=False)
    new_value: str = Field(min_length=1, max_length=100, strict=True, repr=False)
    response_preferences: None


class NativePatchPreferencesApplyCommand(_NativeApplyBase):
    operation: Literal["patch_preferences"]
    base_value: dict[str, Any] = Field(repr=False)
    new_value: dict[str, Any] = Field(repr=False)
    response_preferences: dict[str, Any] = Field(repr=False)


class NativeChangePasswordApplyCommand(_NativeApplyBase):
    operation: Literal["change_password"]
    base_value: str = Field(min_length=1, max_length=255, strict=True, repr=False)
    new_value: str = Field(pattern=_BCRYPT12, strict=True, repr=False)
    response_preferences: None


class NativeSetAvatarRefApplyCommand(_NativeApplyBase):
    operation: Literal["set_avatar_ref"]
    base_value: str | None = Field(max_length=80, strict=True, repr=False)
    new_value: SetAvatarRefPayload = Field(repr=False)
    response_preferences: None


class NativeHeartbeatApplyCommand(_NativeApplyBase):
    operation: Literal["heartbeat"]
    base_value: None
    new_value: None
    response_preferences: None


NativeApplyCommand: TypeAlias = Annotated[
    NativeUpdateNameApplyCommand
    | NativePatchPreferencesApplyCommand
    | NativeChangePasswordApplyCommand
    | NativeSetAvatarRefApplyCommand
    | NativeHeartbeatApplyCommand,
    Field(discriminator="operation"),
]
_NATIVE_APPLY_COMMANDS = TypeAdapter(NativeApplyCommand)


def _native_command_bytes(payload: dict | bytes | ClosedModel) -> bytes:
    try:
        return _json_bytes(payload)
    except (TypeError, ValueError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_prepare_command(payload: dict | bytes) -> NativePrepareCommand:
    try:
        return NativePrepareCommand.model_validate_json(_native_command_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_apply_command(payload: dict | bytes) -> NativeApplyCommand:
    try:
        return _NATIVE_APPLY_COMMANDS.validate_json(_native_command_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def native_account_prepare_command(
    request: AccountRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativePrepareCommand:
    return NativePrepareCommand(
        schema=NATIVE_ACCOUNT_PROTOCOL,
        operation=request.operation,
        operation_id=request.operation_id,
        actor=actor,
        expected_key_hash=expected_key_hash,
        request_fingerprint=request_fingerprint,
        derivation_id=derivation_id,
        audit_context=request.audit_context,
    )


def native_account_apply_command(
    prepared: NativePrepareCommand,
    *,
    base_value,
    new_value,
    response_preferences,
) -> NativeApplyCommand:
    values = {
        "schema": NATIVE_ACCOUNT_PROTOCOL,
        "operation": prepared.operation,
        "operation_id": prepared.operation_id,
        "actor": prepared.actor,
        "expected_key_hash": prepared.expected_key_hash,
        "request_fingerprint": prepared.request_fingerprint,
        "derivation_id": prepared.derivation_id,
        "audit_context": prepared.audit_context,
        "base_value": base_value,
        "new_value": new_value,
        "response_preferences": response_preferences,
    }
    try:
        return _NATIVE_APPLY_COMMANDS.validate_python(values)
    except (ValidationError, ValueError, TypeError):
        raise IdentityUnavailable() from None


class NativePrepareData(ClosedModel):
    account: AccountRecord
    password_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    replay: AccountResultData | None


class NativePrepareReply(ClosedModel):
    protocol: Literal["aap-identity-accounts-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: AccountOperation
    operation_id: uuid.UUID
    principal: Principal
    data: NativePrepareData


class NativeAccountReply(ClosedModel):
    protocol: Literal["aap-identity-accounts-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: AccountOperation
    operation_id: uuid.UUID
    principal: Principal
    data: AccountResultData


class NativeAccountFailureReply(ClosedModel):
    protocol: Literal["aap-identity-accounts-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: AccountFailureDetail


def _same_account_native_credential(actual: Principal, expected: Principal) -> bool:
    return _same_fenced_credential(actual, expected)


def _same_json_value(left: object, right: object) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _native_failure(
    payload: dict | bytes | ClosedModel,
    *,
    operation: str,
    command: NativePrepareCommand | NativeApplyCommand,
) -> NativePrepareReply | NativeAccountReply:
    if isinstance(command, NativePrepareCommand) and isinstance(
        payload, NativePrepareReply
    ):
        return payload
    if not isinstance(command, NativePrepareCommand) and isinstance(
        payload, NativeAccountReply
    ):
        return payload
    encoded = _native_command_bytes(payload)
    raw = json.loads(encoded, object_pairs_hook=_unique_object)
    if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
        raise IdentityUnavailable()
    if raw["ok"]:
        try:
            return (
                NativePrepareReply.model_validate_json(encoded)
                if isinstance(command, NativePrepareCommand)
                else NativeAccountReply.model_validate_json(encoded)
            )
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise IdentityUnavailable() from None
    try:
        failure = NativeAccountFailureReply.model_validate_json(encoded).error
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not _safe_error_for_operation(operation, failure):
        raise IdentityUnavailable()
    secrets = [command.expected_key_hash]
    if isinstance(command, NativeChangePasswordApplyCommand):
        secrets.extend([command.base_value, command.new_value])
    _reject_secret_echo(failure, secrets)
    raise AccountFailure(
        failure.code, failure.status_code, _detail_value(failure.detail)
    )


def decode_native_prepare_reply(
    command: NativePrepareCommand, payload: dict | bytes | ClosedModel
) -> NativePrepareReply:
    """Validate native prepare output before the owner transaction can commit."""
    try:
        reply = _native_failure(payload, operation=command.operation, command=command)
        if not isinstance(reply, NativePrepareReply):
            raise IdentityUnavailable()
        if (
            reply.operation != command.operation
            or reply.operation_id != command.operation_id
            or not _same_account_native_credential(reply.principal, command.actor)
            or reply.data.account.id != command.actor.actor_id
        ):
            raise IdentityUnavailable()
        _assert_finite(reply.data.account.preferences)
        if command.operation == "change_password":
            if reply.data.replay is None and reply.data.password_hash is None:
                raise IdentityUnavailable()
        elif reply.data.password_hash is not None:
            raise IdentityUnavailable()
        if reply.data.replay is not None:
            if command.operation not in {
                "update_name",
                "patch_preferences",
                "change_password",
                "set_avatar_ref",
            }:
                raise IdentityUnavailable()
            _validate_result(command.operation, reply.data.replay)
            if (
                reply.data.replay.account is not None
                and reply.data.replay.account.id != command.actor.actor_id
            ):
                raise IdentityUnavailable()
            if reply.data.replay.preferences is not None:
                _assert_finite(reply.data.replay.preferences)
        return reply
    except AccountFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def decode_native_account_reply(
    command: NativeApplyCommand, payload: dict | bytes | ClosedModel
) -> NativeAccountReply:
    """Validate native apply output before the owner transaction can commit."""
    try:
        reply = _native_failure(payload, operation=command.operation, command=command)
        if not isinstance(reply, NativeAccountReply):
            raise IdentityUnavailable()
        if (
            reply.operation != command.operation
            or reply.operation_id != command.operation_id
            or not _same_account_native_credential(reply.principal, command.actor)
        ):
            raise IdentityUnavailable()
        _validate_result(command.operation, reply.data)
        if (
            reply.data.account is not None
            and reply.data.account.id != command.actor.actor_id
        ):
            raise IdentityUnavailable()
        if reply.data.account is not None:
            _assert_finite(reply.data.account.preferences)
        if reply.data.preferences is not None:
            _assert_finite(reply.data.preferences)
        if isinstance(command, NativeUpdateNameApplyCommand):
            if reply.data.account.name != command.new_value or reply.data.changed != (
                command.base_value != command.new_value
            ):
                raise IdentityUnavailable()
        elif isinstance(command, NativePatchPreferencesApplyCommand):
            if not _same_json_value(
                reply.data.preferences, command.response_preferences
            ):
                raise IdentityUnavailable()
        elif isinstance(command, NativeSetAvatarRefApplyCommand):
            changed = command.base_value != command.new_value.avatar_ref
            if (
                reply.data.account.avatar_ref != command.new_value.avatar_ref
                or reply.data.changed is not changed
                or reply.data.previous_avatar_ref
                != (command.base_value if changed else None)
            ):
                raise IdentityUnavailable()
        return reply
    except AccountFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def decode_account_policy(payload: dict | bytes) -> NativeAccountPolicyReply:
    try:
        return NativeAccountPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
