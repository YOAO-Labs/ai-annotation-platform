"""Closed wire, native and support-fact contracts for platform-role changes."""

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

PLATFORM_ROLE_PROTOCOL = "aap-identity-platform-roles/v1"
NATIVE_PLATFORM_ROLE_PROTOCOL = "aap-identity-platform-roles-native/v1"
PLATFORM_ROLE_POLICY_PROTOCOL = "aap-identity-platform-roles-policy/v1"
PLATFORM_ROLE_DERIVATION = "aap-identity-platform-roles/v1"
WORKSPACE_PLATFORM_ROLE_FACTS_PROTOCOL = "aap-workspace-identity-platform-role-facts/v1"
LABELING_PLATFORM_ROLE_FACTS_PROTOCOL = "aap-labeling-identity-platform-role-facts/v1"

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
_SUPER_ADMIN_REQUIRED = "当前账号已无管理权限"
_TARGET_NOT_FOUND = "用户不存在"
_SELF_CHANGE = "不能修改自己的角色"
_LAST_SUPER_ADMIN = "不能降级最后一名超级管理员"
_ACCOUNT_BUSY = "账号正在更新，请刷新后重试"
_INVALID_ROLE_PREFIX = "非法平台角色: "
_SELF_BLOCKER = "不能修改自己的角色"
_SAME_ROLE_BLOCKER = "目标角色与当前角色相同"
_LAST_SUPER_ADMIN_BLOCKER = "不能降级最后一名超级管理员"
_MEMBERSHIP_BLOCKER = "账号仍持有标注/质检项目职责，请先在项目中变更职责"
_OWNED_PROJECT_BLOCKER = "账号仍是项目负责人，请先转移项目后变更平台角色"
_UNFINISHED_WORK_BLOCKER = "账号仍有未完成的标注/质检工作，请先交接"
_ROLE_BLOCKERS = (
    _MEMBERSHIP_BLOCKER,
    _OWNED_PROJECT_BLOCKER,
    _UNFINISHED_WORK_BLOCKER,
)
_WARNINGS = (
    "平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。",
    "若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。",
)
_INT64_MAX = 2**63 - 1
Int64Count = Annotated[int, Field(ge=0, le=_INT64_MAX, strict=True)]

PlatformRoleOperation: TypeAlias = Literal[
    "preview_role_change", "change_platform_role"
]
PlatformRoleBlocker: TypeAlias = Literal[
    _SELF_BLOCKER,
    _SAME_ROLE_BLOCKER,
    _LAST_SUPER_ADMIN_BLOCKER,
    _MEMBERSHIP_BLOCKER,
    _OWNED_PROJECT_BLOCKER,
    _UNFINISHED_WORK_BLOCKER,
]
PlatformRoleFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "super_admin_required",
        "target_not_found",
        "invalid_platform_role",
        "self_change",
        "last_super_admin",
        "account_busy",
        "project_busy",
        "platform_role_change_blocked",
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
        raise ValueError("JSON values must be finite")
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


def _unique_project_ids(project_ids: list[uuid.UUID]) -> bool:
    return len(project_ids) == len(set(project_ids))


class WorkspacePlatformRoleMembershipProject(ClosedModel):
    project_id: uuid.UUID
    project_name: str = Field(strict=True)
    membership_role: str | None = Field(strict=True)


class WorkspacePlatformRoleFactsReply(ClosedModel):
    protocol: Literal["aap-workspace-identity-platform-role-facts/v1"] = Field(
        alias="schema"
    )
    target_user_id: uuid.UUID
    owned_project_count: Int64Count
    non_viewer_membership_count: Int64Count
    membership_projects: list[WorkspacePlatformRoleMembershipProject]

    @field_validator(
        "owned_project_count", "non_viewer_membership_count", mode="before"
    )
    @classmethod
    def strict_workspace_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Workspace role counts must be integers")
        return value

    @model_validator(mode="after")
    def unique_membership_projects(self) -> WorkspacePlatformRoleFactsReply:
        if not _unique_project_ids(
            [item.project_id for item in self.membership_projects]
        ):
            raise ValueError("Workspace role facts contain duplicate projects")
        return self


class LabelingPlatformRoleProjectCounts(ClosedModel):
    project_id: uuid.UUID
    annotator_batch_count: Int64Count
    reviewer_batch_count: Int64Count
    assigned_task_count: Int64Count
    review_task_count: Int64Count

    @field_validator(
        "annotator_batch_count",
        "reviewer_batch_count",
        "assigned_task_count",
        "review_task_count",
        mode="before",
    )
    @classmethod
    def strict_project_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Labeling role counts must be integers")
        return value


class LabelingPlatformRoleFactsReply(ClosedModel):
    protocol: Literal["aap-labeling-identity-platform-role-facts/v1"] = Field(
        alias="schema"
    )
    target_user_id: uuid.UUID
    unfinished_effective_work_count: Int64Count
    membership_projects: list[LabelingPlatformRoleProjectCounts]

    @field_validator("unfinished_effective_work_count", mode="before")
    @classmethod
    def strict_unfinished_work_count(cls, value):
        if type(value) is not int:
            raise ValueError("Labeling role counts must be integers")
        return value

    @model_validator(mode="after")
    def unique_membership_projects(self) -> LabelingPlatformRoleFactsReply:
        if not _unique_project_ids(
            [item.project_id for item in self.membership_projects]
        ):
            raise ValueError("Labeling role facts contain duplicate projects")
        return self


def decode_workspace_platform_role_facts(
    payload: dict | bytes | ClosedModel, *, target_user_id: uuid.UUID
) -> WorkspacePlatformRoleFactsReply:
    try:
        reply = WorkspacePlatformRoleFactsReply.model_validate_json(
            _json_bytes(payload)
        )
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if reply.target_user_id != target_user_id:
        raise IdentityUnavailable()
    return reply


def decode_labeling_platform_role_facts(
    payload: dict | bytes | ClosedModel, *, target_user_id: uuid.UUID
) -> LabelingPlatformRoleFactsReply:
    try:
        reply = LabelingPlatformRoleFactsReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if reply.target_user_id != target_user_id:
        raise IdentityUnavailable()
    return reply


def validate_platform_role_fact_coverage(
    workspace: WorkspacePlatformRoleFactsReply,
    labeling: LabelingPlatformRoleFactsReply,
) -> None:
    if workspace.target_user_id != labeling.target_user_id or [
        item.project_id for item in workspace.membership_projects
    ] != [item.project_id for item in labeling.membership_projects]:
        raise IdentityUnavailable()


class PlatformRolePayload(ClosedModel):
    target_user_id: uuid.UUID
    role: str = Field(strict=True)


class _WireRequestBase(ClosedModel):
    protocol: Literal["aap-identity-platform-roles/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext
    payload: PlatformRolePayload


class PreviewRoleChangeRequest(_WireRequestBase):
    operation: Literal["preview_role_change"]


class ChangePlatformRoleRequest(_WireRequestBase):
    operation: Literal["change_platform_role"]


PlatformRoleRequest: TypeAlias = Annotated[
    PreviewRoleChangeRequest | ChangePlatformRoleRequest,
    Field(discriminator="operation"),
]
_PLATFORM_ROLE_REQUESTS = TypeAdapter(PlatformRoleRequest)


def decode_platform_role_request(payload: dict | bytes) -> PlatformRoleRequest:
    return _PLATFORM_ROLE_REQUESTS.validate_json(_json_bytes(payload))


class PlatformRolePreviewProject(ClosedModel):
    project_id: uuid.UUID
    project_name: str = Field(strict=True)
    membership_role: str | None = Field(strict=True)
    annotator_batch_count: Int64Count
    reviewer_batch_count: Int64Count
    assigned_task_count: Int64Count
    review_task_count: Int64Count

    @field_validator(
        "annotator_batch_count",
        "reviewer_batch_count",
        "assigned_task_count",
        "review_task_count",
        mode="before",
    )
    @classmethod
    def strict_project_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Platform-role project counts must be integers")
        return value


class PlatformRolePreviewData(ClosedModel):
    user_id: uuid.UUID
    email: str = Field(strict=True)
    current_role: str = Field(strict=True)
    requested_role: str = Field(strict=True)
    can_change: bool
    blockers: list[PlatformRoleBlocker]
    projects: list[PlatformRolePreviewProject]
    assigned_batch_count: Int64Count
    assigned_task_count: Int64Count
    review_task_count: Int64Count
    other_project_count: Int64Count
    warnings: list[str]

    @field_validator("can_change", mode="before")
    @classmethod
    def strict_can_change(cls, value):
        if type(value) is not bool:
            raise ValueError("Platform-role can_change must be a boolean")
        return value

    @field_validator(
        "assigned_batch_count",
        "assigned_task_count",
        "review_task_count",
        "other_project_count",
        mode="before",
    )
    @classmethod
    def strict_totals(cls, value):
        if type(value) is not int:
            raise ValueError("Platform-role totals must be integers")
        return value


class PlatformRoleChangeData(ClosedModel):
    user: DirectoryUserRecord


PlatformRoleData: TypeAlias = PlatformRolePreviewData | PlatformRoleChangeData


class _WireSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-platform-roles/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class PreviewRoleChangeReply(_WireSuccessBase):
    operation: Literal["preview_role_change"]
    data: PlatformRolePreviewData


class ChangePlatformRoleReply(_WireSuccessBase):
    operation: Literal["change_platform_role"]
    data: PlatformRoleChangeData


PlatformRoleReply: TypeAlias = Annotated[
    PreviewRoleChangeReply | ChangePlatformRoleReply,
    Field(discriminator="operation"),
]
_PLATFORM_ROLE_REPLIES = TypeAdapter(PlatformRoleReply)


class ProjectBusyDetail(ClosedModel):
    reason: Literal["project_busy"]


class PlatformRoleBlockedDetail(ClosedModel):
    reason: Literal["platform_role_change_blocked"]
    blockers: list[PlatformRoleBlocker]

    @model_validator(mode="after")
    def nonempty_ordered_blockers(self) -> PlatformRoleBlockedDetail:
        if not self.blockers or len(self.blockers) != len(set(self.blockers)):
            raise ValueError("Role-change blocker detail must be nonempty and unique")
        if sorted(self.blockers, key=_ROLE_BLOCKERS.index) != self.blockers:
            raise ValueError("Role-change blocker detail must keep native order")
        return self


PlatformRoleFailureDetailValue: TypeAlias = (
    str | ProjectBusyDetail | PlatformRoleBlockedDetail | None
)


class PlatformRoleFailureDetail(ClosedModel):
    code: PlatformRoleFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: PlatformRoleFailureDetailValue

    @model_validator(mode="after")
    def fields_match_code(self) -> PlatformRoleFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Platform-role failure requires status and detail")
        detail = self.detail
        if self.code in _AUTH_FAILURES:
            valid = self.status_code is None and detail is None
        elif self.code == "invalid_request":
            valid = self.status_code == 400 and detail is None
        elif self.code == "super_admin_required":
            valid = self.status_code == 403 and detail == _SUPER_ADMIN_REQUIRED
        elif self.code == "target_not_found":
            valid = self.status_code == 404 and detail == _TARGET_NOT_FOUND
        elif self.code == "invalid_platform_role":
            valid = self.status_code == 400 and isinstance(detail, str)
        elif self.code == "self_change":
            valid = self.status_code == 400 and detail == _SELF_CHANGE
        elif self.code == "last_super_admin":
            valid = self.status_code == 400 and detail == _LAST_SUPER_ADMIN
        elif self.code == "account_busy":
            valid = self.status_code == 409 and detail == _ACCOUNT_BUSY
        elif self.code == "project_busy":
            valid = self.status_code == 409 and isinstance(detail, ProjectBusyDetail)
        elif self.code == "platform_role_change_blocked":
            valid = self.status_code == 409 and isinstance(
                detail, PlatformRoleBlockedDetail
            )
        elif self.code == "operation_conflict":
            valid = self.status_code == 409 and detail is None
        elif self.code == "unavailable":
            valid = self.status_code == 503 and detail is None
        else:
            valid = False
        if not valid:
            raise ValueError("Platform-role failure fields do not match its code")
        return self


class PlatformRoleFailureReply(ClosedModel):
    protocol: Literal["aap-identity-platform-roles/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: PlatformRoleFailureDetail


class PlatformRoleFailure(Exception):
    def __init__(
        self,
        code: PlatformRoleFailureCode,
        status_code: int | None = None,
        detail: str | dict[str, Any] | ClosedModel | None = None,
    ):
        if isinstance(detail, ClosedModel):
            detail = detail.model_dump(mode="json", by_alias=True)
        raw = json.dumps(
            {"code": code, "status_code": status_code, "detail": detail},
            ensure_ascii=True,
            allow_nan=False,
        )
        try:
            parsed = PlatformRoleFailureDetail.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid platform-role failure") from None
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = (
            parsed.detail.model_dump(mode="json", by_alias=True)
            if isinstance(parsed.detail, ClosedModel)
            else parsed.detail
        )
        super().__init__(code)


class NativePlatformRoleCommandBase(ClosedModel):
    protocol: Literal["aap-identity-platform-roles-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=_HEX_64, strict=True, repr=False)
    derivation_id: str = Field(pattern=_HEX_64, strict=True)
    audit_context: SessionAuditContext
    payload: PlatformRolePayload

    @model_validator(mode="after")
    def key_hash_matches_credential(self) -> NativePlatformRoleCommandBase:
        if (self.actor.credential_type == "api_key") != (
            self.expected_key_hash is not None
        ):
            raise ValueError("API-key hash must match the credential kind")
        return self


class NativePreviewRoleChangeCommand(NativePlatformRoleCommandBase):
    operation: Literal["preview_role_change"]


class NativeChangePlatformRoleCommand(NativePlatformRoleCommandBase):
    operation: Literal["change_platform_role"]


NativePlatformRoleCommand: TypeAlias = Annotated[
    NativePreviewRoleChangeCommand | NativeChangePlatformRoleCommand,
    Field(discriminator="operation"),
]
_NATIVE_PLATFORM_ROLE_COMMANDS = TypeAdapter(NativePlatformRoleCommand)


def native_platform_role_command(
    request: PlatformRoleRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativePlatformRoleCommand:
    values = request.model_dump(mode="json", by_alias=True)
    values.update(
        {
            "schema": NATIVE_PLATFORM_ROLE_PROTOCOL,
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
        return _NATIVE_PLATFORM_ROLE_COMMANDS.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_platform_role_command(
    payload: dict | bytes | ClosedModel,
) -> NativePlatformRoleCommand:
    try:
        return _NATIVE_PLATFORM_ROLE_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class _NativeSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-platform-roles-native/v1"] = Field(alias="schema")
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


class NativePreviewRoleChangeReply(_NativeSuccessBase):
    operation: Literal["preview_role_change"]
    data: PlatformRolePreviewData


class NativeChangePlatformRoleReply(_NativeSuccessBase):
    operation: Literal["change_platform_role"]
    data: PlatformRoleChangeData


NativePlatformRoleSuccess: TypeAlias = Annotated[
    NativePreviewRoleChangeReply | NativeChangePlatformRoleReply,
    Field(discriminator="operation"),
]
_NATIVE_PLATFORM_ROLE_SUCCESSES = TypeAdapter(NativePlatformRoleSuccess)


class NativePlatformRoleFailureReply(ClosedModel):
    protocol: Literal["aap-identity-platform-roles-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: PlatformRoleFailureDetail


NativePlatformRoleReply: TypeAlias = (
    NativePlatformRoleSuccess | NativePlatformRoleFailureReply
)


class NativePlatformRolePolicyReply(ClosedModel):
    protocol: Literal["aap-identity-platform-roles-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-platform-roles/v1"]


PlatformRolePolicyReply = NativePlatformRolePolicyReply


def _platform_role_is_valid(role: str) -> bool:
    return role in _PLATFORM_ROLES


def _role_work_blockers_are_valid(
    role: str, blockers: list[PlatformRoleBlocker]
) -> bool:
    allowed = []
    if role in {"super_admin", "project_admin", "viewer"}:
        allowed.append(_MEMBERSHIP_BLOCKER)
    if role in {"employee", "viewer"}:
        allowed.append(_OWNED_PROJECT_BLOCKER)
    if role == "viewer":
        allowed.append(_UNFINISHED_WORK_BLOCKER)
    return (
        bool(blockers)
        and len(blockers) == len(set(blockers))
        and all(item in allowed for item in blockers)
        and sorted(blockers, key=allowed.index) == blockers
    )


def _validate_platform_role_preview(
    data: PlatformRolePreviewData,
    request: Any,
    principal: Principal,
) -> None:
    if (
        request.operation != "preview_role_change"
        or not _platform_role_is_valid(request.payload.role)
        or data.user_id != request.payload.target_user_id
        or data.requested_role != request.payload.role
        or data.other_project_count != 0
        or data.warnings != list(_WARNINGS)
        or data.can_change is not (not data.blockers)
    ):
        raise IdentityUnavailable()

    ids = [item.project_id for item in data.projects]
    if not _unique_project_ids(ids):
        raise IdentityUnavailable()
    if data.assigned_batch_count != sum(
        item.annotator_batch_count + item.reviewer_batch_count for item in data.projects
    ):
        raise IdentityUnavailable()
    if data.assigned_task_count != sum(
        item.assigned_task_count for item in data.projects
    ):
        raise IdentityUnavailable()
    if data.review_task_count != sum(item.review_task_count for item in data.projects):
        raise IdentityUnavailable()

    expected_prefix: list[PlatformRoleBlocker] = []
    if data.user_id == principal.actor_id:
        expected_prefix.append(_SELF_BLOCKER)
    if data.current_role == request.payload.role:
        expected_prefix.append(_SAME_ROLE_BLOCKER)
    if data.blockers[: len(expected_prefix)] != expected_prefix:
        raise IdentityUnavailable()

    remaining = list(data.blockers[len(expected_prefix) :])
    if (
        data.current_role == "super_admin"
        and request.payload.role != "super_admin"
        and remaining
        and remaining[0] == _LAST_SUPER_ADMIN_BLOCKER
    ):
        remaining.pop(0)
    elif _LAST_SUPER_ADMIN_BLOCKER in remaining:
        raise IdentityUnavailable()
    if request.payload.role == data.current_role and remaining:
        raise IdentityUnavailable()
    if remaining and not _role_work_blockers_are_valid(request.payload.role, remaining):
        raise IdentityUnavailable()


def _validate_platform_role_change(
    data: PlatformRoleChangeData,
    request: Any,
    principal: Principal,
) -> None:
    if (
        request.operation != "change_platform_role"
        or not _platform_role_is_valid(request.payload.role)
        or data.user.id != request.payload.target_user_id
        or data.user.role != request.payload.role
        or data.user.is_active is not True
        or data.user.id == principal.actor_id
    ):
        raise IdentityUnavailable()


def _strict_reply_payload(
    payload: dict | bytes | ClosedModel,
) -> tuple[bytes, dict]:
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
    request: Any,
    failure: PlatformRoleFailureDetail,
    actor: Principal | None,
) -> bool:
    role = request.payload.role
    detail = failure.detail
    if failure.code in _AUTH_FAILURES:
        return failure.status_code is None and detail is None
    if failure.code == "invalid_request":
        return failure.status_code == 400 and detail is None
    if failure.code == "super_admin_required":
        return failure.status_code == 403 and detail == _SUPER_ADMIN_REQUIRED
    if failure.code == "target_not_found":
        return failure.status_code == 404 and detail == _TARGET_NOT_FOUND
    if failure.code == "invalid_platform_role":
        return (
            not _platform_role_is_valid(role)
            and failure.status_code == 400
            and detail == f"{_INVALID_ROLE_PREFIX}{role}"
        )
    if failure.code == "self_change":
        return (
            actor is not None
            and request.payload.target_user_id == actor.actor_id
            and failure.status_code == 400
            and detail == _SELF_CHANGE
        )
    if failure.code == "last_super_admin":
        return (
            request.operation == "change_platform_role"
            and _platform_role_is_valid(role)
            and role != "super_admin"
            and actor is not None
            and request.payload.target_user_id != actor.actor_id
            and failure.status_code == 400
            and detail == _LAST_SUPER_ADMIN
        )
    if failure.code == "account_busy":
        return failure.status_code == 409 and detail == _ACCOUNT_BUSY
    if failure.code == "project_busy":
        return (
            request.operation == "change_platform_role"
            and _platform_role_is_valid(role)
            and isinstance(detail, ProjectBusyDetail)
            and actor is not None
            and request.payload.target_user_id != actor.actor_id
        )
    if failure.code == "platform_role_change_blocked":
        return (
            request.operation == "change_platform_role"
            and _platform_role_is_valid(role)
            and isinstance(detail, PlatformRoleBlockedDetail)
            and actor is not None
            and request.payload.target_user_id != actor.actor_id
            and _role_work_blockers_are_valid(role, detail.blockers)
        )
    if failure.code == "operation_conflict":
        return (
            request.operation == "change_platform_role"
            and failure.status_code == 409
            and detail is None
        )
    if failure.code == "unavailable":
        return failure.status_code == 503 and detail is None
    return False


def _detail_json(value: PlatformRoleFailureDetailValue) -> object:
    return (
        value.model_dump(mode="json", by_alias=True)
        if isinstance(value, ClosedModel)
        else value
    )


def _failure_text(detail: PlatformRoleFailureDetailValue) -> str:
    value = _detail_json(detail)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def _validated_failure(
    encoded: bytes,
    *,
    command: Any,
    native: bool,
    expected_principal: Principal | None,
    token: str | None,
) -> PlatformRoleFailure:
    model = NativePlatformRoleFailureReply if native else PlatformRoleFailureReply
    try:
        reply = model.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    actor = command.actor if native else expected_principal
    if not _expected_failure(command, reply.error, actor):
        raise IdentityUnavailable()
    if token is not None and token in _failure_text(reply.error.detail):
        raise IdentityUnavailable()
    return PlatformRoleFailure(
        reply.error.code,
        reply.error.status_code,
        _detail_json(reply.error.detail),
    )


def decode_native_platform_role_reply(
    command: NativePlatformRoleCommand,
    payload: dict | bytes | ClosedModel,
) -> NativePlatformRoleSuccess:
    encoded, raw = _strict_reply_payload(payload)
    if raw["ok"] is False:
        raise _validated_failure(
            encoded,
            command=command,
            native=True,
            expected_principal=None,
            token=None,
        )
    try:
        reply = _NATIVE_PLATFORM_ROLE_SUCCESSES.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if (
        reply.operation != command.operation
        or reply.operation_id != command.operation_id
        or not _same_fenced_credential(reply.principal, command.actor)
        or reply.principal.role != "super_admin"
    ):
        raise IdentityUnavailable()
    if command.operation == "preview_role_change":
        preview = reply.data
        if reply.replayed:
            raise IdentityUnavailable()
        _validate_platform_role_preview(preview, command, reply.principal)
    else:
        _validate_platform_role_change(reply.data, command, reply.principal)
    return reply


def decode_platform_role_reply(
    request: PlatformRoleRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal,
) -> PlatformRoleReply:
    encoded, raw = _strict_reply_payload(payload)
    if raw["ok"] is False:
        raise _validated_failure(
            encoded,
            command=request,
            native=False,
            expected_principal=expected_principal,
            token=request.token,
        )
    try:
        reply = _PLATFORM_ROLE_REPLIES.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if (
        reply.operation != request.operation
        or reply.operation_id != request.operation_id
        or not _same_fenced_credential(reply.principal, expected_principal)
        or reply.principal.role != "super_admin"
    ):
        raise IdentityUnavailable()
    if request.operation == "preview_role_change":
        _validate_platform_role_preview(reply.data, request, reply.principal)
    else:
        _validate_platform_role_change(reply.data, request, reply.principal)
    return reply


def decode_native_platform_role_policy(
    payload: dict | bytes | ClosedModel,
) -> NativePlatformRolePolicyReply:
    try:
        return NativePlatformRolePolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_platform_role_policy(
    payload: dict | bytes | ClosedModel,
) -> NativePlatformRolePolicyReply:
    return decode_native_platform_role_policy(payload)


def platform_role_failure_reply(
    failure: PlatformRoleFailure | PlatformRoleFailureCode,
) -> dict:
    if isinstance(failure, PlatformRoleFailure):
        code, status_code, detail = (
            failure.code,
            failure.status_code,
            failure.detail,
        )
    else:
        code = failure
        if code in _AUTH_FAILURES:
            status_code, detail = None, None
        else:
            fixed = {
                "invalid_request": (400, None),
                "super_admin_required": (403, _SUPER_ADMIN_REQUIRED),
                "target_not_found": (404, _TARGET_NOT_FOUND),
                "self_change": (400, _SELF_CHANGE),
                "last_super_admin": (400, _LAST_SUPER_ADMIN),
                "account_busy": (409, _ACCOUNT_BUSY),
                "operation_conflict": (409, None),
                "unavailable": (503, None),
            }
            try:
                status_code, detail = fixed[code]
            except KeyError:
                raise ValueError("Failure requires request-bound detail") from None
    payload = {
        "schema": PLATFORM_ROLE_PROTOCOL,
        "ok": False,
        "error": {"code": code, "status_code": status_code, "detail": detail},
    }
    try:
        return PlatformRoleFailureReply.model_validate_json(
            json.dumps(payload, ensure_ascii=True, allow_nan=False)
        ).model_dump(mode="json", by_alias=True)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid platform-role failure reply") from None
