"""Portable member lifecycle policy and legacy preview snapshot generation."""

from __future__ import annotations

import uuid
from typing import Any

from aap_workspace_contract.access import (
    ANNOTATOR_ROLE,
    PROJECT_ROLES,
    REVIEWER_ROLE,
    VIEWER_ROLE,
    membership_role_compatible,
)
from .contract import (
    MembershipAccountFacts,
    MembershipError,
    MembershipPreview,
    MembershipPrepareFacts,
    MembershipReceiverFacts,
    MembershipRemovedData,
    MembershipRequest,
    MembershipSuccess,
    membership_snapshot_token,
    membership_success_reply,
)

UNFINISHED_ANNOTATION_STATUSES = ("pending", "in_progress", "rejected")
UNFINISHED_REVIEW_STATUSES = ("review",)
_REMOVAL_IGNORED_BLOCKER_CODES = frozenset({"same_role", "target_role_incompatible"})

_BLOCKER_LABELS = {
    "same_role": "目标职责与当前职责相同",
    "target_role_incompatible": "账号平台角色与目标项目职责不兼容",
    "unfinished_annotation_work": "成员仍有未完成的标注工作，需显式指派接收人",
    "unfinished_review_work": "成员仍有未完成的审核工作，需显式指派接收人",
    "active_locks": "成员持有效工作锁，需在交接中释放后才能变更职责",
    "reviewer_coverage_lost": "变更后将没有可用的质检员接收剩余审核工作",
    "invalid_replacement_annotator": "标注接收人必须是该项目中启用且职责匹配的员工",
    "invalid_replacement_reviewer": "质检接收人必须是该项目中启用且职责匹配的员工",
    "identical_replacement_responsibility": "标注接收人与质检接收人不能是同一账号",
    "self_replacement": "接收人不能是正在变更职责的成员本人",
    "replacement_annotator_reviewer_conflict": "标注接收人已是相关任务的质检员，会造成自审冲突",
    "replacement_reviewer_is_annotator": "质检接收人已是相关任务的标注员，会造成自审冲突",
    "replacement_reviewer_contributor": "质检接收人已参与相关任务的标注，不能审核自己的产出",
    "review_contributors_unknown": "相关任务缺少可信的标注贡献者证据，不能为该任务指派质检接收人",
}

_INVALID_MEMBER_ROLE_DETAIL = (
    "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，"
    "平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
)


def snapshot_token(snapshot: dict[str, Any]) -> str:
    """Compute the legacy digest with Python's original JSON defaults."""

    return membership_snapshot_token(snapshot)


def _blocker(code: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"code": code, "message": _BLOCKER_LABELS.get(code, code)}
    entry.update(extra)
    return entry


def _sorted_strings(values) -> list[str]:
    return sorted(str(value) for value in values)


def _index_accounts(
    facts: MembershipPrepareFacts,
) -> dict[uuid.UUID, MembershipAccountFacts]:
    return {account.id: account for account in facts.accounts}


def _index_memberships(
    facts: MembershipPrepareFacts,
) -> dict[tuple[uuid.UUID, uuid.UUID, str], MembershipReceiverFacts]:
    return {
        (membership.project_id, membership.user_id, membership.role): membership
        for membership in facts.receiver_memberships
    }


def _receiver_snapshot(
    *,
    project_id: uuid.UUID,
    replacement_id: uuid.UUID | None,
    project_role: str,
    accounts: dict[uuid.UUID, MembershipAccountFacts],
    memberships: dict[tuple[uuid.UUID, uuid.UUID, str], MembershipReceiverFacts],
) -> dict[str, Any] | None:
    if replacement_id is None:
        return None
    user_id = str(replacement_id)
    user = accounts.get(replacement_id)
    if user is None:
        return {"user_id": user_id, "exists": False}
    membership = memberships.get((project_id, replacement_id, project_role))
    return {
        "user_id": user_id,
        "exists": True,
        "is_active": bool(user.is_active),
        "platform_role": user.role,
        "membership_id": str(membership.id) if membership else None,
        "membership_version": membership.version if membership else None,
        "membership_role": membership.role if membership else None,
    }


def _task_snapshot(task) -> dict[str, Any]:
    annotation_contributors = task.annotation_contributor_ids
    review_contributors = task.review_contributor_ids
    return {
        "status": task.status,
        "version": task.version,
        "batch_id": str(task.batch_id) if task.batch_id else None,
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        "assignee_is_override": bool(task.assignee_is_override),
        "reviewer_id": str(task.reviewer_id) if task.reviewer_id else None,
        "reviewer_is_override": bool(task.reviewer_is_override),
        "reviewer_claimed": bool(task.reviewer_claimed),
        "review_round_id": str(task.review_round_id) if task.review_round_id else None,
        "effective_assignee_id": (
            str(task.assignee_id)
            if task.assignee_id
            else str(task.batch_annotator_id)
            if task.batch_annotator_id
            else None
        ),
        "effective_reviewer_id": (
            str(task.reviewer_id)
            if task.reviewer_id
            else str(task.batch_reviewer_id)
            if task.batch_reviewer_id
            else None
        ),
        "review_submitter_id": (
            str(task.review_submitter_id) if task.review_submitter_id else None
        ),
        "annotation_contributors": (
            sorted(str(value) for value in annotation_contributors)
            if annotation_contributors is not None
            else None
        ),
        "review_contributors": (
            sorted(str(value) for value in review_contributors)
            if review_contributors is not None
            else None
        ),
    }


def build_member_resource_snapshot(
    request: MembershipRequest, facts: MembershipPrepareFacts
) -> dict[str, Any]:
    """Build the exact old snapshot from already scoped typed owner facts."""

    member = facts.member
    labeling = facts.labeling
    if member is None or labeling is None:
        raise ValueError("Member and Labeling facts are required for a preview")
    accounts = _index_accounts(facts)
    memberships = _index_memberships(facts)
    member_account = accounts.get(member.user_id)
    payload = request.payload
    replacement_annotator_id = getattr(payload, "replacement_annotator_id", None)
    replacement_reviewer_id = getattr(payload, "replacement_reviewer_id", None)
    excluded_reviewers = {member.user_id, replacement_reviewer_id}
    eligible_reviewer_ids = _sorted_strings(
        reviewer_id
        for reviewer_id in facts.eligible_reviewer_ids
        if reviewer_id not in excluded_reviewers
    )

    batch_ids = set(labeling.batch_annotator_ids) | set(labeling.batch_reviewer_ids)
    batches_by_id = {
        batch.id: batch for batch in labeling.batches if batch.id in batch_ids
    }
    tasks_by_id = {task.id: task for task in labeling.tasks}

    return {
        "member": {
            "id": str(member.id),
            "user_id": str(member.user_id),
            "role": member.role,
            "version": member.version,
        },
        "member_account": {
            "exists": member_account is not None,
            "is_active": bool(member_account.is_active) if member_account else False,
            "platform_role": member_account.role if member_account else None,
        },
        "project": {
            "id": str(facts.project.id),
            "owner_id": str(facts.project.owner_id),
        },
        "annotation_task_ids": _sorted_strings(labeling.annotation_task_ids),
        "review_task_ids": _sorted_strings(labeling.review_task_ids),
        "unassigned_review_task_ids": _sorted_strings(
            labeling.unassigned_review_task_ids
        ),
        "lock_task_ids": _sorted_strings(labeling.lock_task_ids),
        "review_claim_task_ids": _sorted_strings(labeling.review_claim_task_ids),
        "materialization_annotation_task_ids": _sorted_strings(
            labeling.materialization_annotation_task_ids
        ),
        "materialization_review_task_ids": _sorted_strings(
            labeling.materialization_review_task_ids
        ),
        "batch_annotator_ids": _sorted_strings(labeling.batch_annotator_ids),
        "batch_reviewer_ids": _sorted_strings(labeling.batch_reviewer_ids),
        "eligible_reviewer_ids": eligible_reviewer_ids,
        "tasks": {
            str(task_id): _task_snapshot(tasks_by_id[task_id])
            for task_id in sorted(tasks_by_id, key=lambda value: value.hex)
        },
        "batches": {
            str(batch_id): {
                "status": batch.status,
                "annotator_id": str(batch.annotator_id) if batch.annotator_id else None,
                "reviewer_id": str(batch.reviewer_id) if batch.reviewer_id else None,
                "assigned_user_ids": _sorted_strings(batch.assigned_user_ids or []),
                "admin_locked": bool(batch.admin_locked),
            }
            for batch_id, batch in sorted(
                batches_by_id.items(), key=lambda item: item[0].hex
            )
        },
        "receivers": {
            "annotator": _receiver_snapshot(
                project_id=facts.project.id,
                replacement_id=replacement_annotator_id,
                project_role=ANNOTATOR_ROLE,
                accounts=accounts,
                memberships=memberships,
            ),
            "reviewer": _receiver_snapshot(
                project_id=facts.project.id,
                replacement_id=replacement_reviewer_id,
                project_role=REVIEWER_ROLE,
                accounts=accounts,
                memberships=memberships,
            ),
        },
    }


def _resolve_receiver(
    *,
    facts: MembershipPrepareFacts,
    member_user_id: uuid.UUID,
    replacement_id: uuid.UUID | None,
    project_role: str,
) -> tuple[MembershipAccountFacts | None, dict[str, Any] | None]:
    if replacement_id is None:
        return None, None
    if replacement_id == member_user_id:
        return None, _blocker("self_replacement", project_role=project_role)
    target = _index_accounts(facts).get(replacement_id)
    if target is None or not target.is_active:
        return None, _blocker(f"invalid_replacement_{project_role}")
    if not membership_role_compatible(target.role, project_role):
        return None, _blocker(f"invalid_replacement_{project_role}")
    membership = _index_memberships(facts).get(
        (facts.project.id, replacement_id, project_role)
    )
    if membership is None:
        return None, _blocker(f"invalid_replacement_{project_role}")
    return target, None


def _reviewer_evidence_blocked(
    task_state: dict[str, Any], reviewer_id: str
) -> bool | None:
    """Mirror claim-time evidence; unknown evidence falls back to membership."""

    if task_state.get("review_round_id") is None:
        return None
    accumulated = task_state.get("annotation_contributors")
    contributors = task_state.get("review_contributors")
    submitter = task_state.get("review_submitter_id")
    if accumulated is None or not contributors or not submitter:
        return None
    if submitter not in contributors or not set(accumulated).issubset(contributors):
        return None
    effective_annotator = task_state.get("effective_assignee_id")
    return (
        reviewer_id in contributors
        or reviewer_id == submitter
        or (effective_annotator is not None and reviewer_id == effective_annotator)
    )


def evaluate_role_change(
    request: MembershipRequest, facts: MembershipPrepareFacts, target_role: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the canonical resource snapshot and legacy ordered blockers."""

    member = facts.member
    labeling = facts.labeling
    if member is None or labeling is None:
        raise ValueError("Member and Labeling facts are required for role policy")
    snapshot = build_member_resource_snapshot(request, facts)
    blockers: list[dict[str, Any]] = []
    member_account = snapshot["member_account"]

    if target_role == member.role:
        blockers.append(_blocker("same_role"))
    if not membership_role_compatible(member_account["platform_role"], target_role):
        blockers.append(_blocker("target_role_incompatible"))

    payload = request.payload
    replacement_annotator_id = getattr(payload, "replacement_annotator_id", None)
    replacement_reviewer_id = getattr(payload, "replacement_reviewer_id", None)
    annotation_receiver, annotation_error = _resolve_receiver(
        facts=facts,
        member_user_id=member.user_id,
        replacement_id=replacement_annotator_id,
        project_role=ANNOTATOR_ROLE,
    )
    reviewer_receiver, reviewer_error = _resolve_receiver(
        facts=facts,
        member_user_id=member.user_id,
        replacement_id=replacement_reviewer_id,
        project_role=REVIEWER_ROLE,
    )

    if (
        replacement_annotator_id is not None
        and replacement_reviewer_id is not None
        and replacement_annotator_id == replacement_reviewer_id
    ):
        blockers.append(_blocker("identical_replacement_responsibility"))
    if annotation_error is not None:
        blockers.append(annotation_error)
    if reviewer_error is not None:
        blockers.append(reviewer_error)

    annotation_task_ids = set(labeling.annotation_task_ids)
    review_task_ids = set(labeling.review_task_ids)
    lock_task_ids = set(labeling.lock_task_ids)
    batch_annotator_ids = set(labeling.batch_annotator_ids)
    batch_reviewer_ids = set(labeling.batch_reviewer_ids)
    annotation_removed = target_role != ANNOTATOR_ROLE
    review_removed = target_role != REVIEWER_ROLE
    has_annotation_work = bool(annotation_task_ids or batch_annotator_ids)
    has_review_work = bool(review_task_ids or batch_reviewer_ids)
    annotation_handoff_ready = not annotation_removed or not has_annotation_work
    review_handoff_ready = not review_removed or not has_review_work

    if annotation_removed and has_annotation_work:
        if replacement_annotator_id is None:
            blockers.append(_blocker("unfinished_annotation_work"))
        elif annotation_error is not None:
            annotation_handoff_ready = False
        elif annotation_receiver is not None:
            conflict_ids = _sorted_strings(
                labeling.annotation_receiver_conflict_task_ids
            )
            if conflict_ids:
                blockers.append(
                    _blocker(
                        "replacement_annotator_reviewer_conflict",
                        task_ids=conflict_ids,
                    )
                )
            else:
                annotation_handoff_ready = True

    if review_removed and has_review_work:
        if replacement_reviewer_id is None:
            blockers.append(_blocker("unfinished_review_work"))
        elif reviewer_error is not None:
            review_handoff_ready = False
        elif reviewer_receiver is not None:
            unknown_ids = _sorted_strings(labeling.review_receiver_unknown_task_ids)
            annotator_ids = _sorted_strings(labeling.review_receiver_annotator_task_ids)
            contributor_ids = _sorted_strings(
                labeling.review_receiver_contributor_task_ids
            )
            if unknown_ids:
                blockers.append(
                    _blocker("review_contributors_unknown", task_ids=unknown_ids)
                )
            elif annotator_ids:
                blockers.append(
                    _blocker(
                        "replacement_reviewer_is_annotator", task_ids=annotator_ids
                    )
                )
            elif contributor_ids:
                blockers.append(
                    _blocker(
                        "replacement_reviewer_contributor", task_ids=contributor_ids
                    )
                )
            else:
                review_handoff_ready = True

    if lock_task_ids and target_role != member.role:
        annotation_lock = lock_task_ids & annotation_task_ids
        review_lock = lock_task_ids & review_task_ids
        uncovered = lock_task_ids - annotation_lock - review_lock
        annotation_locks_ok = target_role == ANNOTATOR_ROLE or annotation_handoff_ready
        review_locks_ok = target_role == REVIEWER_ROLE or review_handoff_ready
        if (
            uncovered
            or (annotation_lock and not annotation_locks_ok)
            or (review_lock and not review_locks_ok)
        ):
            blockers.append(
                _blocker("active_locks", task_ids=_sorted_strings(lock_task_ids))
            )

    if (
        member.role == REVIEWER_ROLE
        and review_removed
        and (has_review_work or labeling.unassigned_review_task_ids)
    ):
        replacement_ready = (
            reviewer_error is None and replacement_reviewer_id is not None
        )
        if not replacement_ready and not snapshot["eligible_reviewer_ids"]:
            blockers.append(_blocker("reviewer_coverage_lost"))
        elif not replacement_ready:
            uncovered_review_task_ids = [
                task_id
                for task_id in snapshot["unassigned_review_task_ids"]
                if snapshot["tasks"].get(task_id, {}).get("status") == "review"
                and all(
                    _reviewer_evidence_blocked(snapshot["tasks"][task_id], reviewer_id)
                    for reviewer_id in snapshot["eligible_reviewer_ids"]
                )
            ]
            if uncovered_review_task_ids:
                blockers.append(
                    _blocker(
                        "reviewer_coverage_lost",
                        task_ids=uncovered_review_task_ids,
                    )
                )
    return snapshot, blockers


def policy_error(
    status_code: int, detail: Any, *, rollback: bool = False
) -> MembershipError:
    return MembershipError(
        status_code=status_code,
        detail=detail,
        headers=None,
        rollback=rollback,
    )


def _project_role_error() -> MembershipError:
    return policy_error(400, "非法项目职责")


def _platform_compatibility_error() -> MembershipError:
    return policy_error(400, _INVALID_MEMBER_ROLE_DETAIL)


def validate_request_role(request: MembershipRequest) -> MembershipError | None:
    """Validate roles before native member lookups, preserving legacy ordering."""

    if request.operation == "add_member":
        if request.payload.project_role not in PROJECT_ROLES:
            return _project_role_error()
    elif request.operation in {"preview_role_change", "change_role"}:
        if request.payload.target_role not in PROJECT_ROLES:
            return _project_role_error()
    return None


def add_member_error(
    request: MembershipRequest, facts: MembershipPrepareFacts
) -> MembershipError | None:
    payload = request.payload
    target = _index_accounts(facts).get(payload.target_user_id)
    if target is None or not target.is_active:
        return policy_error(404, "目标用户不存在")
    if not membership_role_compatible(target.role, payload.project_role):
        return _platform_compatibility_error()
    return None


def preview_role_change_reply(
    request: MembershipRequest, facts: MembershipPrepareFacts
) -> MembershipSuccess:
    payload = request.payload
    snapshot, blockers = evaluate_role_change(request, facts, payload.target_role)
    preview = MembershipPreview(
        member_id=facts.member.id,
        user_id=facts.member.user_id,
        current_role=facts.member.role,
        current_version=facts.member.version,
        target_role=payload.target_role,
        requires_handoff=bool(
            [blocker for blocker in blockers if blocker["code"] != "same_role"]
        ),
        blockers=[blocker["code"] for blocker in blockers],
        blocker_details=blockers,
        resource_snapshot=snapshot,
        preview_token=snapshot_token(snapshot),
    )
    return membership_success_reply(request, preview)


def change_role_error(
    request: MembershipRequest, facts: MembershipPrepareFacts
) -> MembershipError | None:
    payload = request.payload
    member = facts.member
    if member.version != payload.expected_version:
        return policy_error(
            409,
            {"reason": "stale_member_version", "current_version": member.version},
        )
    target = _index_accounts(facts).get(member.user_id)
    if target is None or not target.is_active:
        return policy_error(409, "目标成员账号已停用，请刷新后重试")
    snapshot, blockers = evaluate_role_change(request, facts, payload.target_role)
    if snapshot_token(snapshot) != payload.preview_token:
        return policy_error(
            409,
            {
                "reason": "stale_resource_snapshot",
                "detail": "资源已变化，请重新预览",
            },
        )
    if blockers:
        return policy_error(
            409, {"reason": "member_role_change_blocked", "blockers": blockers}
        )
    return None


def remove_member_error(
    request: MembershipRequest, facts: MembershipPrepareFacts
) -> MembershipError | None:
    snapshot, blockers = evaluate_role_change(request, facts, VIEWER_ROLE)
    blockers = [
        blocker
        for blocker in blockers
        if blocker["code"] not in _REMOVAL_IGNORED_BLOCKER_CODES
    ]
    if blockers:
        return policy_error(
            409,
            {
                "reason": "member_removal_blocked",
                "blockers": blockers,
                "resource_snapshot": snapshot,
            },
        )
    return None


def removal_result(request: MembershipRequest, facts: MembershipPrepareFacts):
    return membership_success_reply(
        request,
        MembershipRemovedData(member_id=facts.member.id, removed=True),
    )
