"""Closed wire/native contracts for offboarding preview and commit."""

from __future__ import annotations

import json
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

OFFBOARDING_PROTOCOL = "aap-identity-offboarding/v1"
NATIVE_OFFBOARDING_PROTOCOL = "aap-identity-offboarding-native/v1"
OFFBOARDING_POLICY_PROTOCOL = "aap-identity-offboarding-policy/v1"
OFFBOARDING_DERIVATION = "aap-identity-offboarding/v1"

OffboardingOperation: TypeAlias = Literal["preview_offboarding", "commit_offboarding"]
OffboardingMode: TypeAlias = Literal["handoff", "emergency_suspend"]
OffboardingRole: TypeAlias = Literal["owner", "annotator", "reviewer"]
LabelingOffboardingRole: TypeAlias = Literal["annotator", "reviewer"]

OffboardingFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "manager_required",
        "target_not_found",
        "self_offboard",
        "last_super_admin",
        "target_role_forbidden",
        "inactive_unmanaged",
        "scope_forbidden",
        "offboarding_preview_stale",
        "historical_target",
        "state_forbidden",
        "duplicate_project",
        "receiver_required",
        "receiver_ineligible",
        "offboarding_blocked",
        "account_busy",
        "offboarding_busy",
        "operation_conflict",
        "unavailable",
    ]
)

_INT64_MAX = 2**63 - 1
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
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
    "self_offboard": (400, "不能交接自己"),
    "last_super_admin": (400, "不能停用最后一名超级管理员"),
    "target_role_forbidden": (403, "项目管理员仅能处理标注员/审核员账号"),
    "inactive_unmanaged": (
        403,
        "该用户已停用且不在你管理的项目内，须由超级管理员处理",
    ),
    "scope_forbidden": (403, "该用户存在不在你管理范围内的项目"),
    "historical_target": (409, "历史未知或已删除账号不可再次交接"),
    "state_forbidden": (409, "账号当前状态不可交接"),
    "duplicate_project": (422, "同一项目不能重复提交交接映射"),
    "account_busy": (409, "账号正在更新，请刷新后重试"),
    "operation_conflict": (409, None),
    "unavailable": (503, None),
}


class OffboardingProjectRequest(ClosedModel):
    project_id: uuid.UUID
    owner_receiver_id: uuid.UUID | None
    annotator_receiver_id: uuid.UUID | None
    reviewer_receiver_id: uuid.UUID | None


class PreviewOffboardingPayload(ClosedModel):
    target_user_id: uuid.UUID


class CommitOffboardingPayload(ClosedModel):
    target_user_id: uuid.UUID
    # Publicly unbounded by design; this value can be echoed in a stale-CAS error.
    preview_version: str = Field(strict=True)
    reason: str = Field(max_length=500, strict=True)
    mode: OffboardingMode
    projects: list[OffboardingProjectRequest]


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-offboarding/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class PreviewOffboardingRequest(_WireRequest):
    operation: Literal["preview_offboarding"]
    payload: PreviewOffboardingPayload


class CommitOffboardingRequest(_WireRequest):
    operation: Literal["commit_offboarding"]
    payload: CommitOffboardingPayload


OffboardingRequest: TypeAlias = Annotated[
    PreviewOffboardingRequest | CommitOffboardingRequest,
    Field(discriminator="operation"),
]
_OFFBOARDING_REQUESTS = TypeAdapter(OffboardingRequest)


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


def decode_offboarding_request(payload: dict | bytes) -> OffboardingRequest:
    return _OFFBOARDING_REQUESTS.validate_json(_json_bytes(payload))


class OffboardingReceiverOption(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    email: str = Field(strict=True)
    role: str = Field(strict=True)
    project_member_role: str | None = Field(strict=True)


class OffboardingBlocker(ClosedModel):
    code: Literal[
        "historical_inactive",
        "no_owner_receiver",
        "no_annotator_receiver",
        "no_reviewer_receiver",
        "historic_mixed_role",
    ]
    message: str = Field(strict=True)

    @model_validator(mode="after")
    def message_matches_code(self) -> OffboardingBlocker:
        fixed = {
            "historical_inactive": "该账号的停用来源无法可靠判定，不能恢复或再次交接",
            "historic_mixed_role": "该账号在项目成员或批次历史中同时出现不同职责，需人工核对",
        }
        if self.code in fixed:
            if self.message != fixed[self.code]:
                raise ValueError("Offboarding blocker message does not match its code")
            return self
        role = self.code.removeprefix("no_").removesuffix("_receiver")
        suffix = f"没有符合当前 {role} 权限的启用接收人"
        if not self.message.startswith("项目「") or not self.message.endswith(suffix):
            raise ValueError(
                "Offboarding receiver blocker message is not legacy-shaped"
            )
        if len(self.message) <= len("项目「」") + len(suffix):
            raise ValueError("Offboarding receiver blocker has no project name")
        return self


class OffboardingBatchRef(ClosedModel):
    batch_id: uuid.UUID
    batch_name: str = Field(strict=True)


class OffboardingRolePreview(ClosedModel):
    present: bool
    batches: list[OffboardingBatchRef]
    receiver_options: list[OffboardingReceiverOption]

    @field_validator("present", mode="before")
    @classmethod
    def strict_present(cls, value):
        if type(value) is not bool:
            raise ValueError("Offboarding present must be a boolean")
        return value


class OffboardingProjectRoles(ClosedModel):
    owner: OffboardingRolePreview
    annotator: OffboardingRolePreview
    reviewer: OffboardingRolePreview


class OffboardingTaskCounts(ClosedModel):
    pending: int = Field(ge=0, le=_INT64_MAX, strict=True)
    in_progress: int = Field(ge=0, le=_INT64_MAX, strict=True)
    review: int = Field(ge=0, le=_INT64_MAX, strict=True)
    rejected: int = Field(ge=0, le=_INT64_MAX, strict=True)
    locked: int = Field(ge=0, le=_INT64_MAX, strict=True)

    @field_validator(
        "pending", "in_progress", "review", "rejected", "locked", mode="before"
    )
    @classmethod
    def strict_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Offboarding counts must be integers")
        return value


class OffboardingProjectTasks(ClosedModel):
    annotator: OffboardingTaskCounts
    reviewer: OffboardingTaskCounts


class OffboardingProjectPreview(ClosedModel):
    project_id: uuid.UUID
    project_name: str = Field(strict=True)
    roles: OffboardingProjectRoles
    tasks: OffboardingProjectTasks
    locked_task_count: int = Field(ge=0, le=_INT64_MAX, strict=True)
    blockers: list[OffboardingBlocker]

    @field_validator("locked_task_count", mode="before")
    @classmethod
    def strict_locked_count(cls, value):
        if type(value) is not int:
            raise ValueError("Offboarding lock counts must be integers")
        return value


class OffboardingApiKeyPreview(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    key_prefix: str = Field(strict=True)
    last_used_at: datetime | None
    revoked_at: datetime | None

    @field_validator("last_used_at", "revoked_at")
    @classmethod
    def aware_key_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Offboarding key timestamps must be timezone-aware")
            return value.astimezone(UTC)
        return None


class OffboardingPreviewRecord(ClosedModel):
    user: DirectoryUserRecord
    preview_version: str = Field(strict=True)
    generated_at: datetime
    projects: list[OffboardingProjectPreview]
    api_keys: list[OffboardingApiKeyPreview]
    blockers: list[OffboardingBlocker]
    can_commit: bool

    @field_validator("generated_at")
    @classmethod
    def aware_generated_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Offboarding generated_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("can_commit", mode="before")
    @classmethod
    def strict_can_commit(cls, value):
        if type(value) is not bool:
            raise ValueError("Offboarding can_commit must be a boolean")
        return value


class OffboardingTransfer(ClosedModel):
    project_id: uuid.UUID
    role: OffboardingRole
    receiver_id: uuid.UUID | None
    batch_ids: list[uuid.UUID]
    task_count: int = Field(ge=0, le=_INT64_MAX, strict=True)
    lock_count: int = Field(ge=0, le=_INT64_MAX, strict=True)

    @field_validator("task_count", "lock_count", mode="before")
    @classmethod
    def strict_transfer_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Offboarding transfer counts must be integers")
        return value


class OffboardingUnresolved(ClosedModel):
    project_id: uuid.UUID
    role: OffboardingRole
    reason: Literal["emergency_suspension_requires_later_handoff"]
    batch_ids: list[uuid.UUID]
    task_count: int = Field(ge=0, le=_INT64_MAX, strict=True)
    lock_count: int = Field(ge=0, le=_INT64_MAX, strict=True)

    @field_validator("task_count", "lock_count", mode="before")
    @classmethod
    def strict_unresolved_counts(cls, value):
        if type(value) is not int:
            raise ValueError("Offboarding unresolved counts must be integers")
        return value


class OffboardingResultRecord(ClosedModel):
    user: DirectoryUserRecord
    status: Literal["suspended"]
    mode: OffboardingMode
    transfers: list[OffboardingTransfer]
    unresolved: list[OffboardingUnresolved]
    revoked_api_key_ids: list[uuid.UUID]
    # A successful private write must have a typed Audit row. The public legacy
    # response remains nullable, but accepting null here could commit a partial
    # result after the owner failed to record its required Audit event.
    audit_id: int = Field(ge=1, le=_INT64_MAX, strict=True)


class OffboardingPreviewData(ClosedModel):
    preview: OffboardingPreviewRecord


class OffboardingCommitData(ClosedModel):
    result: OffboardingResultRecord


class _WireSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-offboarding/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class PreviewOffboardingReply(_WireSuccessBase):
    operation: Literal["preview_offboarding"]
    data: OffboardingPreviewData


class CommitOffboardingReply(_WireSuccessBase):
    operation: Literal["commit_offboarding"]
    data: OffboardingCommitData


OffboardingReply: TypeAlias = Annotated[
    PreviewOffboardingReply | CommitOffboardingReply,
    Field(discriminator="operation"),
]
_OFFBOARDING_REPLIES = TypeAdapter(OffboardingReply)


class PreviewStaleDetail(ClosedModel):
    code: Literal["offboarding_preview_stale"]
    expected_preview_version: str = Field(strict=True)
    current_preview_version: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    blockers: list[OffboardingBlocker]


class ReceiverRequiredDetail(ClosedModel):
    code: Literal["receiver_required"]
    project_id: uuid.UUID
    role: OffboardingRole


class ReceiverIneligibleDetail(ClosedModel):
    code: Literal["receiver_ineligible"]
    project_id: uuid.UUID
    role: OffboardingRole
    receiver_id: uuid.UUID


class OffboardingBlockedDetail(ClosedModel):
    code: Literal["offboarding_blocked"]
    blockers: list[OffboardingBlocker]


class OffboardingTaskBusyDetail(ClosedModel):
    code: Literal["offboarding_busy"]


class OffboardingResourceBusyDetail(ClosedModel):
    code: Literal["offboarding_busy"]
    message: Literal["职责正在变化，请刷新预览后重试"]


OffboardingFailureDetailValue: TypeAlias = (
    str
    | PreviewStaleDetail
    | ReceiverRequiredDetail
    | ReceiverIneligibleDetail
    | OffboardingBlockedDetail
    | OffboardingTaskBusyDetail
    | OffboardingResourceBusyDetail
    | None
)


class OffboardingFailureDetail(ClosedModel):
    code: OffboardingFailureCode
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: OffboardingFailureDetailValue

    @model_validator(mode="after")
    def fields_match_code(self) -> OffboardingFailureDetail:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Offboarding failure requires status_code and detail")
        if self.code in _AUTH_FAILURES:
            valid = self.status_code is None and self.detail is None
        elif self.code == "invalid_request":
            valid = self.status_code == 400 and self.detail is None
        elif self.code in _DETAILS:
            status_code, expected_detail = _DETAILS[self.code]
            valid = self.status_code == status_code and self.detail == expected_detail
        elif self.code == "offboarding_preview_stale":
            valid = self.status_code == 409 and isinstance(
                self.detail, PreviewStaleDetail
            )
        elif self.code == "receiver_required":
            valid = self.status_code == 409 and isinstance(
                self.detail, ReceiverRequiredDetail
            )
        elif self.code == "receiver_ineligible":
            valid = self.status_code == 409 and isinstance(
                self.detail, ReceiverIneligibleDetail
            )
        elif self.code == "offboarding_blocked":
            valid = self.status_code == 409 and isinstance(
                self.detail, OffboardingBlockedDetail
            )
        elif self.code == "offboarding_busy":
            valid = self.status_code == 409 and isinstance(
                self.detail, (OffboardingTaskBusyDetail, OffboardingResourceBusyDetail)
            )
        else:
            valid = False
        if not valid:
            raise ValueError("Offboarding failure fields do not match its code")
        return self


class OffboardingFailureReply(ClosedModel):
    protocol: Literal["aap-identity-offboarding/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: OffboardingFailureDetail


class OffboardingFailure(Exception):
    def __init__(
        self,
        code: OffboardingFailureCode,
        status_code: int | None = None,
        detail: str | dict[str, Any] | ClosedModel | None = None,
    ):
        if isinstance(detail, ClosedModel):
            detail = detail.model_dump(mode="json", by_alias=True)
        try:
            parsed = OffboardingFailureDetail.model_validate_json(
                json.dumps(
                    {"code": code, "status_code": status_code, "detail": detail},
                    ensure_ascii=True,
                    allow_nan=False,
                )
            )
        except (ValidationError, ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid offboarding failure") from None
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = (
            parsed.detail.model_dump(mode="json", by_alias=True)
            if isinstance(parsed.detail, ClosedModel)
            else parsed.detail
        )
        super().__init__(code)


def offboarding_failure_reply(
    failure: OffboardingFailure | OffboardingFailureCode,
) -> dict:
    if isinstance(failure, OffboardingFailure):
        code, status_code, detail = failure.code, failure.status_code, failure.detail
    else:
        code = failure
        if code in _AUTH_FAILURES:
            status_code, detail = None, None
        elif code == "invalid_request":
            status_code, detail = 400, None
        elif code == "offboarding_preview_stale":
            raise ValueError("Stale offboarding failure requires its detail")
        elif code in {
            "receiver_required",
            "receiver_ineligible",
            "offboarding_blocked",
            "offboarding_busy",
        }:
            raise ValueError("Structured offboarding failure requires its detail")
        else:
            try:
                status_code, detail = _DETAILS[code]
            except KeyError:
                raise ValueError("Failure requires status and detail") from None
    encoded = json.dumps(
        {
            "schema": OFFBOARDING_PROTOCOL,
            "ok": False,
            "error": {"code": code, "status_code": status_code, "detail": detail},
        },
        ensure_ascii=True,
        allow_nan=False,
    )
    return OffboardingFailureReply.model_validate_json(encoded).model_dump(
        mode="json", by_alias=True
    )


class OffboardingMemberFact(ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    role: str = Field(strict=True)


class OffboardingProjectFact(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    owner_id: uuid.UUID | None


class OffboardingBatchFact(ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str = Field(strict=True)
    status: str = Field(strict=True)
    annotator_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    assigned_user_ids: Any


class OffboardingTaskFact(ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    status: str = Field(strict=True)
    assignee_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    version: int | None

    @field_validator("version", mode="before")
    @classmethod
    def strict_task_version(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("Task versions must be integer or null")
        return value


class OffboardingLockFact(ClosedModel):
    id: uuid.UUID
    task_id: uuid.UUID
    expire_at: datetime

    @field_validator("expire_at")
    @classmethod
    def aware_expiration(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Task lock expiry must be timezone-aware")
        return value.astimezone(UTC)


class OffboardingApiKeyFact(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    key_prefix: str = Field(strict=True)
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @field_validator("last_used_at", "revoked_at", "created_at")
    @classmethod
    def aware_key_times(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("API key timestamps must be timezone-aware")
            return value.astimezone(UTC)
        return None


class OffboardingCandidateUser(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    email: str = Field(strict=True)
    role: str = Field(strict=True)


class OffboardingCandidateMembership(ClosedModel):
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: LabelingOffboardingRole


class OffboardingSnapshotFacts(ClosedModel):
    target: DirectoryUserRecord
    projects: list[OffboardingProjectFact]
    members: list[OffboardingMemberFact]
    batches: list[OffboardingBatchFact]
    tasks: list[OffboardingTaskFact]
    locks: list[OffboardingLockFact]
    api_keys: list[OffboardingApiKeyFact]
    candidate_users: list[OffboardingCandidateUser]
    candidate_memberships: list[OffboardingCandidateMembership]
    other_unreserved: int = Field(ge=0, le=_INT64_MAX, strict=True)

    @field_validator("other_unreserved", mode="before")
    @classmethod
    def strict_unreserved_count(cls, value):
        if type(value) is not int:
            raise ValueError("Super-admin counts must be integers")
        return value

    @model_validator(mode="after")
    def facts_match_target(self) -> OffboardingSnapshotFacts:
        project_ids = [project.id for project in self.projects]
        if project_ids != sorted(set(project_ids)):
            raise ValueError("Offboarding project facts must be unique and sorted")
        scope_ids = set(project_ids)
        if any(member.project_id not in scope_ids for member in self.members):
            raise ValueError("Offboarding membership project is outside scope")
        if any(batch.project_id not in scope_ids for batch in self.batches):
            raise ValueError("Offboarding batch project is outside scope")
        batch_order = [(batch.project_id, batch.id) for batch in self.batches]
        if batch_order != sorted(batch_order):
            raise ValueError("Offboarding batch facts must retain source order")
        # ProjectMember.role ordering comes from the legacy database collation.
        # Preserve that row order instead of re-sorting role text in Python.
        if any(
            task.project_id is not None and task.project_id not in scope_ids
            for task in self.tasks
        ):
            raise ValueError("Offboarding task project is outside scope")
        task_order = [
            (task.project_id is None, task.project_id or uuid.UUID(int=0), task.id)
            for task in self.tasks
        ]
        if task_order != sorted(task_order):
            raise ValueError("Offboarding task facts must retain source order")
        lock_order = [(lock.task_id, lock.id) for lock in self.locks]
        if lock_order != sorted(lock_order):
            raise ValueError("Offboarding lock facts must retain source order")
        if any(
            previous.created_at < current.created_at
            or (previous.created_at == current.created_at and previous.id > current.id)
            for previous, current in zip(self.api_keys, self.api_keys[1:])
        ):
            raise ValueError("Offboarding API-key facts must retain source order")
        candidate_user_ids = [user.id for user in self.candidate_users]
        candidate_user_set = set(candidate_user_ids)
        if len(candidate_user_ids) != len(candidate_user_set):
            raise ValueError("Offboarding candidate users must be unique")
        if self.target.id in candidate_user_set:
            raise ValueError("Offboarding target cannot be its own receiver")
        if any(
            membership.project_id not in scope_ids
            for membership in self.candidate_memberships
        ):
            raise ValueError("Offboarding candidate project is outside scope")
        if any(
            membership.user_id not in candidate_user_set
            for membership in self.candidate_memberships
        ):
            raise ValueError("Offboarding candidate membership has no candidate user")
        return self


class OffboardingScopeBasis(ClosedModel):
    target_role: str = Field(strict=True)
    target_was_active: bool
    project_ids: list[uuid.UUID]

    @field_validator("target_was_active", mode="before")
    @classmethod
    def strict_target_was_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Scope target activity must be a boolean")
        return value

    @model_validator(mode="after")
    def project_ids_sorted_unique(self) -> OffboardingScopeBasis:
        if self.project_ids != sorted(set(self.project_ids)):
            raise ValueError("Accepted scope project ids must be sorted and unique")
        return self


class OffboardingReplayScopeStatus(ClosedModel):
    projects_exist: bool
    all_projects_owned: bool

    @field_validator("projects_exist", "all_projects_owned", mode="before")
    @classmethod
    def strict_scope_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("Offboarding replay scope flags must be booleans")
        return value


class OffboardingPrepareSnapshotData(ClosedModel):
    kind: Literal["snapshot"]
    transaction_id: str = Field(min_length=1, strict=True)
    target_generation: int = Field(ge=0, lt=2**63, strict=True)
    target_is_active: bool
    facts: OffboardingSnapshotFacts

    @field_validator("target_is_active", mode="before")
    @classmethod
    def strict_target_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Target activity must be a boolean")
        return value

    @model_validator(mode="after")
    def generation_matches_record(self) -> OffboardingPrepareSnapshotData:
        if self.target_is_active is not self.facts.target.is_active:
            raise ValueError("Snapshot target activity is inconsistent")
        return self


class OffboardingPrepareReplayData(ClosedModel):
    kind: Literal["replay"]
    transaction_id: str = Field(min_length=1, strict=True)
    target_generation: int = Field(ge=0, lt=2**63, strict=True)
    result: OffboardingResultRecord
    scope_basis: OffboardingScopeBasis
    scope_status: OffboardingReplayScopeStatus


OffboardingPrepareData: TypeAlias = Annotated[
    OffboardingPrepareSnapshotData | OffboardingPrepareReplayData,
    Field(discriminator="kind"),
]


class OffboardingPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-offboarding-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-offboarding/v1"]


class NativeOffboardingPreparePayload(ClosedModel):
    target_user_id: uuid.UUID
    receiver_ids: list[uuid.UUID]
    payload_project_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def sorted_unique_ids(self) -> NativeOffboardingPreparePayload:
        if self.receiver_ids != sorted(set(self.receiver_ids)):
            raise ValueError("Receiver lock ids must be sorted and unique")
        if self.payload_project_ids != sorted(set(self.payload_project_ids)):
            raise ValueError("Payload project lock ids must be sorted and unique")
        return self


class OffboardingOwnerPlan(ClosedModel):
    project_id: uuid.UUID
    expected_owner_id: uuid.UUID
    receiver_id: uuid.UUID


class OffboardingLabelingPlan(ClosedModel):
    project_id: uuid.UUID
    role: LabelingOffboardingRole
    receiver_id: uuid.UUID
    batch_ids: list[uuid.UUID]
    task_ids: list[uuid.UUID]


class OffboardingApplyPayload(ClosedModel):
    transaction_id: str = Field(min_length=1, strict=True)
    target_user_id: uuid.UUID
    expected_target_generation: int = Field(ge=0, lt=2**63, strict=True)
    expected_target_is_active: bool
    mode: OffboardingMode
    normalized_reason: str | None = Field(max_length=500, strict=True)
    reason_present: bool
    owner_plan: list[OffboardingOwnerPlan]
    labeling_plan: list[OffboardingLabelingPlan]
    transfers: list[OffboardingTransfer]
    unresolved: list[OffboardingUnresolved]
    scope_basis: OffboardingScopeBasis

    @field_validator("expected_target_is_active", "reason_present", mode="before")
    @classmethod
    def strict_apply_booleans(cls, value):
        if type(value) is not bool:
            raise ValueError("Offboarding apply flags must be booleans")
        return value

    @model_validator(mode="after")
    def reason_flags_match(self) -> OffboardingApplyPayload:
        if self.normalized_reason is not None and (
            not self.normalized_reason
            or self.normalized_reason != self.normalized_reason.strip()
        ):
            raise ValueError("Offboarding reason must be Python-strip normalized")
        if self.reason_present is not bool(self.normalized_reason):
            raise ValueError("Offboarding reason flag does not match normalized text")
        if self.scope_basis.target_was_active is not self.expected_target_is_active:
            raise ValueError(
                "Offboarding scope basis activity does not match target fence"
            )
        return self


class _NativeCommandBase(ClosedModel):
    protocol: Literal["aap-identity-offboarding-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    operation: OffboardingOperation
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
            raise ValueError("API-key hash must match credential kind")
        return self


class NativeOffboardingPrepareCommand(_NativeCommandBase):
    phase: Literal["prepare"]
    payload: NativeOffboardingPreparePayload


class NativeOffboardingApplyCommand(_NativeCommandBase):
    phase: Literal["apply"]
    operation: Literal["commit_offboarding"]
    payload: OffboardingApplyPayload


NativeOffboardingCommand: TypeAlias = Annotated[
    NativeOffboardingPrepareCommand | NativeOffboardingApplyCommand,
    Field(discriminator="phase"),
]
_NATIVE_OFFBOARDING_COMMANDS = TypeAdapter(NativeOffboardingCommand)


def _native_command_base(
    request,
    actor,
    expected_key_hash,
    request_fingerprint,
    derivation_id,
    phase,
    payload,
):
    values = {
        "schema": NATIVE_OFFBOARDING_PROTOCOL,
        "phase": phase,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "actor": actor.model_dump(mode="json"),
        "expected_key_hash": expected_key_hash,
        "request_fingerprint": request_fingerprint,
        "derivation_id": derivation_id,
        "audit_context": request.audit_context.model_dump(mode="json"),
        "payload": payload,
    }
    try:
        encoded = json.dumps(
            values, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        return _NATIVE_OFFBOARDING_COMMANDS.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def native_prepare_offboarding_command(
    request: OffboardingRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
) -> NativeOffboardingPrepareCommand:
    if request.operation == "preview_offboarding":
        target_user_id = request.payload.target_user_id
        receiver_ids: set[uuid.UUID] = set()
        project_ids: set[uuid.UUID] = set()
    else:
        target_user_id = request.payload.target_user_id
        receiver_ids = {
            receiver_id
            for project in request.payload.projects
            for receiver_id in (
                project.owner_receiver_id,
                project.annotator_receiver_id,
                project.reviewer_receiver_id,
            )
            if receiver_id is not None
        }
        project_ids = {project.project_id for project in request.payload.projects}
    payload = {
        "target_user_id": str(target_user_id),
        "receiver_ids": [str(item) for item in sorted(receiver_ids)],
        "payload_project_ids": [str(item) for item in sorted(project_ids)],
    }
    command = _native_command_base(
        request,
        actor,
        expected_key_hash,
        request_fingerprint,
        derivation_id,
        "prepare",
        payload,
    )
    if not isinstance(command, NativeOffboardingPrepareCommand):
        raise IdentityUnavailable()
    return command


def native_apply_offboarding_command(
    request: CommitOffboardingRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
    payload: OffboardingApplyPayload,
) -> NativeOffboardingApplyCommand:
    command = _native_command_base(
        request,
        actor,
        expected_key_hash,
        request_fingerprint,
        derivation_id,
        "apply",
        payload.model_dump(mode="json", by_alias=True),
    )
    if not isinstance(command, NativeOffboardingApplyCommand):
        raise IdentityUnavailable()
    return command


def decode_native_offboarding_command(
    payload: dict | bytes | ClosedModel,
) -> NativeOffboardingCommand:
    try:
        return _NATIVE_OFFBOARDING_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class _NativeSuccessBase(ClosedModel):
    protocol: Literal["aap-identity-offboarding-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: OffboardingOperation
    operation_id: uuid.UUID
    principal: Principal


class NativeOffboardingPrepareSuccess(_NativeSuccessBase):
    phase: Literal["prepare"]
    data: OffboardingPrepareData


class NativeOffboardingApplyData(ClosedModel):
    result: OffboardingResultRecord


class NativeOffboardingApplySuccess(_NativeSuccessBase):
    phase: Literal["apply"]
    operation: Literal["commit_offboarding"]
    data: NativeOffboardingApplyData
    replayed: bool

    @field_validator("replayed", mode="before")
    @classmethod
    def strict_replayed(cls, value):
        if type(value) is not bool:
            raise ValueError("Native replayed must be a boolean")
        return value


NativeOffboardingSuccess: TypeAlias = Annotated[
    NativeOffboardingPrepareSuccess | NativeOffboardingApplySuccess,
    Field(discriminator="phase"),
]
_NATIVE_OFFBOARDING_SUCCESSES = TypeAdapter(NativeOffboardingSuccess)


class NativeOffboardingFailureReply(ClosedModel):
    protocol: Literal["aap-identity-offboarding-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: OffboardingFailureDetail


def _expected_failure(
    request, error: OffboardingFailureDetail, actor: Principal
) -> bool:
    code, detail = error.code, error.detail
    target_id = request.payload.target_user_id
    if code in _AUTH_FAILURES:
        return error.status_code is None and detail is None
    if code == "invalid_request":
        return error.status_code == 400 and detail is None
    if code == "manager_required":
        return error.status_code == 403 and detail == _DETAILS[code][1]
    if code == "target_not_found":
        return error.status_code == 404 and detail == _DETAILS[code][1]
    if code == "self_offboard":
        return (
            target_id == actor.actor_id
            and error.status_code == 400
            and detail == _DETAILS[code][1]
        )
    if code == "last_super_admin":
        return (
            target_id != actor.actor_id
            and error.status_code == 400
            and detail == _DETAILS[code][1]
        )
    if code in {"target_role_forbidden", "inactive_unmanaged", "scope_forbidden"}:
        return (
            target_id != actor.actor_id
            and error.status_code == _DETAILS[code][0]
            and detail == _DETAILS[code][1]
        )
    if code == "offboarding_preview_stale":
        return (
            isinstance(request, CommitOffboardingRequest)
            and request.operation == "commit_offboarding"
            and isinstance(detail, PreviewStaleDetail)
            and detail.expected_preview_version == request.payload.preview_version
            and error.status_code == 409
        )
    if code in {"historical_target", "state_forbidden"}:
        return (
            request.operation == "commit_offboarding"
            and error.status_code == _DETAILS[code][0]
            and detail == _DETAILS[code][1]
        )
    if code == "duplicate_project":
        return (
            request.operation == "commit_offboarding"
            and _has_duplicate_projects(request)
            and error.status_code == 422
            and detail == _DETAILS[code][1]
        )
    if code == "receiver_required":
        if (
            not _is_handoff_commit(request)
            or not isinstance(detail, ReceiverRequiredDetail)
            or error.status_code != 409
        ):
            return False
        project = _project_mapping(request, detail.project_id)
        if project is None:
            return True
        return getattr(project, f"{detail.role}_receiver_id") is None
    if code == "receiver_ineligible":
        if (
            not _is_handoff_commit(request)
            or not isinstance(detail, ReceiverIneligibleDetail)
            or error.status_code != 409
        ):
            return False
        project = _project_mapping(request, detail.project_id)
        return (
            project is not None
            and getattr(project, f"{detail.role}_receiver_id") == detail.receiver_id
        )
    if code == "offboarding_blocked":
        return (
            _is_handoff_commit(request)
            and isinstance(detail, OffboardingBlockedDetail)
            and bool(detail.blockers)
            and error.status_code == 409
        )
    if code == "account_busy":
        return error.status_code == 409 and detail == _DETAILS[code][1]
    if code == "offboarding_busy":
        return (
            request.operation == "commit_offboarding"
            and error.status_code == 409
            and isinstance(
                detail, (OffboardingTaskBusyDetail, OffboardingResourceBusyDetail)
            )
        )
    if code == "operation_conflict":
        return (
            request.operation == "commit_offboarding"
            and error.status_code == 409
            and detail is None
        )
    if code == "unavailable":
        return error.status_code == 503 and detail is None
    return False


def _is_handoff_commit(request) -> bool:
    return (
        request.operation == "commit_offboarding"
        and getattr(request.payload, "mode", None) == "handoff"
    )


def _project_mapping(
    request, project_id: uuid.UUID
) -> OffboardingProjectRequest | None:
    if request.operation != "commit_offboarding" or not isinstance(
        request.payload, CommitOffboardingPayload
    ):
        return None
    matches = [
        project
        for project in request.payload.projects
        if project.project_id == project_id
    ]
    return matches[0] if len(matches) == 1 else None


def _has_duplicate_projects(request) -> bool:
    return (
        request.operation == "commit_offboarding"
        and isinstance(request.payload, CommitOffboardingPayload)
        and len({project.project_id for project in request.payload.projects})
        != len(request.payload.projects)
    )


def _decode_failure(
    payload: bytes, *, request: OffboardingRequest, actor: Principal, native: bool
) -> OffboardingFailure:
    model = NativeOffboardingFailureReply if native else OffboardingFailureReply
    try:
        reply = model.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if native and reply.error.code in {
        "offboarding_preview_stale",
        "receiver_required",
        "receiver_ineligible",
        "offboarding_blocked",
        "duplicate_project",
    }:
        raise IdentityUnavailable()
    if not _expected_failure(request, reply.error, actor):
        raise IdentityUnavailable()
    detail = reply.error.detail
    if isinstance(detail, ClosedModel):
        detail = detail.model_dump(mode="json", by_alias=True)
    return OffboardingFailure(reply.error.code, reply.error.status_code, detail)


def _validate_preview_success(
    request: PreviewOffboardingRequest, reply: PreviewOffboardingReply
) -> None:
    preview = reply.data.preview
    if (
        reply.operation_id != request.operation_id
        or preview.user.id != request.payload.target_user_id
    ):
        raise IdentityUnavailable()
    if _HEX_64.fullmatch(preview.preview_version) is None:
        raise IdentityUnavailable()


def _normalize_reason(reason: str) -> str | None:
    return (reason or "").strip()[:500] or None


def _validate_commit_success(
    request: CommitOffboardingRequest, reply: CommitOffboardingReply
) -> None:
    _validate_commit_result(request, reply.data.result, reply.principal)


def _validate_commit_result(
    request: CommitOffboardingRequest,
    result: OffboardingResultRecord,
    principal: Principal,
) -> None:
    _validate_suspended_result(
        result,
        target_id=request.payload.target_user_id,
        principal=principal,
        mode=request.payload.mode,
        reason=_normalize_reason(request.payload.reason),
    )


def _validate_suspended_result(
    result: OffboardingResultRecord,
    *,
    target_id: uuid.UUID,
    principal: Principal,
    mode: OffboardingMode,
    reason: str | None,
) -> None:
    if (
        result.user.id != target_id
        or result.status != "suspended"
        or result.mode != mode
        or result.user.is_active is not False
        or result.user.status != "offline"
        or result.user.disabled_kind
        != ("emergency_suspended" if mode == "emergency_suspend" else "suspended")
        or result.user.disabled_reason != reason
        or result.user.disabled_by != principal.actor_id
        or result.user.disabled_at is None
        or any(transfer.receiver_id is None for transfer in result.transfers)
        or any(
            item.project_id is None or item.role is None for item in result.unresolved
        )
    ):
        raise IdentityUnavailable()


def _validate_native_success_binding(command, reply) -> None:
    """Bind private facts and post-write results before the SQL transaction commits."""
    request = command
    target_id = request.payload.target_user_id
    if isinstance(reply, NativeOffboardingPrepareSuccess):
        if request.operation == "preview_offboarding":
            if (
                not isinstance(reply.data, OffboardingPrepareSnapshotData)
                or reply.data.facts.target.id != target_id
            ):
                raise IdentityUnavailable()
            return
        if isinstance(reply.data, OffboardingPrepareSnapshotData):
            if reply.data.facts.target.id != target_id:
                raise IdentityUnavailable()
            return
        if isinstance(reply.data, OffboardingPrepareReplayData):
            if (
                reply.data.result.user.id != target_id
                or reply.data.scope_basis.target_role != reply.data.result.user.role
            ):
                raise IdentityUnavailable()
            _validate_suspended_result(
                reply.data.result,
                target_id=target_id,
                principal=reply.principal,
                mode=reply.data.result.mode,
                reason=reply.data.result.user.disabled_reason,
            )
            return
        raise IdentityUnavailable()

    if isinstance(reply, NativeOffboardingApplySuccess):
        if request.operation != "commit_offboarding":
            raise IdentityUnavailable()
        _validate_suspended_result(
            reply.data.result,
            target_id=target_id,
            principal=reply.principal,
            mode=command.payload.mode,
            reason=command.payload.normalized_reason,
        )
        return
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
        raise ValueError("Expected admitted Principal for offboarding reply")
    if raw["ok"] is False:
        return _decode_failure(encoded, request=command, actor=actor, native=native)
    adapter = _NATIVE_OFFBOARDING_SUCCESSES if native else _OFFBOARDING_REPLIES
    try:
        reply = adapter.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
    if native:
        if (
            reply.operation_id != command.operation_id
            or reply.operation != command.operation
        ):
            raise IdentityUnavailable()
        if reply.phase != command.phase:
            raise IdentityUnavailable()
        if not _same_fenced_credential(reply.principal, actor):
            raise IdentityUnavailable()
        if not reply.principal.is_active or reply.principal.role not in _MANAGER_ROLES:
            raise IdentityUnavailable()
        _validate_native_success_binding(command, reply)
        return reply
    if (
        reply.operation_id != command.operation_id
        or reply.operation != command.operation
    ):
        raise IdentityUnavailable()
    if (
        not _same_fenced_credential(reply.principal, actor)
        or not reply.principal.is_active
        or reply.principal.role not in _MANAGER_ROLES
    ):
        raise IdentityUnavailable()
    if isinstance(reply, PreviewOffboardingReply):
        _validate_preview_success(command, reply)
    else:
        _validate_commit_success(command, reply)
    return reply


def decode_native_offboarding_reply(
    command: NativeOffboardingCommand, payload: dict | bytes | ClosedModel
) -> NativeOffboardingSuccess:
    reply = _decode_reply(
        command, payload, native=True, expected_principal=command.actor
    )
    if isinstance(reply, OffboardingFailure):
        raise reply
    return reply


def decode_offboarding_reply(
    request: OffboardingRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal,
) -> OffboardingReply:
    reply = _decode_reply(
        request, payload, native=False, expected_principal=expected_principal
    )
    if isinstance(reply, OffboardingFailure):
        raise reply
    return reply


def decode_offboarding_policy(
    payload: dict | bytes | ClosedModel,
) -> OffboardingPolicyReply:
    try:
        return OffboardingPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
