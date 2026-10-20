"""Application workflow composing membership and labeling responsibility owners."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ProjectRole, TaskStatus
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import project_membership as membership
from app.services.project_membership import (
    PROJECT_ROLES,
    _REMOVAL_IGNORED_BLOCKER_CODES,
    UNFINISHED_ANNOTATION_STATUSES,
    UNFINISHED_REVIEW_STATUSES,
    _acquire_task_advisory_nowait,
    _active_batch_ids,
    _active_lock_task_ids,
    _annotation_task_ids,
    _assert_actor_can_manage,
    _inherits_annotation_default,
    _inherits_review_default,
    _lock_accounts_nowait,
    _lock_batch_ids_nowait,
    _lock_live_task_locks_nowait,
    _lock_membership_nowait,
    _lock_project_nowait,
    _lock_replacement_memberships_nowait,
    _lock_task_ids_nowait,
    _materialization_task_ids,
    _review_task_ids,
    _snapshot_token,
    evaluate_role_change,
)
from app.services.audit import AuditAction, AuditService
from app.services.batch import BatchService
from app.services.identity_contracts import IdentityActorFacts


async def change_role(
    db: AsyncSession,
    *,
    project: Project,
    actor: User | IdentityActorFacts,
    member_id: uuid.UUID,
    target_role: str,
    expected_version: int,
    preview_token: str,
    reason: str,
    replacement_annotator_id: uuid.UUID | None,
    replacement_reviewer_id: uuid.UUID | None,
) -> ProjectMember:
    """Apply a role change with CAS, resource-snapshot revalidation and handoff.

    Acquisition order (all nonblocking): accounts -> project -> target/receiver
    memberships -> affected batches -> affected tasks -> per-task advisory locks
    -> task locks.  The snapshot is rebuilt only after every lock is held, so a
    preview token can never be applied against newer state.
    """

    if target_role not in PROJECT_ROLES:
        raise HTTPException(status_code=400, detail="非法项目职责")

    pre_member = await db.scalar(
        select(ProjectMember)
        .where(
            ProjectMember.id == member_id,
            ProjectMember.project_id == project.id,
        )
        .execution_options(populate_existing=True)
    )
    if pre_member is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    accounts = await _lock_accounts_nowait(
        db,
        [
            actor.id,
            pre_member.user_id,
            replacement_annotator_id,
            replacement_reviewer_id,
        ],
    )
    locked_actor = accounts.get(actor.id)
    locked_project = await _lock_project_nowait(db, project.id)
    # Use only the freshly locked account; never fall back to the caller's
    # possibly stale ORM instance.
    await _assert_actor_can_manage(actor=locked_actor, project=locked_project)

    member = await _lock_membership_nowait(
        db, project_id=locked_project.id, member_id=member_id
    )
    if member.version != expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "stale_member_version",
                "current_version": member.version,
            },
        )

    target_user = accounts.get(member.user_id)
    if target_user is None or not target_user.is_active:
        raise HTTPException(status_code=409, detail="目标成员账号已停用，请刷新后重试")

    await _lock_replacement_memberships_nowait(
        db,
        project_id=locked_project.id,
        user_ids=[replacement_annotator_id, replacement_reviewer_id],
    )
    pre_annotation_ids = await _annotation_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    pre_review_ids = await _review_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    pre_lock_ids = await _active_lock_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    pre_batch_annotator_ids = await _active_batch_ids(
        db,
        project_id=locked_project.id,
        column=TaskBatch.annotator_id,
        statuses=UNFINISHED_ANNOTATION_STATUSES,
        inherit_filter=_inherits_annotation_default(member.user_id),
        user_id=member.user_id,
    )
    pre_batch_reviewer_ids = await _active_batch_ids(
        db,
        project_id=locked_project.id,
        column=TaskBatch.reviewer_id,
        statuses=(*UNFINISHED_ANNOTATION_STATUSES, *UNFINISHED_REVIEW_STATUSES),
        inherit_filter=_inherits_review_default(member.user_id),
        user_id=member.user_id,
    )
    # Bound locks to the actual unfinished dependencies and live locks instead
    # of every historical task/batch the account ever touched.
    await _lock_batch_ids_nowait(
        db,
        project_id=locked_project.id,
        batch_ids=[*pre_batch_annotator_ids, *pre_batch_reviewer_ids],
    )
    (
        materialization_annotation_ids,
        materialization_review_ids,
    ) = await _materialization_task_ids(
        db,
        project_id=locked_project.id,
        batch_annotator_ids=pre_batch_annotator_ids,
        batch_reviewer_ids=pre_batch_reviewer_ids,
    )
    affected_task_ids = [*pre_annotation_ids, *pre_review_ids, *pre_lock_ids]
    # Prelock the exact historical rows a batch-default change would stamp for
    # the responsibility being removed; unrelated completed non-NULL history
    # and the retained responsibility's history are intentionally excluded.
    materialization_ids: list[uuid.UUID] = []
    if target_role != ProjectRole.ANNOTATOR.value:
        materialization_ids.extend(materialization_annotation_ids)
    if target_role != ProjectRole.REVIEWER.value:
        materialization_ids.extend(materialization_review_ids)
    await _lock_task_ids_nowait(
        db,
        project_id=locked_project.id,
        task_ids=[*affected_task_ids, *materialization_ids],
    )
    await _acquire_task_advisory_nowait(db, task_ids=affected_task_ids)
    await _lock_live_task_locks_nowait(
        db, project_id=locked_project.id, user_id=member.user_id
    )

    # Recheck the membership version and rebuild the canonical snapshot under
    # the locks; a stale preview token is a conflict, not a silent apply.
    member = await _lock_membership_nowait(
        db, project_id=locked_project.id, member_id=member_id
    )
    if member.version != expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "stale_member_version",
                "current_version": member.version,
            },
        )

    snapshot, blockers = await evaluate_role_change(
        db,
        project=project,
        member=member,
        target_role=target_role,
        replacement_annotator_id=replacement_annotator_id,
        replacement_reviewer_id=replacement_reviewer_id,
        fresh_project=locked_project,
    )
    if _snapshot_token(snapshot) != preview_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "stale_resource_snapshot",
                "detail": "资源已变化，请重新预览",
            },
        )
    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "member_role_change_blocked",
                "blockers": blockers,
            },
        )

    labeling = BatchService(db)
    annotation_removed = target_role != ProjectRole.ANNOTATOR.value
    review_removed = target_role != ProjectRole.REVIEWER.value
    if annotation_removed and (
        snapshot["annotation_task_ids"] or snapshot["batch_annotator_ids"]
    ):
        if replacement_annotator_id is not None:
            annotation_task_ids = await _annotation_task_ids(
                db, project_id=locked_project.id, user_id=member.user_id
            )
            await labeling.handoff_project_member_annotation(
                task_ids=annotation_task_ids,
                batch_ids=snapshot["batch_annotator_ids"],
                from_user_id=member.user_id,
                to_user_id=replacement_annotator_id,
                unfinished_statuses=UNFINISHED_ANNOTATION_STATUSES,
            )
    if review_removed and (
        snapshot["review_task_ids"]
        or snapshot["review_claim_task_ids"]
        or snapshot["batch_reviewer_ids"]
    ):
        if replacement_reviewer_id is not None:
            review_task_ids = await _review_task_ids(
                db, project_id=locked_project.id, user_id=member.user_id
            )
            await labeling.handoff_project_member_reviewer(
                task_ids=review_task_ids,
                batch_ids=snapshot["batch_reviewer_ids"],
                from_user_id=member.user_id,
                to_user_id=replacement_reviewer_id,
                completed_status=TaskStatus.COMPLETED.value,
            )
    active_lock_task_ids = await _active_lock_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    released = await labeling.release_project_task_locks(
        user_id=member.user_id, task_ids=active_lock_task_ids
    )

    old_role = membership.update_member_role(member, target_role)

    await AuditService.log(
        db,
        actor=locked_actor,
        action=AuditAction.PROJECT_MEMBER_ROLE_CHANGE,
        target_type="project_member",
        target_id=str(member.id),
        detail={
            "project_id": str(locked_project.id),
            "user_id": str(member.user_id),
            "from_role": old_role,
            "to_role": target_role,
            "version": member.version,
            "reason": reason,
            "replacement_annotator_id": (
                str(replacement_annotator_id) if replacement_annotator_id else None
            ),
            "replacement_reviewer_id": (
                str(replacement_reviewer_id) if replacement_reviewer_id else None
            ),
            "released_lock_count": released,
        },
    )
    await db.commit()
    await db.refresh(member)
    return member


async def remove_member(
    db: AsyncSession,
    *,
    project: Project,
    actor: User | IdentityActorFacts,
    member_id: uuid.UUID,
) -> None:
    """Remove an *idle* member after the same blockers used by role change.

    An idle member is removed directly; outstanding work, active locks or loss
    of reviewer coverage returns a 409 asking the caller to hand off and retry.
    Assignments are never silently cleared.
    """

    pre_member = await db.scalar(
        select(ProjectMember)
        .where(
            ProjectMember.id == member_id,
            ProjectMember.project_id == project.id,
        )
        .execution_options(populate_existing=True)
    )
    if pre_member is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    accounts = await _lock_accounts_nowait(db, [actor.id, pre_member.user_id])
    locked_actor = accounts.get(actor.id)
    locked_project = await _lock_project_nowait(db, project.id)
    await _assert_actor_can_manage(actor=locked_actor, project=locked_project)

    member = await _lock_membership_nowait(
        db, project_id=locked_project.id, member_id=member_id
    )
    pre_annotation_ids = await _annotation_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    pre_review_ids = await _review_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    pre_lock_ids = await _active_lock_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    pre_batch_annotator_ids = await _active_batch_ids(
        db,
        project_id=locked_project.id,
        column=TaskBatch.annotator_id,
        statuses=UNFINISHED_ANNOTATION_STATUSES,
        inherit_filter=_inherits_annotation_default(member.user_id),
        user_id=member.user_id,
    )
    pre_batch_reviewer_ids = await _active_batch_ids(
        db,
        project_id=locked_project.id,
        column=TaskBatch.reviewer_id,
        statuses=(*UNFINISHED_ANNOTATION_STATUSES, *UNFINISHED_REVIEW_STATUSES),
        inherit_filter=_inherits_review_default(member.user_id),
        user_id=member.user_id,
    )
    await _lock_batch_ids_nowait(
        db,
        project_id=locked_project.id,
        batch_ids=[*pre_batch_annotator_ids, *pre_batch_reviewer_ids],
    )
    affected_task_ids = [*pre_annotation_ids, *pre_review_ids, *pre_lock_ids]
    await _lock_task_ids_nowait(
        db, project_id=locked_project.id, task_ids=affected_task_ids
    )
    await _acquire_task_advisory_nowait(db, task_ids=affected_task_ids)
    await _lock_live_task_locks_nowait(
        db, project_id=locked_project.id, user_id=member.user_id
    )

    member = await _lock_membership_nowait(
        db, project_id=locked_project.id, member_id=member_id
    )
    snapshot, blockers = await evaluate_role_change(
        db,
        project=project,
        member=member,
        target_role=ProjectRole.VIEWER.value,
        replacement_annotator_id=None,
        replacement_reviewer_id=None,
        fresh_project=locked_project,
    )
    blockers = [
        entry
        for entry in blockers
        if entry["code"] not in _REMOVAL_IGNORED_BLOCKER_CODES
    ]
    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "member_removal_blocked",
                "blockers": blockers,
                "resource_snapshot": snapshot,
            },
        )

    active_lock_task_ids = await _active_lock_task_ids(
        db, project_id=locked_project.id, user_id=member.user_id
    )
    await BatchService(db).release_project_task_locks(
        user_id=member.user_id, task_ids=active_lock_task_ids
    )
    await AuditService.log(
        db,
        actor=locked_actor,
        action=AuditAction.PROJECT_MEMBER_REMOVE,
        target_type="project_member",
        target_id=str(member.id),
        detail={
            "project_id": str(locked_project.id),
            "user_id": str(member.user_id),
            "project_role": member.role,
        },
    )
    await membership.delete_member_row(db, member)
    await db.commit()
