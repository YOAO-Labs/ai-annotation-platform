"""Project membership mutations: add, role change (preview/CAS/handoff), remove.

This module is the single owner of ``project_members`` mutation introduced by
the project-scoped employee-roles plan (Increment B1).  It reuses the existing
effective-assignment helpers and the repository's nonblocking lock policy:

* accounts are locked first, in stable ID order, with ``FOR UPDATE NOWAIT`` so a
  concurrent lifecycle/offboarding mutation either serializes or resolves to a
  retryable ``409``;
* the current project row, the target membership (``FOR UPDATE NOWAIT``), the
  replacement memberships (``FOR SHARE NOWAIT``), the affected batches, task
  rows, per-task advisory locks and task locks are then acquired in the same
  order the lifecycle handoff uses (task rows before the per-task advisory lock);
* every resource query is filtered to the *one* project, so a mutation never
  clears another project's locks, claims or assignments;
* a busy lock rolls the transaction back to a retryable ``409`` instead of
  leaving a partial handoff behind.

The write path rebuilds the same canonical snapshot preview produced and
compares an opaque token, so new assignments, receiver role/version/activity
changes or a project-ownership transfer after the preview conflict instead of
silently applying against stale state.

Nothing here performs whole-account offboarding, credential retirement or
cross-project work handoff; those remain owned by
``services/user_lifecycle.py``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import (
    PROJECT_ROLES,
    PlatformRole,
    ProjectRole,
    TaskStatus,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.services.audit import AuditAction, AuditService
from app.services.project_access import (
    assert_membership_role_compatible,
    is_privileged_for_project,
    membership_role_compatible,
)
from app.services.scheduler import (
    effective_task_assignee_expr,
    effective_task_reviewer_expr,
)
from app.services.user_lifecycle import UserLifecycleService

_BUSY_SQLSTATES = {"55P03", "40P01", "40001"}

#: Annotation work that has not reached review/completion.
UNFINISHED_ANNOTATION_STATUSES = (
    TaskStatus.PENDING.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.REJECTED.value,
)
#: Review work still waiting on the reviewer.
UNFINISHED_REVIEW_STATUSES = (TaskStatus.REVIEW.value,)

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
    "replacement_annotator_reviewer_conflict": (
        "标注接收人已是相关任务的质检员，会造成自审冲突"
    ),
    "replacement_reviewer_is_annotator": (
        "质检接收人已是相关任务的标注员，会造成自审冲突"
    ),
    "replacement_reviewer_contributor": (
        "质检接收人已参与相关任务的标注，不能审核自己的产出"
    ),
    "review_contributors_unknown": (
        "相关任务缺少可信的标注贡献者证据，不能为该任务指派质检接收人"
    ),
}


def _busy_db_error(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) in _BUSY_SQLSTATES


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_token(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _blocker(code: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"code": code, "message": _BLOCKER_LABELS.get(code, code)}
    entry.update(extra)
    return entry


async def _conflict_rollback(
    db: AsyncSession, exc: DBAPIError, *, reason: str, message: str
) -> None:
    """Roll the whole transaction back and surface a retryable 409 on contention."""

    await db.rollback()
    if _busy_db_error(exc):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": reason, "detail": message},
        ) from exc
    raise exc


# ---------------------------------------------------------------------------
# Nonblocking lock acquisition
# ---------------------------------------------------------------------------


async def _lock_accounts_nowait(
    db: AsyncSession, user_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, User]:
    """Reuse the lifecycle account lock so every writer shares one lock order."""

    ids = sorted({uid for uid in user_ids if uid is not None})
    if not ids:
        return {}
    return await UserLifecycleService.lock_accounts(db, ids)


async def _lock_project_nowait(db: AsyncSession, project_id: uuid.UUID) -> Project:
    try:
        row = await db.scalar(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        await _conflict_rollback(
            db,
            exc,
            reason="resource_busy",
            message="项目资源正在变更，请刷新后重试",
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return row


async def _lock_membership_nowait(
    db: AsyncSession, *, project_id: uuid.UUID, member_id: uuid.UUID
) -> ProjectMember:
    try:
        member = await db.scalar(
            select(ProjectMember)
            .where(
                ProjectMember.id == member_id,
                ProjectMember.project_id == project_id,
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        await _conflict_rollback(
            db,
            exc,
            reason="membership_busy",
            message="成员正在变更，请刷新后重试",
        )
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成员不存在")
    return member


async def _lock_replacement_memberships_nowait(
    db: AsyncSession, *, project_id: uuid.UUID, user_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, ProjectMember]:
    """``FOR SHARE`` the receiver memberships so a concurrent role change 409s."""

    ids = sorted({uid for uid in user_ids if uid is not None})
    if not ids:
        return {}
    try:
        rows = await db.scalars(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id.in_(ids),
            )
            .order_by(ProjectMember.user_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        await _conflict_rollback(
            db,
            exc,
            reason="membership_busy",
            message="接收人成员关系正在变更，请刷新后重试",
        )
    return {member.user_id: member for member in rows}


async def _lock_batch_ids_nowait(
    db: AsyncSession, *, project_id: uuid.UUID, batch_ids: Iterable[uuid.UUID]
) -> list[TaskBatch]:
    """Lock only the batches that are active dependencies of unfinished work."""

    ids = sorted({uuid.UUID(str(bid)) for bid in batch_ids})
    if not ids:
        return []
    try:
        rows = await db.scalars(
            select(TaskBatch)
            .where(TaskBatch.project_id == project_id, TaskBatch.id.in_(ids))
            .order_by(TaskBatch.id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        await _conflict_rollback(
            db,
            exc,
            reason="resource_busy",
            message="批次资源正在变更，请刷新后重试",
        )
    return list(rows)


async def _lock_task_ids_nowait(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_ids: Iterable[uuid.UUID],
) -> list[Task]:
    """Lock only the affected unfinished/locked task rows, never full history."""

    ids = sorted({uuid.UUID(str(tid)) for tid in task_ids})
    if not ids:
        return []
    try:
        rows = await db.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.id.in_(ids))
            .order_by(Task.id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        await _conflict_rollback(
            db,
            exc,
            reason="resource_busy",
            message="任务资源正在变更，请刷新后重试",
        )
    return list(rows)


async def _lock_live_task_locks_nowait(
    db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> list[TaskLock]:
    """Lock only live (unexpired) locks held by the member in this project."""

    try:
        rows = await db.scalars(
            select(TaskLock)
            .join(Task, Task.id == TaskLock.task_id)
            .where(
                Task.project_id == project_id,
                TaskLock.user_id == user_id,
                TaskLock.expire_at > _utcnow(),
            )
            .order_by(TaskLock.task_id, TaskLock.user_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        await _conflict_rollback(
            db,
            exc,
            reason="resource_busy",
            message="任务锁正在变更，请刷新后重试",
        )
    return list(rows)


async def _acquire_task_advisory_nowait(
    db: AsyncSession, *, task_ids: Iterable[uuid.UUID]
) -> None:
    """Nonblocking per-task advisory lock, after the task rows are locked."""

    for task_id in sorted({tid for tid in task_ids}):
        acquired = await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"aap:task-edit-lock:{task_id}"},
        )
        if not acquired:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason": "resource_busy",
                    "detail": "任务正在编辑，请刷新后重试",
                },
            )


# ---------------------------------------------------------------------------
# Read-only resource queries (all strictly scoped to one project)
# ---------------------------------------------------------------------------


def _inherits_annotation_default(member_user_id: uuid.UUID):
    return or_(
        Task.assignee_id.is_(None),
        and_(
            Task.assignee_id == member_user_id,
            Task.assignee_is_override.is_(False),
        ),
    )


def _inherits_review_default(member_user_id: uuid.UUID):
    return or_(
        Task.reviewer_id.is_(None),
        and_(
            Task.reviewer_id == member_user_id,
            Task.reviewer_is_override.is_(False),
        ),
    )


async def _annotation_task_ids(
    db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = await db.execute(
        select(Task.id)
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(
            Task.project_id == project_id,
            Task.status.in_(UNFINISHED_ANNOTATION_STATUSES),
            effective_task_assignee_expr() == user_id,
        )
        .order_by(Task.id)
    )
    return [task_id for (task_id,) in rows.all()]


async def _review_task_ids(
    db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = await db.execute(
        select(Task.id)
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(
            Task.project_id == project_id,
            Task.status.in_(UNFINISHED_REVIEW_STATUSES),
            effective_task_reviewer_expr() == user_id,
        )
        .order_by(Task.id)
    )
    return [task_id for (task_id,) in rows.all()]


async def _unassigned_review_task_ids(
    db: AsyncSession, *, project_id: uuid.UUID
) -> list[uuid.UUID]:
    """Review work that no eligible reviewer currently owns (open review pool)."""

    rows = await db.execute(
        select(Task.id)
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(
            Task.project_id == project_id,
            Task.status.in_(UNFINISHED_REVIEW_STATUSES),
            effective_task_reviewer_expr().is_(None),
        )
        .order_by(Task.id)
    )
    return [task_id for (task_id,) in rows.all()]


async def _claimed_review_task_ids(
    db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = await db.execute(
        select(Task.id)
        .where(
            Task.project_id == project_id,
            Task.status.in_(UNFINISHED_REVIEW_STATUSES),
            Task.reviewer_id == user_id,
            Task.reviewer_claimed_at.is_not(None),
        )
        .order_by(Task.id)
    )
    return [task_id for (task_id,) in rows.all()]


async def _active_lock_task_ids(
    db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = await db.execute(
        select(TaskLock.task_id)
        .join(Task, Task.id == TaskLock.task_id)
        .where(
            Task.project_id == project_id,
            TaskLock.user_id == user_id,
            TaskLock.expire_at > _utcnow(),
        )
        .order_by(TaskLock.task_id)
    )
    return [task_id for (task_id,) in rows.all()]


async def _active_batch_ids(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    column,
    statuses: tuple[str, ...],
    inherit_filter,
    user_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Batches whose default is *used by an unfinished task*.

    A batch default that only survives on terminal/archived work is historical
    attribution and must not be rewritten.  A batch default an unfinished task
    still inherits is an active dependency even when the task override columns
    look null.
    """

    rows = await db.execute(
        select(TaskBatch.id)
        .where(
            TaskBatch.project_id == project_id,
            column == user_id,
            select(Task.id)
            .where(
                Task.batch_id == TaskBatch.id,
                Task.status.in_(statuses),
                inherit_filter,
            )
            .exists(),
        )
        .order_by(TaskBatch.id)
    )
    return [batch_id for (batch_id,) in rows.all()]


async def _materialization_task_ids(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    batch_annotator_ids: Iterable[uuid.UUID],
    batch_reviewer_ids: Iterable[uuid.UUID],
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Exact historical rows that a batch-default change would stamp.

    These are the only past-phase tasks the handoff writes (NULL-inheriting
    rows in an active batch).  They must be prelocked with the rest of the
    affected set; unrelated completed non-NULL history stays untouched.
    """

    annotation_batches = sorted({uuid.UUID(str(bid)) for bid in batch_annotator_ids})
    review_batches = sorted({uuid.UUID(str(bid)) for bid in batch_reviewer_ids})
    annotation_targets: list[uuid.UUID] = []
    review_targets: list[uuid.UUID] = []
    if annotation_batches:
        rows = await db.execute(
            select(Task.id)
            .where(
                Task.project_id == project_id,
                Task.batch_id.in_(annotation_batches),
                Task.assignee_id.is_(None),
                Task.status.not_in(UNFINISHED_ANNOTATION_STATUSES),
            )
            .order_by(Task.id)
        )
        annotation_targets = [task_id for (task_id,) in rows.all()]
    if review_batches:
        rows = await db.execute(
            select(Task.id)
            .where(
                Task.project_id == project_id,
                Task.batch_id.in_(review_batches),
                Task.reviewer_id.is_(None),
                Task.status == TaskStatus.COMPLETED.value,
            )
            .order_by(Task.id)
        )
        review_targets = [task_id for (task_id,) in rows.all()]
    return annotation_targets, review_targets


async def _eligible_reviewer_ids(
    db: AsyncSession, *, project_id: uuid.UUID, exclude_user_ids: Iterable[uuid.UUID]
) -> list[str]:
    excluded = {uid for uid in exclude_user_ids if uid is not None}
    rows = await db.execute(
        select(ProjectMember.user_id)
        .join(User, User.id == ProjectMember.user_id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == ProjectRole.REVIEWER.value,
            User.is_active.is_(True),
            User.role == PlatformRole.EMPLOYEE.value,
        )
        .order_by(ProjectMember.user_id)
    )
    return sorted(str(user_id) for (user_id,) in rows.all() if user_id not in excluded)


async def _load_tasks_with_batch(
    db: AsyncSession, *, task_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[Task, TaskBatch | None]]:
    ids = sorted({tid for tid in task_ids})
    if not ids:
        return {}
    rows = await db.execute(
        select(Task, TaskBatch)
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(Task.id.in_(ids))
    )
    return {task.id: (task, batch) for task, batch in rows.all()}


def _task_state(task: Task, batch: TaskBatch | None) -> dict[str, Any]:
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
        "reviewer_claimed": task.reviewer_claimed_at is not None,
        "review_round_id": (
            str(task.review_round_id) if task.review_round_id else None
        ),
        "effective_assignee_id": (
            str(task.assignee_id)
            if task.assignee_id
            else (str(batch.annotator_id) if batch and batch.annotator_id else None)
        ),
        "effective_reviewer_id": (
            str(task.reviewer_id)
            if task.reviewer_id
            else (str(batch.reviewer_id) if batch and batch.reviewer_id else None)
        ),
        "review_submitter_id": (
            str(task.review_submitter_id) if task.review_submitter_id else None
        ),
        # ``None`` means the evidence is unknown (legacy); an empty list is a
        # known-empty set.  The token must distinguish the two.
        "annotation_contributors": (
            sorted(str(uid) for uid in annotation_contributors)
            if annotation_contributors is not None
            else None
        ),
        "review_contributors": (
            sorted(str(uid) for uid in review_contributors)
            if review_contributors is not None
            else None
        ),
    }


async def _receiver_state(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    replacement_id: uuid.UUID | None,
    project_role: str,
) -> dict[str, Any] | None:
    """Account + membership state for a supplied receiver.

    Included in the snapshot so a receiver role/version/activity change after
    the preview invalidates the token instead of applying stale authority.
    """

    if replacement_id is None:
        return None
    user = await db.get(User, replacement_id)
    if user is None:
        return {"user_id": str(replacement_id), "exists": False}
    membership = await db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == replacement_id,
            ProjectMember.role == project_role,
        )
    )
    return {
        "user_id": str(replacement_id),
        "exists": True,
        "is_active": bool(user.is_active),
        "platform_role": user.role,
        "membership_id": str(membership.id) if membership else None,
        "membership_version": membership.version if membership else None,
        "membership_role": membership.role if membership else None,
    }


async def build_member_resource_snapshot(
    db: AsyncSession,
    *,
    project: Project,
    member: ProjectMember,
    replacement_annotator_id: uuid.UUID | None = None,
    replacement_reviewer_id: uuid.UUID | None = None,
    fresh_project: Project | None = None,
) -> dict[str, Any]:
    """Canonical, project-local snapshot of a member's outstanding resources."""

    user_id = member.user_id
    project_row = fresh_project or project
    annotation_task_ids = await _annotation_task_ids(
        db, project_id=project_row.id, user_id=user_id
    )
    review_task_ids = await _review_task_ids(
        db, project_id=project_row.id, user_id=user_id
    )
    unassigned_review_task_ids = await _unassigned_review_task_ids(
        db, project_id=project_row.id
    )
    lock_task_ids = await _active_lock_task_ids(
        db, project_id=project_row.id, user_id=user_id
    )
    review_claim_task_ids = await _claimed_review_task_ids(
        db, project_id=project_row.id, user_id=user_id
    )
    batch_annotator_ids = await _active_batch_ids(
        db,
        project_id=project_row.id,
        column=TaskBatch.annotator_id,
        statuses=UNFINISHED_ANNOTATION_STATUSES,
        inherit_filter=_inherits_annotation_default(user_id),
        user_id=user_id,
    )
    batch_reviewer_ids = await _active_batch_ids(
        db,
        project_id=project_row.id,
        column=TaskBatch.reviewer_id,
        # A reviewer default is prospectively used by *any* unfinished task,
        # including pending/in-progress annotation work, not only tasks that
        # already entered review.
        statuses=(*UNFINISHED_ANNOTATION_STATUSES, *UNFINISHED_REVIEW_STATUSES),
        inherit_filter=_inherits_review_default(user_id),
        user_id=user_id,
    )
    eligible_reviewer_ids = await _eligible_reviewer_ids(
        db,
        project_id=project_row.id,
        exclude_user_ids=[user_id, replacement_reviewer_id],
    )
    (
        materialization_annotation_ids,
        materialization_review_ids,
    ) = await _materialization_task_ids(
        db,
        project_id=project_row.id,
        batch_annotator_ids=batch_annotator_ids,
        batch_reviewer_ids=batch_reviewer_ids,
    )
    affected_task_ids = (
        set(annotation_task_ids)
        | set(review_task_ids)
        | set(lock_task_ids)
        | set(review_claim_task_ids)
        | set(materialization_annotation_ids)
        | set(materialization_review_ids)
        # Open-pool review states ride along so coverage evaluation can check
        # frozen contributor evidence per remaining reviewer.
        | set(unassigned_review_task_ids)
    )
    tasks = await _load_tasks_with_batch(db, task_ids=affected_task_ids)
    member_user = await db.get(User, user_id, populate_existing=True)
    batches = (
        await db.scalars(
            select(TaskBatch)
            .where(TaskBatch.id.in_(set(batch_annotator_ids) | set(batch_reviewer_ids)))
            .order_by(TaskBatch.id)
        )
    ).all()
    return {
        "member": {
            "id": str(member.id),
            "user_id": str(user_id),
            "role": member.role,
            "version": member.version,
        },
        "member_account": {
            "exists": member_user is not None,
            "is_active": bool(member_user.is_active) if member_user else False,
            "platform_role": member_user.role if member_user else None,
        },
        "project": {"id": str(project_row.id), "owner_id": str(project_row.owner_id)},
        "annotation_task_ids": sorted(str(tid) for tid in annotation_task_ids),
        "review_task_ids": sorted(str(tid) for tid in review_task_ids),
        "unassigned_review_task_ids": sorted(
            str(tid) for tid in unassigned_review_task_ids
        ),
        "lock_task_ids": sorted(str(tid) for tid in lock_task_ids),
        "review_claim_task_ids": sorted(str(tid) for tid in review_claim_task_ids),
        "materialization_annotation_task_ids": sorted(
            str(tid) for tid in materialization_annotation_ids
        ),
        "materialization_review_task_ids": sorted(
            str(tid) for tid in materialization_review_ids
        ),
        "batch_annotator_ids": sorted(str(bid) for bid in batch_annotator_ids),
        "batch_reviewer_ids": sorted(str(bid) for bid in batch_reviewer_ids),
        "eligible_reviewer_ids": eligible_reviewer_ids,
        "tasks": {
            str(tid): _task_state(*tasks[tid])
            for tid in sorted(tasks, key=lambda value: value.hex)
        },
        "batches": {
            str(batch.id): {
                "status": batch.status,
                "annotator_id": str(batch.annotator_id) if batch.annotator_id else None,
                "reviewer_id": str(batch.reviewer_id) if batch.reviewer_id else None,
                "assigned_user_ids": sorted(
                    str(uid) for uid in (batch.assigned_user_ids or [])
                ),
                "admin_locked": bool(batch.admin_locked),
            }
            for batch in batches
        },
        "receivers": {
            "annotator": await _receiver_state(
                db,
                project_id=project_row.id,
                replacement_id=replacement_annotator_id,
                project_role=ProjectRole.ANNOTATOR.value,
            ),
            "reviewer": await _receiver_state(
                db,
                project_id=project_row.id,
                replacement_id=replacement_reviewer_id,
                project_role=ProjectRole.REVIEWER.value,
            ),
        },
    }


# ---------------------------------------------------------------------------
# Receiver validation and blocker evaluation
# ---------------------------------------------------------------------------


async def _resolve_receiver(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    member_user_id: uuid.UUID,
    replacement_id: uuid.UUID | None,
    project_role: str,
) -> tuple[User | None, dict[str, Any] | None]:
    """Validate a supplied handoff receiver; invalid is always reported.

    An invalid or self receiver is rejected even when the member has no counted
    work, so a caller can never smuggle an unusable receiver past the blockers.
    """

    if replacement_id is None:
        return None, None
    if replacement_id == member_user_id:
        return None, _blocker("self_replacement", project_role=project_role)
    target = await db.get(User, replacement_id)
    if target is None or not target.is_active:
        return None, _blocker(f"invalid_replacement_{project_role}")
    if not membership_role_compatible(target.role, project_role):
        return None, _blocker(f"invalid_replacement_{project_role}")
    member_row = await db.scalar(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == replacement_id,
            ProjectMember.role == project_role,
        )
    )
    if member_row is None:
        return None, _blocker(f"invalid_replacement_{project_role}")
    return target, None


async def _annotation_receiver_conflicts(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    member_user_id: uuid.UUID,
    receiver_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Reject an annotator receiver who is already the task's effective reviewer."""

    rows = await db.execute(
        select(Task.id, Task.reviewer_id, TaskBatch.reviewer_id)
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(
            Task.project_id == project_id,
            Task.status.in_(UNFINISHED_ANNOTATION_STATUSES),
            effective_task_assignee_expr() == member_user_id,
        )
        .order_by(Task.id)
    )
    conflicts: list[str] = []
    for task_id, task_reviewer, batch_reviewer in rows.all():
        if (task_reviewer or batch_reviewer) == receiver_id:
            conflicts.append(str(task_id))
    if conflicts:
        return _blocker("replacement_annotator_reviewer_conflict", task_ids=conflicts)
    return None


async def _review_receiver_conflicts(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    member_user_id: uuid.UUID,
    receiver_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Compare a reviewer receiver against each task's contributors.

    Self-review is a property of the resource, not of the two supplied IDs: the
    same receiver can be safe for one task and disqualified for another.  Frozen
    contributor evidence and an explicit effective annotator are both checked,
    and unknown (legacy) evidence is treated as unverifiable rather than empty.
    """

    rows = await db.execute(
        select(
            Task.id,
            Task.assignee_id,
            Task.review_submitter_id,
            Task.annotation_contributor_ids,
            Task.review_contributor_ids,
            TaskBatch.annotator_id,
        )
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(
            Task.project_id == project_id,
            # Reviewer responsibility covers every unfinished task that will
            # inherit the (rewritten) batch reviewer default, not only tasks
            # already in review: pending/in-progress/rejected rows with a NULL
            # reviewer inherit it the moment they are submitted.
            Task.status.in_(
                (*UNFINISHED_ANNOTATION_STATUSES, *UNFINISHED_REVIEW_STATUSES)
            ),
            effective_task_reviewer_expr() == member_user_id,
        )
        .order_by(Task.id)
    )
    annotator_conflicts: list[str] = []
    contributor_conflicts: list[str] = []
    unknown_conflicts: list[str] = []
    receiver_str = str(receiver_id)
    for (
        task_id,
        task_assignee,
        submitter_id,
        annotation_contributors,
        review_contributors,
        batch_annotator,
    ) in rows.all():
        task_id_str = str(task_id)
        if (task_assignee or batch_annotator) == receiver_id:
            annotator_conflicts.append(task_id_str)
        if submitter_id == receiver_id:
            contributor_conflicts.append(task_id_str)
            continue
        if annotation_contributors is None:
            unknown_conflicts.append(task_id_str)
            continue
        if receiver_str in {str(uid) for uid in annotation_contributors}:
            contributor_conflicts.append(task_id_str)
            continue
        if review_contributors is not None and receiver_str in {
            str(uid) for uid in review_contributors
        }:
            contributor_conflicts.append(task_id_str)
    if unknown_conflicts:
        return _blocker("review_contributors_unknown", task_ids=unknown_conflicts)
    if annotator_conflicts:
        return _blocker(
            "replacement_reviewer_is_annotator", task_ids=annotator_conflicts
        )
    if contributor_conflicts:
        return _blocker(
            "replacement_reviewer_contributor", task_ids=contributor_conflicts
        )
    return None


def _reviewer_evidence_blocked(
    task_state: dict[str, Any], reviewer_id: str
) -> bool | None:
    """Mirror ``assert_review_evidence_current`` without raising.

    Returns ``True`` when the reviewer would be denied on claim
    (``self_review_denied``), ``False`` when they are allowed, and ``None``
    when the evidence is unknown/legacy and cannot be decided here — the
    membership-based fallback then applies, as elsewhere in this module.
    """

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


async def evaluate_role_change(
    db: AsyncSession,
    *,
    project: Project,
    member: ProjectMember,
    target_role: str,
    replacement_annotator_id: uuid.UUID | None,
    replacement_reviewer_id: uuid.UUID | None,
    fresh_project: Project | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(snapshot, blocker_details)`` for a proposed role change.

    Read-only.  The same evaluation runs at preview and again under lock before
    the write, so a stale preview can never apply.
    """

    project_row = fresh_project or project
    snapshot = await build_member_resource_snapshot(
        db,
        project=project_row,
        member=member,
        replacement_annotator_id=replacement_annotator_id,
        replacement_reviewer_id=replacement_reviewer_id,
        fresh_project=project_row,
    )
    blockers: list[dict[str, Any]] = []

    if target_role == member.role:
        blockers.append(_blocker("same_role"))
    if not membership_role_compatible(
        snapshot["member_account"]["platform_role"], target_role
    ):
        # Surface the same incompatibility in preview and apply instead of a
        # 400 on the write path only.
        blockers.append(_blocker("target_role_incompatible"))

    annotation_receiver, annotation_error = await _resolve_receiver(
        db,
        project_id=project_row.id,
        member_user_id=member.user_id,
        replacement_id=replacement_annotator_id,
        project_role=ProjectRole.ANNOTATOR.value,
    )
    reviewer_receiver, reviewer_error = await _resolve_receiver(
        db,
        project_id=project_row.id,
        member_user_id=member.user_id,
        replacement_id=replacement_reviewer_id,
        project_role=ProjectRole.REVIEWER.value,
    )

    if (
        replacement_annotator_id is not None
        and replacement_reviewer_id is not None
        and replacement_annotator_id == replacement_reviewer_id
    ):
        blockers.append(_blocker("identical_replacement_responsibility"))
    # A supplied receiver must be valid regardless of whether we counted work.
    if annotation_error is not None:
        blockers.append(annotation_error)
    if reviewer_error is not None:
        blockers.append(reviewer_error)

    annotation_task_ids = set(uuid.UUID(tid) for tid in snapshot["annotation_task_ids"])
    review_task_ids = set(uuid.UUID(tid) for tid in snapshot["review_task_ids"])
    lock_task_ids = set(uuid.UUID(tid) for tid in snapshot["lock_task_ids"])
    batch_annotator_ids = set(uuid.UUID(bid) for bid in snapshot["batch_annotator_ids"])
    batch_reviewer_ids = set(uuid.UUID(bid) for bid in snapshot["batch_reviewer_ids"])

    # The responsibility is removed by the *target* role while the snapshot
    # still carries actual work: a blocker is only cleared by a real handoff.
    annotation_removed = target_role != ProjectRole.ANNOTATOR.value
    review_removed = target_role != ProjectRole.REVIEWER.value
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
            conflict = await _annotation_receiver_conflicts(
                db,
                project_id=project_row.id,
                member_user_id=member.user_id,
                receiver_id=annotation_receiver.id,
            )
            if conflict is not None:
                blockers.append(conflict)
            else:
                annotation_handoff_ready = True

    if review_removed and has_review_work:
        if replacement_reviewer_id is None:
            blockers.append(_blocker("unfinished_review_work"))
        elif reviewer_error is not None:
            review_handoff_ready = False
        elif reviewer_receiver is not None:
            conflict = await _review_receiver_conflicts(
                db,
                project_id=project_row.id,
                member_user_id=member.user_id,
                receiver_id=reviewer_receiver.id,
            )
            if conflict is not None:
                blockers.append(conflict)
            else:
                review_handoff_ready = True

    # Active locks are releasable, but only when the responsibility that owns
    # the lock has a valid handoff (or is retained).  An uncovered lock blocks.
    if lock_task_ids and target_role != member.role:
        annotation_lock = lock_task_ids & annotation_task_ids
        review_lock = lock_task_ids & review_task_ids
        uncovered = lock_task_ids - annotation_lock - review_lock
        annotation_locks_ok = (
            target_role == ProjectRole.ANNOTATOR.value or annotation_handoff_ready
        )
        review_locks_ok = (
            target_role == ProjectRole.REVIEWER.value or review_handoff_ready
        )
        if (
            uncovered
            or (annotation_lock and not annotation_locks_ok)
            or (review_lock and not review_locks_ok)
        ):
            blockers.append(
                _blocker(
                    "active_locks",
                    task_ids=sorted(str(tid) for tid in lock_task_ids),
                )
            )

    # Losing the last reviewer must cover remaining unassigned review work as
    # well as the member's own target-owned review work and active batch
    # reviewer defaults.
    if (
        member.role == ProjectRole.REVIEWER.value
        and review_removed
        and (has_review_work or snapshot["unassigned_review_task_ids"])
    ):
        replacement_ready = (
            reviewer_error is None and replacement_reviewer_id is not None
        )
        if not replacement_ready and not snapshot["eligible_reviewer_ids"]:
            blockers.append(_blocker("reviewer_coverage_lost"))
        elif not replacement_ready:
            # A remaining reviewer membership alone does not prove coverage:
            # that reviewer may be a frozen contributor or the effective
            # annotator for an outstanding task, and their claim would be
            # denied.  Coverage exists per task only when some eligible
            # reviewer can actually claim it; unknown (legacy) evidence keeps
            # the membership-based fallback.
            uncovered_review_task_ids = [
                task_id
                for task_id in snapshot["unassigned_review_task_ids"]
                if snapshot["tasks"].get(task_id, {}).get("status")
                == TaskStatus.REVIEW.value
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


def _handoff_blockers(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in blockers if entry["code"] != "same_role"]


#: Blockers ignored by removal: removal never grants a new role, so a role
#: pairing that would be incompatible on write must not trap an idle member
#: (including an administrative or disabled account) in the project forever.
_REMOVAL_IGNORED_BLOCKER_CODES = frozenset({"same_role", "target_role_incompatible"})


# ---------------------------------------------------------------------------
# Mutation paths
# ---------------------------------------------------------------------------


async def _assert_actor_can_manage(*, actor: User | None, project: Project) -> None:
    """Recheck actor activity and manager authority *after* locks are held."""

    if actor is None or not actor.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="账号已停用"
        )
    if is_privileged_for_project(actor, project):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="仅项目负责人或超级管理员可执行成员变更",
    )


async def add_member(
    db: AsyncSession,
    *,
    project: Project,
    actor: User,
    target_user_id: uuid.UUID,
    project_role: str,
) -> ProjectMember:
    """Add a compatible active account to the project (409 on duplicate)."""

    if project_role not in PROJECT_ROLES:
        raise HTTPException(status_code=400, detail="非法项目职责")

    accounts = await _lock_accounts_nowait(db, [actor.id, target_user_id])
    locked_actor = accounts.get(actor.id)
    locked_project = await _lock_project_nowait(db, project.id)
    await _assert_actor_can_manage(actor=locked_actor, project=locked_project)

    # Use only the locked account row; a missing target is a hard 404 rather
    # than a stale identity-map fallback.
    target = accounts.get(target_user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    # Platform/project compatibility is rechecked from the locked account row.
    assert_membership_role_compatible(target.role, project_role)

    result = await db.execute(
        pg_insert(ProjectMember)
        .values(
            id=uuid.uuid4(),
            project_id=locked_project.id,
            user_id=target_user_id,
            role=project_role,
            assigned_by=locked_actor.id,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
        .returning(ProjectMember.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="该用户已在项目中")

    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == locked_project.id,
                ProjectMember.user_id == target_user_id,
            )
        )
    ).scalar_one()
    await AuditService.log(
        db,
        actor=locked_actor,
        action=AuditAction.PROJECT_MEMBER_ADD,
        target_type="project_member",
        target_id=str(member.id),
        detail={
            "project_id": str(locked_project.id),
            "user_id": str(target_user_id),
            "project_role": project_role,
            "platform_role": target.role,
        },
    )
    await db.commit()
    await db.refresh(member)
    return member


async def preview_role_change(
    db: AsyncSession,
    *,
    project: Project,
    actor: User,
    member_id: uuid.UUID,
    target_role: str,
    replacement_annotator_id: uuid.UUID | None,
    replacement_reviewer_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Read-only role-change preview (no writes, no locks)."""

    if target_role not in PROJECT_ROLES:
        raise HTTPException(status_code=400, detail="非法项目职责")
    member = await db.scalar(
        select(ProjectMember)
        .where(
            ProjectMember.id == member_id,
            ProjectMember.project_id == project.id,
        )
        .execution_options(populate_existing=True)
    )
    if member is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    fresh_project = await db.get(Project, project.id, populate_existing=True)
    if fresh_project is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    snapshot, blockers = await evaluate_role_change(
        db,
        project=project,
        member=member,
        target_role=target_role,
        replacement_annotator_id=replacement_annotator_id,
        replacement_reviewer_id=replacement_reviewer_id,
        fresh_project=fresh_project,
    )
    return {
        "member_id": member.id,
        "user_id": member.user_id,
        "current_role": member.role,
        "current_version": member.version,
        "target_role": target_role,
        "requires_handoff": bool(_handoff_blockers(blockers)),
        "blockers": [entry["code"] for entry in blockers],
        "blocker_details": blockers,
        "resource_snapshot": snapshot,
        "preview_token": _snapshot_token(snapshot),
    }


def _replace_assigned_user_id(
    values: Iterable[Any], *, old_id: uuid.UUID, new_id: uuid.UUID
) -> list[str]:
    old_str, new_str = str(old_id), str(new_id)
    updated: list[str] = []
    replaced = False
    for raw in values or []:
        if str(raw) == old_str:
            updated.append(new_str)
            replaced = True
        else:
            updated.append(str(raw))
    if not replaced:
        updated.append(new_str)
    return updated


async def _release_member_project_locks(
    db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    task_ids = await _active_lock_task_ids(db, project_id=project_id, user_id=user_id)
    if not task_ids:
        return 0
    await db.execute(
        delete(TaskLock).where(
            TaskLock.user_id == user_id,
            TaskLock.task_id.in_(task_ids),
        )
    )
    return len(task_ids)


async def _handoff_annotation_work(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    from_user_id: uuid.UUID,
    to_user_id: uuid.UUID,
    active_batch_ids: Iterable[uuid.UUID],
) -> None:
    """Move outstanding annotation work, preserving override/attribution state.

    Only unfinished task rows receive the new assignee; ``assigned_at`` and the
    ``assignee_is_override`` flag are preserved so a task override never
    silently turns into a batch default (and vice versa).  Only batch defaults
    still used by unfinished work are rewritten; terminal batches keep their
    historical attribution.
    """

    task_ids = await _annotation_task_ids(
        db, project_id=project_id, user_id=from_user_id
    )
    if task_ids:
        await db.execute(
            update(Task).where(Task.id.in_(task_ids)).values(assignee_id=to_user_id)
        )
    batch_ids = sorted({uuid.UUID(str(bid)) for bid in active_batch_ids})
    if batch_ids:
        # Materialize the old default onto tasks past the editable annotation
        # phase that only inherited it, so changing the batch default cannot
        # retroactively rewrite their historical attribution.  Unfinished
        # annotation rows were reassigned to the receiver above.
        await db.execute(
            update(Task)
            .where(
                Task.batch_id.in_(batch_ids),
                Task.assignee_id.is_(None),
                Task.status.not_in(UNFINISHED_ANNOTATION_STATUSES),
            )
            .values(assignee_id=from_user_id)
        )
        batches = (
            await db.scalars(
                select(TaskBatch)
                .where(TaskBatch.id.in_(batch_ids))
                .order_by(TaskBatch.id)
                .execution_options(populate_existing=True)
            )
        ).all()
        for batch in batches:
            batch.annotator_id = to_user_id
            batch.assigned_user_ids = _replace_assigned_user_id(
                batch.assigned_user_ids, old_id=from_user_id, new_id=to_user_id
            )


async def _handoff_review_work(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    from_user_id: uuid.UUID,
    to_user_id: uuid.UUID,
    active_batch_ids: Iterable[uuid.UUID],
) -> None:
    """Move outstanding review work; a transferred claim is released."""

    task_ids = await _review_task_ids(db, project_id=project_id, user_id=from_user_id)
    if task_ids:
        await db.execute(
            update(Task)
            .where(Task.id.in_(task_ids))
            .values(reviewer_id=to_user_id, reviewer_claimed_at=None)
        )
    batch_ids = sorted({uuid.UUID(str(bid)) for bid in active_batch_ids})
    if batch_ids:
        # Preserve the effective reviewer on completed history that only
        # inherited the batch default.  Pending/in-progress rows keep a NULL
        # reviewer so they inherit the *new* default; review rows were moved to
        # the receiver above.
        await db.execute(
            update(Task)
            .where(
                Task.batch_id.in_(batch_ids),
                Task.reviewer_id.is_(None),
                Task.status == TaskStatus.COMPLETED.value,
            )
            .values(reviewer_id=from_user_id)
        )
        batches = (
            await db.scalars(
                select(TaskBatch)
                .where(TaskBatch.id.in_(batch_ids))
                .order_by(TaskBatch.id)
                .execution_options(populate_existing=True)
            )
        ).all()
        for batch in batches:
            batch.reviewer_id = to_user_id
            batch.assigned_user_ids = _replace_assigned_user_id(
                batch.assigned_user_ids, old_id=from_user_id, new_id=to_user_id
            )


async def change_role(
    db: AsyncSession,
    *,
    project: Project,
    actor: User,
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

    old_role = member.role
    annotation_removed = target_role != ProjectRole.ANNOTATOR.value
    review_removed = target_role != ProjectRole.REVIEWER.value
    if annotation_removed and (
        snapshot["annotation_task_ids"] or snapshot["batch_annotator_ids"]
    ):
        if replacement_annotator_id is not None:
            await _handoff_annotation_work(
                db,
                project_id=locked_project.id,
                from_user_id=member.user_id,
                to_user_id=replacement_annotator_id,
                active_batch_ids=snapshot["batch_annotator_ids"],
            )
    if review_removed and (
        snapshot["review_task_ids"]
        or snapshot["review_claim_task_ids"]
        or snapshot["batch_reviewer_ids"]
    ):
        if replacement_reviewer_id is not None:
            await _handoff_review_work(
                db,
                project_id=locked_project.id,
                from_user_id=member.user_id,
                to_user_id=replacement_reviewer_id,
                active_batch_ids=snapshot["batch_reviewer_ids"],
            )
    released = await _release_member_project_locks(
        db, project_id=locked_project.id, user_id=member.user_id
    )

    member.role = target_role
    member.version = member.version + 1
    member.updated_at = _utcnow()

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
    actor: User,
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

    await _release_member_project_locks(
        db, project_id=locked_project.id, user_id=member.user_id
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
    await db.delete(member)
    await db.commit()
