"""Portable owner for offboarding preview snapshots and commits."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections import defaultdict
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable, Principal
from .offboarding_contract import (
    OFFBOARDING_DERIVATION,
    OFFBOARDING_PROTOCOL,
    CommitOffboardingReply,
    CommitOffboardingRequest,
    NativeOffboardingApplySuccess,
    NativeOffboardingPrepareSuccess,
    OffboardingApplyPayload,
    OffboardingApiKeyPreview,
    OffboardingBatchRef,
    OffboardingBatchFact,
    OffboardingBlocker,
    OffboardingFailure,
    OffboardingLabelingPlan,
    OffboardingOwnerPlan,
    OffboardingPrepareReplayData,
    OffboardingPrepareSnapshotData,
    OffboardingPreviewRecord,
    OffboardingProjectRoles,
    OffboardingProjectPreview,
    OffboardingProjectTasks,
    OffboardingReceiverOption,
    OffboardingRequest,
    OffboardingResultRecord,
    OffboardingRole,
    OffboardingRolePreview,
    OffboardingScopeBasis,
    OffboardingSnapshotFacts,
    OffboardingTaskCounts,
    OffboardingTransfer,
    OffboardingUnresolved,
    LabelingOffboardingRole,
    PreviewOffboardingRequest,
    _DETAILS,
    decode_native_offboarding_reply,
    decode_offboarding_reply,
    native_apply_offboarding_command,
    native_prepare_offboarding_command,
    offboarding_failure_reply,
)

_REACTIVATABLE_KINDS = {"suspended", "emergency_suspended"}
_HISTORICAL_KINDS = {"deleted", "historical_unknown"}
_HANDOFF_STATUSES = {"pending", "in_progress", "review", "rejected"}
_ROLE_ORDER: tuple[OffboardingRole, ...] = ("owner", "annotator", "reviewer")
_LABELING_ROLES: tuple[LabelingOffboardingRole, ...] = ("annotator", "reviewer")
_VALID_PROJECT_ROLES = {"annotator", "reviewer", "viewer"}
_MANAGER_ROLES = {"super_admin", "project_admin"}


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class OffboardingTransaction(Protocol):
    async def prepare_offboarding_command(self, payload: dict) -> dict: ...

    async def apply_offboarding_command(self, payload: dict) -> dict: ...


class OffboardingData(Protocol):
    def transaction(
        self,
    ) -> AbstractAsyncContextManager[OffboardingTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    if not isinstance(secret, str) or not secret:
        raise IdentityUnavailable()
    try:
        master = hmac.new(
            secret.encode("utf-8"),
            OFFBOARDING_DERIVATION.encode("ascii"),
            hashlib.sha256,
        ).digest()
    except (UnicodeError, TypeError, ValueError):
        raise IdentityUnavailable() from None
    return master, hashlib.sha256(master).hexdigest()


def offboarding_request_fingerprint(secret: str, request: OffboardingRequest) -> str:
    master, _ = _derivation(secret)
    canonical = json.dumps(
        {
            "operation": request.operation,
            "payload": request.payload.model_dump(mode="json", by_alias=True),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(master, canonical, hashlib.sha256).hexdigest()


def offboarding_derivation_id(secret: str) -> str:
    return _derivation(secret)[1]


def legacy_offboarding_resource_state(facts: OffboardingSnapshotFacts) -> dict:
    """Reproduce the legacy Python resource projection byte-for-byte."""

    candidate_memberships = {
        (item.project_id, item.user_id, item.role)
        for item in facts.candidate_memberships
    }
    members_by_project: dict[uuid.UUID, set[str]] = defaultdict(set)
    batches_by_project: dict[uuid.UUID, list[OffboardingBatchFact]] = defaultdict(list)
    tasks_by_project: dict[uuid.UUID, list] = defaultdict(list)
    locks_by_task: dict[uuid.UUID, int] = defaultdict(int)
    for member in facts.members:
        members_by_project[member.project_id].add(member.role)
    for batch in facts.batches:
        batches_by_project[batch.project_id].append(batch)
    for task in facts.tasks:
        if task.project_id is not None:
            tasks_by_project[task.project_id].append(task)
    for lock in facts.locks:
        locks_by_task[lock.task_id] += 1

    receivers: dict[str, dict[str, list[dict[str, object]]]] = {}
    for project in facts.projects:
        role_options: dict[str, list[dict[str, object]]] = {}
        for role in _ROLE_ORDER:
            options: list[dict[str, object]] = []
            for user in facts.candidate_users:
                if role == "owner":
                    if user.role != "project_admin":
                        continue
                    member_role = None
                else:
                    if (project.id, user.id, role) not in candidate_memberships:
                        continue
                    member_role = role
                options.append(
                    {
                        "id": user.id,
                        "name": user.name,
                        "email": user.email,
                        "role": user.role,
                        "project_member_role": member_role,
                    }
                )
            # candidate_users retain the SQL name/email/id collation order.
            role_options[role] = options
        receivers[str(project.id)] = role_options

    target = facts.target
    return {
        "target": {
            "id": str(target.id),
            "is_active": target.is_active,
            "disabled_kind": target.disabled_kind,
            "disabled_at": target.disabled_at,
            "disabled_by": target.disabled_by,
            "disabled_reason": target.disabled_reason,
            "role": target.role,
        },
        "projects": [
            {
                "id": str(project.id),
                "owner_id": str(project.owner_id) if project.owner_id else None,
            }
            for project in facts.projects
        ],
        "members": [
            {
                "id": str(member.id),
                "project_id": str(member.project_id),
                "role": member.role,
            }
            for member in facts.members
        ],
        "batches": [
            {
                "id": str(batch.id),
                "project_id": str(batch.project_id),
                "status": batch.status,
                "annotator_id": str(batch.annotator_id) if batch.annotator_id else None,
                "reviewer_id": str(batch.reviewer_id) if batch.reviewer_id else None,
                # Preserve Python ``list(raw or [])`` for historical JSON values.
                "assigned_user_ids": list(batch.assigned_user_ids or []),
            }
            for batch in facts.batches
        ],
        "tasks": [
            {
                "id": str(task.id),
                "project_id": str(task.project_id),
                "batch_id": str(task.batch_id) if task.batch_id else None,
                "status": task.status,
                "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                "reviewer_id": str(task.reviewer_id) if task.reviewer_id else None,
                "version": task.version,
            }
            for task in facts.tasks
        ],
        "locks": [
            {
                "id": str(lock.id),
                "task_id": str(lock.task_id),
                "expire_at": lock.expire_at,
            }
            for lock in facts.locks
        ],
        "api_keys": [
            {
                "id": str(key.id),
                "revoked_at": key.revoked_at,
                "name": key.name,
                "prefix": key.key_prefix,
            }
            for key in facts.api_keys
        ],
        "receivers": receivers,
    }


def legacy_offboarding_preview_version(facts: OffboardingSnapshotFacts) -> str:
    try:
        canonical = json.dumps(
            legacy_offboarding_resource_state(facts), sort_keys=True, default=str
        ).encode()
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise IdentityUnavailable() from None
    return hashlib.sha256(canonical).hexdigest()


def offboarding_reason_values(raw_reason: str) -> tuple[str | None, bool]:
    stripped = (raw_reason or "").strip()
    return stripped[:500] or None, bool(stripped)


def _options_by_role(
    facts: OffboardingSnapshotFacts,
) -> dict[uuid.UUID, dict[str, list[OffboardingReceiverOption]]]:
    memberships = {
        (item.project_id, item.user_id, item.role)
        for item in facts.candidate_memberships
    }
    result: dict[uuid.UUID, dict[str, list[OffboardingReceiverOption]]] = {}
    for project in facts.projects:
        role_map = {}
        for role in _ROLE_ORDER:
            options = []
            for user in facts.candidate_users:
                if role == "owner":
                    if user.role != "project_admin":
                        continue
                    member_role = None
                else:
                    if (project.id, user.id, role) not in memberships:
                        continue
                    member_role = role
                options.append(
                    OffboardingReceiverOption(
                        id=user.id,
                        name=user.name,
                        email=user.email,
                        role=user.role,
                        project_member_role=member_role,
                    )
                )
            # candidate_users retain the SQL name/email/id collation order.
            role_map[role] = options
        result[project.id] = role_map
    return result


def build_offboarding_preview(
    facts: OffboardingSnapshotFacts,
) -> OffboardingPreviewRecord:
    """Apply the legacy preview policy to ordered, typed owner facts."""
    target = facts.target
    version = legacy_offboarding_preview_version(facts)
    options = _options_by_role(facts)
    member_roles: dict[uuid.UUID, set[str]] = defaultdict(set)
    batches_by_project: dict[uuid.UUID, list[OffboardingBatchFact]] = defaultdict(list)
    tasks_by_project: dict[uuid.UUID, list] = defaultdict(list)
    locks_by_task: dict[uuid.UUID, int] = defaultdict(int)
    for member in facts.members:
        member_roles[member.project_id].add(member.role)
    for batch in facts.batches:
        batches_by_project[batch.project_id].append(batch)
    for task in facts.tasks:
        if task.project_id is not None:
            tasks_by_project[task.project_id].append(task)
    for lock in facts.locks:
        locks_by_task[lock.task_id] += 1

    blockers: list[OffboardingBlocker] = []
    if not target.is_active and target.disabled_kind in _HISTORICAL_KINDS:
        blockers.append(
            OffboardingBlocker(
                code="historical_inactive",
                message="该账号的停用来源无法可靠判定，不能恢复或再次交接",
            )
        )

    projects: list[OffboardingProjectPreview] = []
    for project in facts.projects:
        role_present: dict[str, bool] = {}
        role_batches: dict[str, list[OffboardingBatchFact]] = {}
        project_blockers: list[OffboardingBlocker] = []
        project_members = member_roles[project.id]
        project_batches = batches_by_project[project.id]
        project_tasks = tasks_by_project[project.id]
        for role in _ROLE_ORDER:
            if role == "owner":
                matching_batches = []
                present = project.owner_id == target.id
            elif role == "annotator":
                matching_batches = [
                    batch
                    for batch in project_batches
                    if batch.annotator_id == target.id
                ]
                present = (
                    bool(matching_batches)
                    or any(task.assignee_id == target.id for task in project_tasks)
                    or "annotator" in project_members
                )
            else:
                matching_batches = [
                    batch for batch in project_batches if batch.reviewer_id == target.id
                ]
                present = (
                    bool(matching_batches)
                    or any(task.reviewer_id == target.id for task in project_tasks)
                    or "reviewer" in project_members
                )
            role_batches[role] = matching_batches
            role_present[role] = present
            if present and not options[project.id][role]:
                project_blockers.append(
                    OffboardingBlocker(
                        code=f"no_{role}_receiver",
                        message=(
                            f"项目「{project.name}」没有符合当前 {role} 权限的启用接收人"
                        ),
                    )
                )

        mismatched = any(role not in _VALID_PROJECT_ROLES for role in project_members)
        if any(
            (batch.annotator_id == target.id and "annotator" not in project_members)
            or (batch.reviewer_id == target.id and "reviewer" not in project_members)
            for batch in project_batches
        ):
            mismatched = True
        if mismatched:
            project_blockers.append(
                OffboardingBlocker(
                    code="historic_mixed_role",
                    message="该账号在项目成员或批次历史中同时出现不同职责，需人工核对",
                )
            )
        blockers.extend(project_blockers)

        counts = {
            role: {
                name: 0
                for name in ("pending", "in_progress", "review", "rejected", "locked")
            }
            for role in _LABELING_ROLES
        }
        for task in project_tasks:
            if task.status not in _HANDOFF_STATUSES:
                continue
            task_locks = locks_by_task[task.id]
            if task.assignee_id == target.id:
                counts["annotator"][task.status] += 1
                counts["annotator"]["locked"] += task_locks
            if task.reviewer_id == target.id:
                counts["reviewer"][task.status] += 1
                counts["reviewer"]["locked"] += task_locks

        projects.append(
            OffboardingProjectPreview(
                project_id=project.id,
                project_name=project.name,
                roles=OffboardingProjectRoles(
                    **{
                        role: OffboardingRolePreview(
                            present=role_present[role],
                            batches=[
                                OffboardingBatchRef(
                                    batch_id=batch.id,
                                    batch_name=batch.name,
                                )
                                for batch in role_batches[role]
                            ],
                            receiver_options=options[project.id][role],
                        )
                        for role in _ROLE_ORDER
                    }
                ),
                tasks=OffboardingProjectTasks(
                    annotator=OffboardingTaskCounts(**counts["annotator"]),
                    reviewer=OffboardingTaskCounts(**counts["reviewer"]),
                ),
                locked_task_count=sum(locks_by_task[task.id] for task in project_tasks),
                blockers=project_blockers,
            )
        )

    can_commit = bool(
        (target.is_active or target.disabled_kind in _REACTIVATABLE_KINDS)
        and not blockers
        and target.disabled_kind not in _HISTORICAL_KINDS
    )
    try:
        return OffboardingPreviewRecord(
            user=target,
            preview_version=version,
            generated_at=datetime.now(UTC),
            projects=projects,
            api_keys=[
                OffboardingApiKeyPreview(
                    id=key.id,
                    name=key.name,
                    key_prefix=key.key_prefix,
                    last_used_at=key.last_used_at,
                    revoked_at=key.revoked_at,
                )
                for key in facts.api_keys
            ],
            blockers=blockers,
            can_commit=can_commit,
        )
    except (ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def _assert_current_scope(actor: Principal, facts: OffboardingSnapshotFacts) -> None:
    target = facts.target
    if not actor.is_active or actor.role not in _MANAGER_ROLES:
        raise OffboardingFailure("manager_required", *_DETAILS["manager_required"])
    if actor.actor_id == target.id:
        raise OffboardingFailure("self_offboard", *_DETAILS["self_offboard"])
    if (
        target.is_active
        and target.role == "super_admin"
        and facts.other_unreserved == 0
    ):
        raise OffboardingFailure("last_super_admin", *_DETAILS["last_super_admin"])
    if actor.role != "project_admin":
        return
    if target.role != "employee":
        raise OffboardingFailure(
            "target_role_forbidden", *_DETAILS["target_role_forbidden"]
        )
    if not facts.projects:
        if not target.is_active:
            raise OffboardingFailure(
                "inactive_unmanaged", *_DETAILS["inactive_unmanaged"]
            )
    elif any(project.owner_id != actor.actor_id for project in facts.projects):
        raise OffboardingFailure("scope_forbidden", *_DETAILS["scope_forbidden"])


def _assert_replay_scope(actor: Principal, data: OffboardingPrepareReplayData) -> None:
    if not actor.is_active or actor.role not in _MANAGER_ROLES:
        raise OffboardingFailure("manager_required", *_DETAILS["manager_required"])
    if actor.role == "super_admin":
        return
    if data.scope_basis.target_role != "employee":
        raise OffboardingFailure(
            "target_role_forbidden", *_DETAILS["target_role_forbidden"]
        )
    if data.scope_basis.project_ids:
        if (
            not data.scope_status.projects_exist
            or not data.scope_status.all_projects_owned
        ):
            raise OffboardingFailure("scope_forbidden", *_DETAILS["scope_forbidden"])
        return
    if not data.scope_basis.target_was_active:
        raise OffboardingFailure("inactive_unmanaged", *_DETAILS["inactive_unmanaged"])


def _role_present(project: OffboardingProjectPreview, role: OffboardingRole) -> bool:
    return getattr(project.roles, role).present


def _request_map(request: CommitOffboardingRequest) -> dict[uuid.UUID, object]:
    items = request.payload.projects
    if len({item.project_id for item in items}) != len(items):
        raise OffboardingFailure("duplicate_project", *_DETAILS["duplicate_project"])
    return {item.project_id: item for item in items}


def _validate_receivers(
    request: CommitOffboardingRequest,
    preview: OffboardingPreviewRecord,
    project_requests: dict[uuid.UUID, object],
) -> None:
    for project in preview.projects:
        mapping = project_requests.get(project.project_id)
        for role in _ROLE_ORDER:
            if not _role_present(project, role):
                continue
            receiver_id = (
                getattr(mapping, f"{role}_receiver_id") if mapping is not None else None
            )
            if receiver_id is None:
                raise OffboardingFailure(
                    "receiver_required",
                    409,
                    {
                        "code": "receiver_required",
                        "project_id": str(project.project_id),
                        "role": role,
                    },
                )
            option_ids = {
                option.id for option in getattr(project.roles, role).receiver_options
            }
            if receiver_id not in option_ids:
                raise OffboardingFailure(
                    "receiver_ineligible",
                    409,
                    {
                        "code": "receiver_ineligible",
                        "project_id": str(project.project_id),
                        "role": role,
                        "receiver_id": str(receiver_id),
                    },
                )


def _build_plans(
    facts: OffboardingSnapshotFacts,
    preview: OffboardingPreviewRecord,
    request: CommitOffboardingRequest,
    project_requests: dict[uuid.UUID, object],
) -> tuple[
    list[OffboardingTransfer],
    list[OffboardingUnresolved],
    list[OffboardingOwnerPlan],
    list[OffboardingLabelingPlan],
]:
    target_id = facts.target.id
    preview_by_id = {project.project_id: project for project in preview.projects}
    tasks_by_project: dict[uuid.UUID, list] = defaultdict(list)
    batches_by_project: dict[uuid.UUID, list[OffboardingBatchFact]] = defaultdict(list)
    locks_by_task: dict[uuid.UUID, int] = defaultdict(int)
    for task in facts.tasks:
        if task.project_id is not None:
            tasks_by_project[task.project_id].append(task)
    for batch in facts.batches:
        batches_by_project[batch.project_id].append(batch)
    for lock in facts.locks:
        locks_by_task[lock.task_id] += 1

    transfers: list[OffboardingTransfer] = []
    unresolved: list[OffboardingUnresolved] = []
    owner_plan: list[OffboardingOwnerPlan] = []
    labeling_plan: list[OffboardingLabelingPlan] = []

    for project in facts.projects:
        project_preview = preview_by_id[project.id]
        mapping = project_requests.get(project.id)
        for role in _ROLE_ORDER:
            if not _role_present(project_preview, role):
                continue
            if request.payload.mode == "emergency_suspend":
                if role == "owner":
                    batch_ids: list[uuid.UUID] = []
                    task_count = lock_count = 0
                else:
                    related_tasks = [
                        task
                        for task in tasks_by_project[project.id]
                        if task.status in _HANDOFF_STATUSES
                        and (
                            task.assignee_id == target_id
                            or task.reviewer_id == target_id
                        )
                    ]
                    batch_ids = [batch.id for batch in batches_by_project[project.id]]
                    task_count = sum(
                        1
                        for task in tasks_by_project[project.id]
                        if task.status in _HANDOFF_STATUSES
                        and (
                            task.assignee_id == target_id
                            if role == "annotator"
                            else task.reviewer_id == target_id
                        )
                    )
                    lock_count = sum(locks_by_task[task.id] for task in related_tasks)
                unresolved.append(
                    OffboardingUnresolved(
                        project_id=project.id,
                        role=role,
                        reason="emergency_suspension_requires_later_handoff",
                        batch_ids=batch_ids,
                        task_count=task_count,
                        lock_count=lock_count,
                    )
                )
                continue

            receiver_id = getattr(mapping, f"{role}_receiver_id") if mapping else None
            if receiver_id is None:
                raise IdentityUnavailable()
            if role == "owner":
                if project.owner_id != target_id:
                    raise IdentityUnavailable()
                transfers.append(
                    OffboardingTransfer(
                        project_id=project.id,
                        role=role,
                        receiver_id=receiver_id,
                        batch_ids=[],
                        task_count=0,
                        lock_count=0,
                    )
                )
                owner_plan.append(
                    OffboardingOwnerPlan(
                        project_id=project.id,
                        expected_owner_id=target_id,
                        receiver_id=receiver_id,
                    )
                )
                continue

            if role == "annotator":
                field = "annotator_id"
                task_field = "assignee_id"
            else:
                field = "reviewer_id"
                task_field = "reviewer_id"
            batches = [
                batch
                for batch in batches_by_project[project.id]
                if getattr(batch, field) == target_id
            ]
            tasks = [
                task
                for task in tasks_by_project[project.id]
                if task.status in _HANDOFF_STATUSES
                and getattr(task, task_field) == target_id
            ]
            batch_ids = [batch.id for batch in batches]
            task_ids = [task.id for task in tasks]
            lock_count = sum(locks_by_task[task.id] for task in tasks)
            transfers.append(
                OffboardingTransfer(
                    project_id=project.id,
                    role=role,
                    receiver_id=receiver_id,
                    batch_ids=batch_ids,
                    task_count=len(tasks),
                    lock_count=lock_count,
                )
            )
            labeling_plan.append(
                OffboardingLabelingPlan(
                    project_id=project.id,
                    role=role,
                    receiver_id=receiver_id,
                    batch_ids=batch_ids,
                    task_ids=task_ids,
                )
            )
    return transfers, unresolved, owner_plan, labeling_plan


def _scope_basis(facts: OffboardingSnapshotFacts) -> OffboardingScopeBasis:
    return OffboardingScopeBasis(
        target_role=facts.target.role,
        target_was_active=facts.target.is_active,
        project_ids=[project.id for project in facts.projects],
    )


def _assert_postimage(
    facts: OffboardingSnapshotFacts,
    result: OffboardingResultRecord,
    principal: Principal,
    request: CommitOffboardingRequest,
    expected_transfers: list[OffboardingTransfer],
    expected_unresolved: list[OffboardingUnresolved],
) -> None:
    normalized_reason, _ = offboarding_reason_values(request.payload.reason)
    expected_kind = (
        "emergency_suspended"
        if request.payload.mode == "emergency_suspend"
        else "suspended"
    )
    expected_user = facts.target.model_copy(
        update={
            "status": "offline",
            "is_active": False,
            "disabled_kind": expected_kind,
            "disabled_at": result.user.disabled_at,
            "disabled_by": principal.actor_id,
            "disabled_reason": normalized_reason,
        }
    )
    expected_revoked = sorted(
        key.id for key in facts.api_keys if key.revoked_at is None
    )
    if (
        result.user != expected_user
        or result.transfers != expected_transfers
        or result.unresolved != expected_unresolved
        or result.revoked_api_key_ids != expected_revoked
        or result.audit_id < 1
    ):
        raise IdentityUnavailable()


def _success_payload(request, principal: Principal, data: dict) -> dict:
    return {
        "schema": OFFBOARDING_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": data,
    }


class OffboardingManager:
    def __init__(self, auth: BoundAuthenticator, data: OffboardingData, secret: str):
        self.auth = auth
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(
        request: OffboardingRequest,
        verified: VerifiedCredential,
        failure: OffboardingFailure,
    ) -> None:
        try:
            decode_offboarding_reply(
                request,
                offboarding_failure_reply(failure),
                expected_principal=verified.principal,
            )
        except OffboardingFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: OffboardingRequest):
        try:
            verified = await self.auth.authenticate_bound(request.token)
        except IdentityFailure as error:
            status = 400 if error.code == "invalid_request" else None
            raise OffboardingFailure(error.code, status, None) from None

        fingerprint = offboarding_request_fingerprint(self.secret, request)
        prepare_command = native_prepare_offboarding_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=fingerprint,
            derivation_id=self.derivation_id,
        )
        prepare_payload = prepare_command.model_dump(mode="json", by_alias=True)
        try:
            json.dumps(prepare_payload, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, UnicodeError):
            raise IdentityUnavailable() from None

        async with self.data.transaction() as transaction:
            try:
                raw_prepare = await transaction.prepare_offboarding_command(
                    prepare_payload
                )
                prepared = decode_native_offboarding_reply(prepare_command, raw_prepare)
            except OffboardingFailure as failure:
                self._raise_checked_failure(request, verified, failure)

            if isinstance(prepared, NativeOffboardingPrepareSuccess):
                if isinstance(prepared.data, OffboardingPrepareReplayData):
                    if not isinstance(request, CommitOffboardingRequest):
                        raise IdentityUnavailable()
                    _assert_replay_scope(prepared.principal, prepared.data)
                    self._raise_checked_result_binding(request, prepared)
                    payload = _success_payload(
                        request,
                        prepared.principal,
                        {"result": prepared.data.result.model_dump(mode="json")},
                    )
                    return decode_offboarding_reply(
                        request, payload, expected_principal=prepared.principal
                    )
                if not isinstance(prepared.data, OffboardingPrepareSnapshotData):
                    raise IdentityUnavailable()
                facts = prepared.data.facts
                actor = prepared.principal
                if isinstance(request, PreviewOffboardingRequest):
                    _assert_current_scope(actor, facts)
                    preview = build_offboarding_preview(facts)
                    payload = _success_payload(
                        request,
                        actor,
                        {"preview": preview.model_dump(mode="json")},
                    )
                    return decode_offboarding_reply(
                        request, payload, expected_principal=actor
                    )
                if not isinstance(request, CommitOffboardingRequest):
                    raise IdentityUnavailable()
                return await self._commit_snapshot(
                    request=request,
                    verified=verified,
                    prepared=prepared,
                    facts=facts,
                    transaction=transaction,
                )
            raise IdentityUnavailable()

    @staticmethod
    def _raise_checked_result_binding(
        request: CommitOffboardingRequest,
        prepared: NativeOffboardingPrepareSuccess,
    ) -> None:
        if not isinstance(prepared.data, OffboardingPrepareReplayData):
            raise IdentityUnavailable()
        result = prepared.data.result
        try:
            payload = _success_payload(
                request,
                prepared.principal,
                {"result": result.model_dump(mode="json")},
            )
            decode_offboarding_reply(
                request, payload, expected_principal=prepared.principal
            )
        except OffboardingFailure:
            raise IdentityUnavailable() from None

    async def _commit_snapshot(
        self,
        *,
        request: CommitOffboardingRequest,
        verified: VerifiedCredential,
        prepared: NativeOffboardingPrepareSuccess,
        facts: OffboardingSnapshotFacts,
        transaction: OffboardingTransaction,
    ) -> CommitOffboardingReply:
        actor = prepared.principal
        # Scope is checked before the preview token, matching the legacy error order.
        _assert_current_scope(actor, facts)
        preview = build_offboarding_preview(facts)
        if request.payload.preview_version != preview.preview_version:
            from .offboarding_contract import PreviewStaleDetail

            raise OffboardingFailure(
                "offboarding_preview_stale",
                409,
                PreviewStaleDetail(
                    code="offboarding_preview_stale",
                    expected_preview_version=request.payload.preview_version,
                    current_preview_version=preview.preview_version,
                    blockers=preview.blockers,
                ),
            )
        target = facts.target
        if not target.is_active and target.disabled_kind in _HISTORICAL_KINDS:
            raise OffboardingFailure(
                "historical_target", *_DETAILS["historical_target"]
            )
        if not target.is_active and target.disabled_kind not in _REACTIVATABLE_KINDS:
            raise OffboardingFailure("state_forbidden", *_DETAILS["state_forbidden"])

        project_requests = _request_map(request)
        if request.payload.mode == "handoff":
            _validate_receivers(request, preview, project_requests)
            if preview.blockers:
                raise OffboardingFailure(
                    "offboarding_blocked",
                    409,
                    {
                        "code": "offboarding_blocked",
                        "blockers": [
                            blocker.model_dump(mode="json")
                            for blocker in preview.blockers
                        ],
                    },
                )

        transfers, unresolved, owner_plan, labeling_plan = _build_plans(
            facts, preview, request, project_requests
        )
        normalized_reason, reason_present = offboarding_reason_values(
            request.payload.reason
        )
        apply_payload = OffboardingApplyPayload(
            transaction_id=prepared.data.transaction_id,
            target_user_id=target.id,
            expected_target_generation=prepared.data.target_generation,
            expected_target_is_active=prepared.data.target_is_active,
            mode=request.payload.mode,
            normalized_reason=normalized_reason,
            reason_present=reason_present,
            owner_plan=owner_plan,
            labeling_plan=labeling_plan,
            transfers=transfers,
            unresolved=unresolved,
            scope_basis=_scope_basis(facts),
        )
        apply_command = native_apply_offboarding_command(
            request,
            actor=actor,
            expected_key_hash=verified.key_hash,
            request_fingerprint=offboarding_request_fingerprint(self.secret, request),
            derivation_id=self.derivation_id,
            payload=apply_payload,
        )
        apply_json = apply_command.model_dump(mode="json", by_alias=True)
        try:
            json.dumps(apply_json, ensure_ascii=True, allow_nan=False)
            raw_result = await transaction.apply_offboarding_command(apply_json)
            native = decode_native_offboarding_reply(apply_command, raw_result)
        except OffboardingFailure as failure:
            self._raise_checked_failure(request, verified, failure)
        if not isinstance(native, NativeOffboardingApplySuccess):
            raise IdentityUnavailable()
        result = native.data.result
        _assert_postimage(
            facts,
            result,
            native.principal,
            request,
            transfers,
            unresolved,
        )
        payload = _success_payload(
            request,
            native.principal,
            {"result": result.model_dump(mode="json")},
        )
        return decode_offboarding_reply(
            request, payload, expected_principal=verified.principal
        )
