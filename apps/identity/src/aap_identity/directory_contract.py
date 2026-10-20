"""Closed wire/native contracts for the Identity user directory read owner."""

from __future__ import annotations

import json
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

from .account_contract import _same_fenced_credential
from .contract import (
    ClosedModel,
    FailureCode,
    IdentityUnavailable,
    Principal,
    _reject_protocol_alias,
)
from .session_contract import SessionAuditContext

DIRECTORY_PROTOCOL = "aap-identity-directory/v1"
NATIVE_DIRECTORY_PROTOCOL = "aap-identity-directory-native/v1"
DIRECTORY_POLICY_PROTOCOL = "aap-identity-directory-policy/v1"
DIRECTORY_DERIVATION = "aap-identity-directory/v1"

DirectoryOperation: TypeAlias = Literal[
    "list_users", "query_users", "user_stats", "export_users"
]
DirectoryFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "invalid_request",
        "manager_required",
        "invalid_platform_role",
        "current_scope_busy",
        "operation_conflict",
        "unavailable",
    ]
)

_INT64_MAX = 2**63 - 1
_HEX_64 = r"^[0-9a-f]{64}$"
_PLATFORM_ROLES = {"super_admin", "project_admin", "employee", "viewer"}
_AUTH_FAILURES = {
    "credential_invalid",
    "credential_revoked",
    "generation_outdated",
    "token_expired",
    "user_inactive",
    "invalid_account_role",
    "credential_busy",
}
_MANAGER_REQUIRED = "需要角色权限: super_admin或project_admin"

Int64Count = Annotated[int, Field(ge=0, le=_INT64_MAX, strict=True)]
PositiveInt64 = Annotated[int, Field(ge=1, le=_INT64_MAX, strict=True)]
UserStatusFilter = Literal["active", "inactive", "all"]


class ListUsersPayload(ClosedModel):
    role: str | None = Field(default=None, strict=True)
    status: UserStatusFilter = "active"
    project_id: uuid.UUID | None = None


class _FilteredUsersPayload(ClosedModel):
    status: UserStatusFilter = "active"
    project_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    role: str | None = Field(default=None, strict=True)
    search: str | None = Field(default=None, max_length=255, strict=True)


class QueryUsersPayload(_FilteredUsersPayload):
    page: PositiveInt64 = 1
    page_size: int = Field(default=50, ge=1, le=500, strict=True)


class UserStatsPayload(_FilteredUsersPayload):
    pass


class ExportUsersPayload(_FilteredUsersPayload):
    format: Literal["csv", "json"] = "csv"


class _WireRequestBase(ClosedModel):
    protocol: Literal["aap-identity-directory/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class ListUsersRequest(_WireRequestBase):
    operation: Literal["list_users"]
    payload: ListUsersPayload


class QueryUsersRequest(_WireRequestBase):
    operation: Literal["query_users"]
    payload: QueryUsersPayload


class UserStatsRequest(_WireRequestBase):
    operation: Literal["user_stats"]
    payload: UserStatsPayload


class ExportUsersRequest(_WireRequestBase):
    operation: Literal["export_users"]
    payload: ExportUsersPayload


DirectoryRequest: TypeAlias = Annotated[
    ListUsersRequest | QueryUsersRequest | UserStatsRequest | ExportUsersRequest,
    Field(discriminator="operation"),
]
_DIRECTORY_REQUESTS = TypeAdapter(DirectoryRequest)


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
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
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
    if isinstance(value, datetime):
        return value.isoformat()
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


def decode_directory_request(payload: dict | bytes) -> DirectoryRequest:
    """Decode a closed private command, rejecting duplicate fields and aliases."""
    return _DIRECTORY_REQUESTS.validate_json(_json_bytes(payload))


def normalized_directory_request(request: DirectoryRequest) -> DirectoryRequest:
    """Normalize search exactly as the existing Python query builder does."""
    if request.operation in {"query_users", "user_stats", "export_users"}:
        search = request.payload.search
        normalized = search.strip() or None if search is not None else None
        payload = request.payload.model_copy(update={"search": normalized})
        return request.model_copy(update={"payload": payload})
    return request


class DirectoryUserRecord(ClosedModel):
    """The exact public UserOut projection, with historical role/status strings."""

    id: uuid.UUID
    email: str = Field(strict=True)
    name: str = Field(strict=True)
    role: str = Field(strict=True)
    group_name: str | None = Field(strict=True)
    group_id: uuid.UUID | None
    status: str = Field(strict=True)
    is_active: bool
    disabled_kind: str | None = Field(strict=True)
    disabled_at: datetime | None
    disabled_by: uuid.UUID | None
    disabled_reason: str | None = Field(strict=True)
    email_verified_at: datetime | None
    last_login_at: datetime | None
    last_seen_at: datetime | None
    password_admin_reset_at: datetime | None
    deactivation_requested_at: datetime | None
    deactivation_scheduled_at: datetime | None
    preferences: dict[str, Any]
    avatar_ref: str | None = Field(strict=True)
    created_at: datetime

    @field_validator("is_active", mode="before")
    @classmethod
    def strict_is_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Directory is_active must be a boolean")
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
                raise ValueError("Directory timestamps must include a timezone")
            return value.astimezone(UTC)
        return None


class DirectoryPageItem(DirectoryUserRecord):
    is_managed: bool
    is_lifecycle_managed: bool

    @field_validator("is_managed", "is_lifecycle_managed", mode="before")
    @classmethod
    def strict_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("Directory flags must be booleans")
        return value


class DirectoryExportRecord(ClosedModel):
    id: uuid.UUID
    email: str = Field(strict=True)
    name: str = Field(strict=True)
    role: str = Field(strict=True)
    group_name: str | None = Field(strict=True)
    group_id: uuid.UUID | None
    status: str = Field(strict=True)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Directory timestamp must include a timezone")
        return value.astimezone(UTC)


class DirectoryListData(ClosedModel):
    users: list[DirectoryUserRecord]


class DirectoryQueryData(ClosedModel):
    items: list[DirectoryPageItem]
    total: Int64Count
    page: PositiveInt64
    page_size: int = Field(ge=1, le=500, strict=True)
    pages: Int64Count

    @field_validator("total", "page", "page_size", "pages", mode="before")
    @classmethod
    def strict_pagination_numbers(cls, value):
        if type(value) is not int:
            raise ValueError("Directory pagination values must be integers")
        return value


class DirectoryStatsData(ClosedModel):
    total: Int64Count
    online: Int64Count
    weekly_active: Int64Count

    @field_validator("total", "online", "weekly_active", mode="before")
    @classmethod
    def strict_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Directory counts must be integers")
        return value


class DirectoryExportData(ClosedModel):
    users: list[DirectoryExportRecord]
    exported_by: str = Field(strict=True)
    exported_at: datetime
    request_id: str | None = Field(strict=True)

    @field_validator("exported_at")
    @classmethod
    def aware_exported_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Export timestamp must include a timezone")
        return value.astimezone(UTC)


DirectoryData: TypeAlias = (
    DirectoryListData | DirectoryQueryData | DirectoryStatsData | DirectoryExportData
)


class _WireReplyBase(ClosedModel):
    protocol: Literal["aap-identity-directory/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class ListUsersReply(_WireReplyBase):
    operation: Literal["list_users"]
    data: DirectoryListData


class QueryUsersReply(_WireReplyBase):
    operation: Literal["query_users"]
    data: DirectoryQueryData


class UserStatsReply(_WireReplyBase):
    operation: Literal["user_stats"]
    data: DirectoryStatsData


class ExportUsersReply(_WireReplyBase):
    operation: Literal["export_users"]
    data: DirectoryExportData


DirectorySuccess: TypeAlias = Annotated[
    ListUsersReply | QueryUsersReply | UserStatsReply | ExportUsersReply,
    Field(discriminator="operation"),
]
DirectoryReply: TypeAlias = DirectorySuccess
_DIRECTORY_SUCCESSES = TypeAdapter(DirectorySuccess)


class DirectoryFailureDetail(ClosedModel):
    code: DirectoryFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: str | None = Field(strict=True)

    @model_validator(mode="after")
    def fields_match_code(self) -> DirectoryFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Directory failure requires status_code and detail")
        if self.code in _AUTH_FAILURES:
            valid = self.status_code is None and self.detail is None
        elif self.code == "invalid_request":
            valid = self.status_code == 400 and self.detail is None
        elif self.code == "manager_required":
            valid = self.status_code == 403 and self.detail == _MANAGER_REQUIRED
        elif self.code == "invalid_platform_role":
            valid = self.status_code == 400 and isinstance(self.detail, str)
        elif self.code in {"current_scope_busy", "operation_conflict"}:
            valid = self.status_code == 409 and self.detail is None
        elif self.code == "unavailable":
            valid = self.status_code == 503 and self.detail is None
        else:
            valid = False
        if not valid:
            raise ValueError("Directory failure fields do not match its code")
        return self


class DirectoryFailureReply(ClosedModel):
    protocol: Literal["aap-identity-directory/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DirectoryFailureDetail


class DirectoryFailure(Exception):
    def __init__(
        self,
        code: DirectoryFailureCode,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        parsed = DirectoryFailureDetail(
            code=code, status_code=status_code, detail=detail
        )
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = parsed.detail
        super().__init__(code)


def directory_failure_reply(
    failure: DirectoryFailure | DirectoryFailureCode,
) -> dict:
    if isinstance(failure, DirectoryFailure):
        code, status_code, detail = (
            failure.code,
            failure.status_code,
            failure.detail,
        )
    else:
        code = failure
        fixed = {
            "invalid_request": (400, None),
            "manager_required": (403, _MANAGER_REQUIRED),
            "current_scope_busy": (409, None),
            "operation_conflict": (409, None),
            "unavailable": (503, None),
        }
        if code in _AUTH_FAILURES:
            status_code, detail = None, None
        elif code in fixed:
            status_code, detail = fixed[code]
        else:
            raise ValueError(
                "Status and detail are required for this directory failure"
            )
    return DirectoryFailureReply(
        schema=DIRECTORY_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    ).model_dump(mode="json", by_alias=True)


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-directory-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        default=None, min_length=1, max_length=255, strict=True, repr=False
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


class NativeListUsersCommand(_NativeCommandBase):
    operation: Literal["list_users"]
    payload: ListUsersPayload


class NativeQueryUsersCommand(_NativeCommandBase):
    operation: Literal["query_users"]
    payload: QueryUsersPayload


class NativeUserStatsCommand(_NativeCommandBase):
    operation: Literal["user_stats"]
    payload: UserStatsPayload


class NativeExportUsersCommand(_NativeCommandBase):
    operation: Literal["export_users"]
    payload: ExportUsersPayload


NativeDirectoryCommand: TypeAlias = Annotated[
    NativeListUsersCommand
    | NativeQueryUsersCommand
    | NativeUserStatsCommand
    | NativeExportUsersCommand,
    Field(discriminator="operation"),
]
_NATIVE_DIRECTORY_COMMANDS = TypeAdapter(NativeDirectoryCommand)


class _NativeSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-directory-native/v1"] = Field(alias="schema")
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


class NativeListUsersReply(_NativeSuccessBase):
    operation: Literal["list_users"]
    data: DirectoryListData


class NativeQueryUsersReply(_NativeSuccessBase):
    operation: Literal["query_users"]
    data: DirectoryQueryData


class NativeUserStatsReply(_NativeSuccessBase):
    operation: Literal["user_stats"]
    data: DirectoryStatsData


class NativeExportUsersReply(_NativeSuccessBase):
    operation: Literal["export_users"]
    data: DirectoryExportData


NativeDirectorySuccess: TypeAlias = Annotated[
    NativeListUsersReply
    | NativeQueryUsersReply
    | NativeUserStatsReply
    | NativeExportUsersReply,
    Field(discriminator="operation"),
]
_NATIVE_DIRECTORY_SUCCESSES = TypeAdapter(NativeDirectorySuccess)


class NativeDirectoryFailureReply(ClosedModel):
    protocol: Literal["aap-identity-directory-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: DirectoryFailureDetail


NativeDirectoryReply: TypeAlias = NativeDirectorySuccess | NativeDirectoryFailureReply


class NativeDirectoryPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-directory-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-directory/v1"]


DirectoryPolicyReply = NativeDirectoryPolicyReply


def decode_native_directory_command(
    payload: dict | bytes | ClosedModel,
) -> NativeDirectoryCommand:
    try:
        return _NATIVE_DIRECTORY_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def native_directory_command(
    request: DirectoryRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativeDirectoryCommand:
    normalized = normalized_directory_request(request)
    values = normalized.model_dump(mode="json", by_alias=True)
    values.update(
        {
            "schema": NATIVE_DIRECTORY_PROTOCOL,
            "actor": actor.model_dump(mode="json"),
            "expected_key_hash": expected_key_hash,
            "request_fingerprint": request_fingerprint,
            "derivation_id": derivation_id,
        }
    )
    values.pop("token", None)
    try:
        return _NATIVE_DIRECTORY_COMMANDS.validate_json(
            json.dumps(
                values, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
        )
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def _role_is_valid(role: str | None) -> bool:
    return role is None or role in _PLATFORM_ROLES


def _expected_failure(
    command: _WireRequestBase | _NativeCommandBase,
    failure: DirectoryFailureDetail,
) -> bool:
    if failure.code == "manager_required":
        return failure.status_code == 403 and failure.detail == _MANAGER_REQUIRED
    if failure.code == "invalid_platform_role":
        role = getattr(command.payload, "role", None)
        return (
            role is not None
            and not _role_is_valid(role)
            and failure.status_code == 400
            and failure.detail == f"非法平台角色: {role}"
        )
    if failure.code in _AUTH_FAILURES:
        return failure.status_code is None and failure.detail is None
    if failure.code == "invalid_request":
        return failure.status_code == 400 and failure.detail is None
    if failure.code == "current_scope_busy":
        return failure.status_code == 409 and failure.detail is None
    if failure.code == "operation_conflict":
        return (
            command.operation == "export_users"
            and failure.status_code == 409
            and failure.detail is None
        )
    if failure.code == "unavailable":
        return failure.status_code == 503 and failure.detail is None
    return False


def _validated_failure(
    encoded: bytes,
    *,
    command: _WireRequestBase | _NativeCommandBase,
    native: bool,
) -> DirectoryFailure:
    model = NativeDirectoryFailureReply if native else DirectoryFailureReply
    try:
        reply = model.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not _expected_failure(command, reply.error):
        raise IdentityUnavailable()
    if isinstance(command, _WireRequestBase) and reply.error.detail is not None:
        if command.token in reply.error.detail:
            raise IdentityUnavailable()
    return DirectoryFailure(
        reply.error.code, reply.error.status_code, reply.error.detail
    )


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


def _validate_filters(
    payload: _FilteredUsersPayload | ListUsersPayload,
    users: list[DirectoryUserRecord] | list[DirectoryExportRecord],
) -> None:
    if payload.role is not None and not _role_is_valid(payload.role):
        raise IdentityUnavailable()
    if payload.role is not None and any(user.role != payload.role for user in users):
        raise IdentityUnavailable()
    if users and isinstance(users[0], DirectoryUserRecord):
        if payload.status == "active" and any(not user.is_active for user in users):
            raise IdentityUnavailable()
        if payload.status == "inactive" and any(user.is_active for user in users):
            raise IdentityUnavailable()
    if isinstance(payload, _FilteredUsersPayload) and payload.group_id is not None:
        if any(user.group_id != payload.group_id for user in users):
            raise IdentityUnavailable()


def _validate_created_order(
    users: list[DirectoryUserRecord | DirectoryExportRecord], *, id_ties: bool
) -> None:
    for previous, current in zip(users, users[1:]):
        if previous.created_at < current.created_at:
            raise IdentityUnavailable()
        if id_ties and previous.created_at == current.created_at:
            if previous.id.int < current.id.int:
                raise IdentityUnavailable()


def _validate_query_flags(principal: Principal, data: DirectoryQueryData) -> None:
    for item in data.items:
        if principal.role == "super_admin":
            if not item.is_managed or not item.is_lifecycle_managed:
                raise IdentityUnavailable()
        else:
            if item.id == principal.actor_id and not item.is_managed:
                raise IdentityUnavailable()
            if item.is_active and item.role == "employee" and not item.is_managed:
                raise IdentityUnavailable()
            if item.is_lifecycle_managed and not item.is_managed:
                raise IdentityUnavailable()


def _validate_data_binding(
    command: _WireRequestBase | _NativeCommandBase,
    principal: Principal,
    data: DirectoryData,
    *,
    replayed: bool | None,
) -> None:
    operation = command.operation
    payload = command.payload
    if operation == "list_users":
        _validate_filters(payload, data.users)
        _validate_created_order(data.users, id_ties=False)
    elif operation == "query_users":
        _validate_filters(payload, data.items)
        expected_pages = (data.total + payload.page_size - 1) // payload.page_size
        offset = (payload.page - 1) * payload.page_size
        expected_item_count = (
            0 if offset >= data.total else min(payload.page_size, data.total - offset)
        )
        if (
            data.page != payload.page
            or data.page_size != payload.page_size
            or data.pages != expected_pages
            or len(data.items) != expected_item_count
            or (data.page > expected_pages and bool(data.items))
        ):
            raise IdentityUnavailable()
        _validate_created_order(data.items, id_ties=True)
        _validate_query_flags(principal, data)
    elif operation == "user_stats":
        if data.online > data.total or data.weekly_active > data.total:
            raise IdentityUnavailable()
    elif operation == "export_users":
        if replayed is False:
            _validate_filters(payload, data.users)
        _validate_created_order(data.users, id_ties=True)
        if replayed is False and (
            data.exported_by != principal.email
            or data.request_id != command.audit_context.request_id
        ):
            raise IdentityUnavailable()


def _decode_success(
    command: _WireRequestBase | _NativeCommandBase,
    payload: dict | bytes | ClosedModel,
    *,
    native: bool,
    expected_principal: Principal | None,
    wire_replayed: bool | None,
):
    encoded, raw = _strict_reply_payload(payload)
    if raw["ok"] is False:
        return _validated_failure(
            encoded,
            command=command,
            native=native,
        )
    adapter = _NATIVE_DIRECTORY_SUCCESSES if native else _DIRECTORY_SUCCESSES
    try:
        reply = adapter.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if (
        reply.operation != command.operation
        or reply.operation_id != command.operation_id
    ):
        raise IdentityUnavailable()
    if expected_principal is not None and not _same_fenced_credential(
        reply.principal, expected_principal
    ):
        raise IdentityUnavailable()
    if reply.principal.role not in {"super_admin", "project_admin"}:
        raise IdentityUnavailable()
    replayed = reply.replayed if native else wire_replayed
    if command.operation != "export_users" and replayed is True:
        raise IdentityUnavailable()
    _validate_data_binding(command, reply.principal, reply.data, replayed=replayed)
    return reply


def decode_native_directory_reply(
    command: NativeDirectoryCommand,
    payload: dict | bytes | ClosedModel,
) -> NativeDirectorySuccess:
    """Validate owner data before its transaction commits."""
    reply = _decode_success(
        command,
        payload,
        native=True,
        expected_principal=command.actor,
        wire_replayed=None,
    )
    if isinstance(reply, DirectoryFailure):
        raise reply
    return reply


def decode_directory_reply(
    request: DirectoryRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal | None = None,
    replayed: bool | None = None,
) -> DirectoryReply:
    reply = _decode_success(
        request,
        payload,
        native=False,
        expected_principal=expected_principal,
        wire_replayed=replayed,
    )
    if isinstance(reply, DirectoryFailure):
        raise reply
    return reply


def decode_native_directory_policy(
    payload: dict | bytes | ClosedModel,
) -> NativeDirectoryPolicyReply:
    try:
        return NativeDirectoryPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_directory_policy(
    payload: dict | bytes | ClosedModel,
) -> NativeDirectoryPolicyReply:
    return decode_native_directory_policy(payload)
