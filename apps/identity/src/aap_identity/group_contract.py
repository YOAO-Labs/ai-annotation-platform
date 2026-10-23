"""Closed wire and native contracts for Identity group commands."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
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

GROUP_PROTOCOL = "aap-identity-groups/v1"
NATIVE_GROUP_PROTOCOL = "aap-identity-groups-native/v1"
GROUP_DERIVATION = "aap-identity-groups/v1"
GROUP_POLICY_PROTOCOL = "aap-identity-groups-policy/v1"

GroupOperation: TypeAlias = Literal[
    "list_groups",
    "query_groups",
    "create_group",
    "update_group",
    "delete_group",
    "preview_group_assignments",
    "apply_group_assignments",
    "assign_user_group",
]
GroupFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "group_not_found",
        "target_not_found",
        "group_busy",
        "account_busy",
        "operation_conflict",
        "policy_rejected",
    ]
)

_HEX_64 = r"^[0-9a-f]{64}$"
_OUT_OF_SCOPE = "用户不存在或不在管理范围内"
_GROUP_NOT_FOUND = "数据组不存在"
_GENERIC_ITEM_FAILURE = "处理失败，请稍后重试"
_SAFE_APPLY_ERRORS = {
    _OUT_OF_SCOPE,
    "account_busy",
    "group_busy",
    _GENERIC_ITEM_FAILURE,
}


class EmptyPayload(ClosedModel):
    pass


class QueryGroupsPayload(ClosedModel):
    page: int = Field(ge=1, strict=True)
    page_size: int = Field(ge=1, le=500, strict=True)
    search: str | None = Field(strict=True)


class CreateGroupPayload(ClosedModel):
    name: str = Field(min_length=1, max_length=100, strict=True)
    description: str | None = Field(max_length=500, strict=True)


class UpdateGroupPayload(ClosedModel):
    group_id: uuid.UUID
    name: str | None = Field(min_length=1, max_length=100, strict=True)
    description: str | None = Field(max_length=500, strict=True)


class GroupTargetPayload(ClosedModel):
    group_id: uuid.UUID


class BulkGroupAssignmentsPayload(ClosedModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    group_id: uuid.UUID | None


class AssignUserGroupPayload(ClosedModel):
    user_id: uuid.UUID
    group_id: uuid.UUID | None


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-groups/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class ListGroupsRequest(_WireRequest):
    operation: Literal["list_groups"]
    payload: EmptyPayload


class QueryGroupsRequest(_WireRequest):
    operation: Literal["query_groups"]
    payload: QueryGroupsPayload


class CreateGroupRequest(_WireRequest):
    operation: Literal["create_group"]
    payload: CreateGroupPayload


class UpdateGroupRequest(_WireRequest):
    operation: Literal["update_group"]
    payload: UpdateGroupPayload


class DeleteGroupRequest(_WireRequest):
    operation: Literal["delete_group"]
    payload: GroupTargetPayload


class PreviewGroupAssignmentsRequest(_WireRequest):
    operation: Literal["preview_group_assignments"]
    payload: BulkGroupAssignmentsPayload


class ApplyGroupAssignmentsRequest(_WireRequest):
    operation: Literal["apply_group_assignments"]
    payload: BulkGroupAssignmentsPayload


class AssignUserGroupRequest(_WireRequest):
    operation: Literal["assign_user_group"]
    payload: AssignUserGroupPayload


GroupRequest: TypeAlias = Annotated[
    ListGroupsRequest
    | QueryGroupsRequest
    | CreateGroupRequest
    | UpdateGroupRequest
    | DeleteGroupRequest
    | PreviewGroupAssignmentsRequest
    | ApplyGroupAssignmentsRequest
    | AssignUserGroupRequest,
    Field(discriminator="operation"),
]
_GROUP_REQUESTS = TypeAdapter(GroupRequest)


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


def decode_group_request(payload: dict | bytes) -> GroupRequest:
    try:
        return _GROUP_REQUESTS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise


class GroupRecord(ClosedModel):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=100, strict=True)
    description: str | None = Field(max_length=500, strict=True)
    member_count: int = Field(ge=0, strict=True)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Group timestamp must include a timezone")
        return value.astimezone(UTC)


class ListGroupsData(ClosedModel):
    items: list[GroupRecord]


class QueryGroupsData(ClosedModel):
    items: list[GroupRecord]
    total: int = Field(ge=0, strict=True)
    page: int = Field(ge=1, strict=True)
    page_size: int = Field(ge=1, le=500, strict=True)
    pages: int = Field(ge=0, strict=True)

    @field_validator("pages", mode="before")
    @classmethod
    def strict_pages(cls, value):
        if type(value) is not int:
            raise ValueError("pages must be an integer")
        return value


class GroupData(ClosedModel):
    group: GroupRecord


class DeletedGroupData(ClosedModel):
    group_id: uuid.UUID
    deleted: Literal[True]

    @field_validator("deleted", mode="before")
    @classmethod
    def strict_deleted(cls, value):
        if type(value) is not bool:
            raise ValueError("deleted must be a boolean")
        return value


class GroupAssignmentPreviewItem(ClosedModel):
    user_id: uuid.UUID
    email: str | None = Field(strict=True)
    name: str | None = Field(strict=True)
    ok: bool
    current_group_id: uuid.UUID | None
    current_group_name: str | None = Field(strict=True)
    next_group_id: uuid.UUID | None
    next_group_name: str | None = Field(strict=True)
    error: str | None = Field(strict=True)

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool:
            raise ValueError("ok must be a boolean")
        return value


class GroupAssignmentsPreviewData(ClosedModel):
    group_id: uuid.UUID | None
    group_name: str | None = Field(strict=True)
    items: list[GroupAssignmentPreviewItem]
    applicable: int = Field(ge=0, strict=True)
    blocked: int = Field(ge=0, strict=True)


class GroupAssignmentResultItem(ClosedModel):
    user_id: uuid.UUID
    ok: bool
    retryable: bool
    error: str | None = Field(strict=True)

    @field_validator("ok", "retryable", mode="before")
    @classmethod
    def strict_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("Assignment flags must be booleans")
        return value


class GroupAssignmentsResultData(ClosedModel):
    items: list[GroupAssignmentResultItem]
    succeeded: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)


class AssignedAccountData(ClosedModel):
    account: AccountRecord


class _ReplyBase(ClosedModel):
    protocol: Literal["aap-identity-groups/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class ListGroupsReply(_ReplyBase):
    operation: Literal["list_groups"]
    data: ListGroupsData


class QueryGroupsReply(_ReplyBase):
    operation: Literal["query_groups"]
    data: QueryGroupsData


class CreateGroupReply(_ReplyBase):
    operation: Literal["create_group"]
    data: GroupData


class UpdateGroupReply(_ReplyBase):
    operation: Literal["update_group"]
    data: GroupData


class DeleteGroupReply(_ReplyBase):
    operation: Literal["delete_group"]
    data: DeletedGroupData


class PreviewGroupAssignmentsReply(_ReplyBase):
    operation: Literal["preview_group_assignments"]
    data: GroupAssignmentsPreviewData


class ApplyGroupAssignmentsReply(_ReplyBase):
    operation: Literal["apply_group_assignments"]
    data: GroupAssignmentsResultData


class AssignUserGroupReply(_ReplyBase):
    operation: Literal["assign_user_group"]
    data: AssignedAccountData


GroupReply: TypeAlias = Annotated[
    ListGroupsReply
    | QueryGroupsReply
    | CreateGroupReply
    | UpdateGroupReply
    | DeleteGroupReply
    | PreviewGroupAssignmentsReply
    | ApplyGroupAssignmentsReply
    | AssignUserGroupReply,
    Field(discriminator="operation"),
]
_GROUP_REPLIES = TypeAdapter(GroupReply)


class GroupFailureDetail(ClosedModel):
    code: GroupFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: str | None = Field(strict=True)

    @model_validator(mode="after")
    def fields_match_code(self) -> GroupFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Group failure requires status_code and detail")
        if self.code == "policy_rejected":
            if (self.status_code, self.detail) not in {
                (400, "名称不能为空"),
                (403, "当前账号已无管理权限"),
            } and not (
                self.status_code == 409
                and isinstance(self.detail, str)
                and self.detail.startswith("已存在同名数据组: ")
                and len(self.detail) > len("已存在同名数据组: ")
            ):
                raise ValueError("Invalid group policy rejection")
        elif self.status_code is not None or self.detail is not None:
            raise ValueError("Only group policy failures carry status and detail")
        return self


class GroupFailureReply(ClosedModel):
    protocol: Literal["aap-identity-groups/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: GroupFailureDetail


class GroupFailure(Exception):
    def __init__(
        self,
        code: GroupFailureCode,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        parsed = GroupFailureDetail(code=code, status_code=status_code, detail=detail)
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = parsed.detail
        super().__init__(code)


def group_failure_reply(
    failure: GroupFailure | GroupFailureCode,
) -> dict:
    if isinstance(failure, GroupFailure):
        code, status_code, detail = (
            failure.code,
            failure.status_code,
            failure.detail,
        )
    else:
        code, status_code, detail = failure, None, None
    return GroupFailureReply(
        schema=GROUP_PROTOCOL,
        ok=False,
        error={"code": code, "status_code": status_code, "detail": detail},
    ).model_dump(mode="json", by_alias=True)


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-groups-native/v1"] = Field(alias="schema")
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


class NativeListGroupsCommand(_NativeCommandBase):
    operation: Literal["list_groups"]
    payload: EmptyPayload


class NativeQueryGroupsCommand(_NativeCommandBase):
    operation: Literal["query_groups"]
    payload: QueryGroupsPayload


class NativeCreateGroupCommand(_NativeCommandBase):
    operation: Literal["create_group"]
    payload: CreateGroupPayload


class NativeUpdateGroupCommand(_NativeCommandBase):
    operation: Literal["update_group"]
    payload: UpdateGroupPayload


class NativeDeleteGroupCommand(_NativeCommandBase):
    operation: Literal["delete_group"]
    payload: GroupTargetPayload


class NativePreviewGroupAssignmentsCommand(_NativeCommandBase):
    operation: Literal["preview_group_assignments"]
    payload: BulkGroupAssignmentsPayload


class NativeApplyGroupAssignmentsCommand(_NativeCommandBase):
    operation: Literal["apply_group_assignments"]
    payload: BulkGroupAssignmentsPayload


class NativeAssignUserGroupCommand(_NativeCommandBase):
    operation: Literal["assign_user_group"]
    payload: AssignUserGroupPayload


NativeGroupCommand: TypeAlias = Annotated[
    NativeListGroupsCommand
    | NativeQueryGroupsCommand
    | NativeCreateGroupCommand
    | NativeUpdateGroupCommand
    | NativeDeleteGroupCommand
    | NativePreviewGroupAssignmentsCommand
    | NativeApplyGroupAssignmentsCommand
    | NativeAssignUserGroupCommand,
    Field(discriminator="operation"),
]
_NATIVE_GROUP_COMMANDS = TypeAdapter(NativeGroupCommand)


class _NativeSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-groups-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class NativeListGroupsReply(_NativeSuccessBase):
    operation: Literal["list_groups"]
    data: ListGroupsData


class NativeQueryGroupsReply(_NativeSuccessBase):
    operation: Literal["query_groups"]
    data: QueryGroupsData


class NativeCreateGroupReply(_NativeSuccessBase):
    operation: Literal["create_group"]
    data: GroupData


class NativeUpdateGroupReply(_NativeSuccessBase):
    operation: Literal["update_group"]
    data: GroupData


class NativeDeleteGroupReply(_NativeSuccessBase):
    operation: Literal["delete_group"]
    data: DeletedGroupData


class NativePreviewGroupAssignmentsReply(_NativeSuccessBase):
    operation: Literal["preview_group_assignments"]
    data: GroupAssignmentsPreviewData


class NativeApplyGroupAssignmentsReply(_NativeSuccessBase):
    operation: Literal["apply_group_assignments"]
    data: GroupAssignmentsResultData


class NativeAssignUserGroupReply(_NativeSuccessBase):
    operation: Literal["assign_user_group"]
    data: AssignedAccountData


NativeGroupSuccess: TypeAlias = Annotated[
    NativeListGroupsReply
    | NativeQueryGroupsReply
    | NativeCreateGroupReply
    | NativeUpdateGroupReply
    | NativeDeleteGroupReply
    | NativePreviewGroupAssignmentsReply
    | NativeApplyGroupAssignmentsReply
    | NativeAssignUserGroupReply,
    Field(discriminator="operation"),
]
_NATIVE_GROUP_SUCCESSES = TypeAdapter(NativeGroupSuccess)


class NativeGroupFailureReply(ClosedModel):
    protocol: Literal["aap-identity-groups-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: GroupFailureDetail


class GroupPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-groups-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-groups/v1"]


def decode_native_group_command(payload: dict | bytes) -> NativeGroupCommand:
    try:
        return _NATIVE_GROUP_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def native_group_command(
    request: GroupRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativeGroupCommand:
    values = request.model_dump(mode="json", by_alias=True)
    values.update(
        {
            "schema": NATIVE_GROUP_PROTOCOL,
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
        return _NATIVE_GROUP_COMMANDS.validate_json(encoded)
    except (ValidationError, ValueError, TypeError):
        raise IdentityUnavailable() from None


def _expected_policy_error(
    command: _WireRequest | _NativeCommandBase,
    error: GroupFailureDetail,
) -> bool:
    operation = command.operation
    payload = command.payload
    if error.code == "policy_rejected":
        if error.status_code == 403:
            return error.detail == "当前账号已无管理权限"
        if error.status_code == 400:
            return (
                operation in {"create_group", "update_group"}
                and getattr(payload, "name", None) is not None
                and not getattr(payload, "name").strip()
                and error.detail == "名称不能为空"
            )
        if error.status_code == 409:
            name = getattr(payload, "name", None)
            if name is None:
                return False
            expected = name.strip() if operation == "create_group" else name
            return error.detail == f"已存在同名数据组: {expected}"
        return False

    if error.code == "group_not_found":
        return (
            operation in {"update_group", "delete_group"}
            or (operation == "assign_user_group" and payload.group_id is not None)
            or (operation == "apply_group_assignments" and payload.group_id is not None)
        )
    if error.code == "target_not_found":
        return operation == "assign_user_group"
    if error.code == "group_busy":
        return operation in {
            "create_group",
            "update_group",
            "delete_group",
            "assign_user_group",
            "apply_group_assignments",
        }
    if error.code == "account_busy":
        return True
    if error.code == "operation_conflict":
        return operation in {
            "create_group",
            "update_group",
            "delete_group",
            "assign_user_group",
            "apply_group_assignments",
        }
    return True


def _validated_failure(
    encoded: bytes,
    *,
    command: _WireRequest | _NativeCommandBase,
    native: bool,
) -> GroupFailure:
    model = NativeGroupFailureReply if native else GroupFailureReply
    try:
        reply = model.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if not _expected_policy_error(command, reply.error):
        raise IdentityUnavailable()
    if isinstance(command, _WireRequest) and reply.error.detail is not None:
        if command.token in reply.error.detail:
            raise IdentityUnavailable()
    raise GroupFailure(reply.error.code, reply.error.status_code, reply.error.detail)


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


def _validate_data_binding(command, data) -> None:
    operation = command.operation
    payload = command.payload
    if operation == "query_groups":
        expected_pages = (data.total + payload.page_size - 1) // payload.page_size
        if (
            data.page != payload.page
            or data.page_size != payload.page_size
            or data.pages != expected_pages
            or len(data.items) > payload.page_size
        ):
            raise IdentityUnavailable()
    elif operation == "create_group":
        group = data.group
        if group.id != command.operation_id:
            raise IdentityUnavailable()
        if group.name != payload.name.strip() or group.description != (
            payload.description or None
        ):
            raise IdentityUnavailable()
    elif operation == "update_group":
        group = data.group
        if group.id != payload.group_id:
            raise IdentityUnavailable()
        if payload.name is not None and group.name != payload.name.strip():
            raise IdentityUnavailable()
        if payload.description is not None and group.description != (
            payload.description or None
        ):
            raise IdentityUnavailable()
    elif operation == "delete_group":
        if data.group_id != payload.group_id or data.deleted is not True:
            raise IdentityUnavailable()
    elif operation == "preview_group_assignments":
        if (
            data.group_id != payload.group_id
            or len(data.items) != len(payload.user_ids)
            or [item.user_id for item in data.items] != payload.user_ids
            or data.applicable != sum(item.ok for item in data.items)
            or data.blocked != len(data.items) - data.applicable
        ):
            raise IdentityUnavailable()
        expected_group_name = None
        for item in data.items:
            if item.ok:
                if item.email is None or item.name is None or item.error is not None:
                    raise IdentityUnavailable()
                if (
                    item.next_group_id != payload.group_id
                    or (payload.group_id is None and item.next_group_name is not None)
                    or (payload.group_id is not None and item.next_group_name is None)
                ):
                    raise IdentityUnavailable()
                if expected_group_name is None:
                    expected_group_name = item.next_group_name
            elif item.error == _OUT_OF_SCOPE:
                if any(
                    value is not None
                    for value in (
                        item.email,
                        item.name,
                        item.current_group_id,
                        item.current_group_name,
                        item.next_group_id,
                        item.next_group_name,
                    )
                ):
                    raise IdentityUnavailable()
            elif item.error == _GROUP_NOT_FOUND:
                if (
                    payload.group_id is None
                    or item.email is None
                    or item.name is None
                    or any(
                        value is not None
                        for value in (
                            item.current_group_id,
                            item.current_group_name,
                            item.next_group_id,
                            item.next_group_name,
                        )
                    )
                ):
                    raise IdentityUnavailable()
            else:
                raise IdentityUnavailable()
        if data.group_name != expected_group_name:
            raise IdentityUnavailable()
    elif operation == "apply_group_assignments":
        if (
            len(data.items) != len(payload.user_ids)
            or [item.user_id for item in data.items] != payload.user_ids
            or data.succeeded != sum(item.ok for item in data.items)
            or data.failed != len(data.items) - data.succeeded
        ):
            raise IdentityUnavailable()
        for item in data.items:
            if item.ok:
                if item.retryable or item.error is not None:
                    raise IdentityUnavailable()
            elif not item.retryable or item.error not in _SAFE_APPLY_ERRORS:
                raise IdentityUnavailable()
    elif operation == "assign_user_group":
        account = data.account
        if (
            account.id != payload.user_id
            or account.is_active is not True
            or account.group_id != payload.group_id
            or (payload.group_id is None and account.group_name is not None)
        ):
            raise IdentityUnavailable()


def _decode_success(
    command: _WireRequest | _NativeCommandBase,
    payload: dict | bytes | ClosedModel,
    *,
    native: bool,
    expected_principal: Principal | None,
):
    encoded, raw = _strict_reply_payload(payload)
    if raw["ok"] is False:
        return _validated_failure(encoded, command=command, native=native)
    adapter = _NATIVE_GROUP_SUCCESSES if native else _GROUP_REPLIES
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
    _validate_data_binding(command, reply.data)
    return reply


def decode_native_group_reply(
    command: NativeGroupCommand, payload: dict | bytes | ClosedModel
) -> NativeGroupSuccess:
    """Validate the owner reply while its transaction is still open."""
    reply = _decode_success(
        command, payload, native=True, expected_principal=command.actor
    )
    if isinstance(reply, GroupFailure):
        raise reply
    return reply


def decode_group_reply(
    request: GroupRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal | None = None,
) -> GroupReply:
    reply = _decode_success(
        request, payload, native=False, expected_principal=expected_principal
    )
    if isinstance(reply, GroupFailure):
        raise reply
    return reply


def decode_group_policy(payload: dict | bytes | ClosedModel) -> GroupPolicyReply:
    try:
        return GroupPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
