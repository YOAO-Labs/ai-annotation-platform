"""Canonical task visibility and annotation-write admission.

HTTP dependencies and mutation owners use the same project-bound rules. This
module does not acquire resource locks or commit: callers preserve their own
account/membership/resource lock order and transaction boundaries.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ProjectRole
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.annotation_evidence import assert_review_evidence_current
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectCapability,
)
from app.services.project_access import get_workspace_project, resolve_project_access
from app.services.scheduler import (
    annotator_can_rework_task,
    effective_task_assignee_id,
    visible_batch_statuses_for_project_role,
)
from app.services.labeling_core_read_relations import (
    batch_row,
    get_core_row,
    task_row,
)

_LOCKED_STATUSES = {"review", "completed"}


def assert_access_binding(
    access: ProjectAccess,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
) -> None:
    """Fail closed when the resolved access does not match user and resource."""

    if access.user_id != actor_id or access.project_id != project_id:
        raise HTTPException(
            status_code=409,
            detail={"reason": "access_context_mismatch"},
        )


def assert_task_editable(
    task: Task,
    actor_id: uuid.UUID | None = None,
    *,
    access: ProjectAccess | None = None,
) -> None:
    """Keep submitted/completed tasks locked, except authorized review edits.

    Only ``access.is_manager`` is management: a foreign-project administrator
    membership must not bypass task assignment or review checks. A non-manager
    without matching access fails closed; the global role is never a fallback.
    """
    if actor_id is not None and access is not None:
        assert_access_binding(access, actor_id=actor_id, project_id=task.project_id)

    is_manager = bool(access is not None and access.is_manager)
    if actor_id is not None and not is_manager:
        if access is None:
            raise HTTPException(status_code=403, detail="缺少项目权限上下文")
        if (
            access.project_role == ProjectRole.ANNOTATOR.value
            and task.assignee_id is not None
            and task.assignee_id != actor_id
        ):
            raise HTTPException(
                status_code=403, detail="Task belongs to another annotator"
            )
    if task.status not in _LOCKED_STATUSES:
        return
    if task.status == "review" and actor_id is not None:
        if is_manager or (
            access is not None and access.project_role == ProjectRole.REVIEWER.value
        ):
            return
    raise HTTPException(
        status_code=409,
        detail={"reason": "task_locked", "status": task.status},
    )


async def resolve_effective_task_assignee_id(
    db: AsyncSession, task: Task
) -> uuid.UUID | None:
    batch = await get_core_row(db, batch_row, task.batch_id) if task.batch_id else None
    return effective_task_assignee_id(task, batch)


def assert_effective_task_assignee(
    actor_id: uuid.UUID,
    effective_assignee_id: uuid.UUID | None,
    *,
    action: str,
    project_id: uuid.UUID,
    allow_open_pool: bool = False,
    access: ProjectAccess | None = None,
) -> None:
    """Require the effective annotator or the resource's verified manager.

    ``project_id`` is the actual resource project. A matching project annotator
    may use an unassigned open pool only when the caller explicitly allows it.
    """

    if access is not None:
        assert_access_binding(access, actor_id=actor_id, project_id=project_id)
        if access.is_manager:
            return
        if access.project_role == ProjectRole.ANNOTATOR.value and (
            effective_assignee_id == actor_id
            or (allow_open_pool and effective_assignee_id is None)
        ):
            return
    raise HTTPException(status_code=403, detail=f"only effective assignee can {action}")


def _task_write_allowed(
    task: Task,
    access: ProjectAccess,
    *,
    allow_review_adjustment: bool,
) -> bool:
    """Use project capabilities, admitting reviewers only for review adjustment."""

    if access.is_manager:
        return True
    if access.project_role == ProjectRole.ANNOTATOR.value and access.has(
        ProjectCapability.ANNOTATION_WRITE.value
    ):
        return True
    if (
        allow_review_adjustment
        and access.project_role == ProjectRole.REVIEWER.value
        and task.status == "review"
        and access.has(ProjectCapability.REVIEW_WRITE.value)
    ):
        return True
    return False


async def resolve_task_access(
    db: AsyncSession,
    task: Task,
    actor_id: uuid.UUID,
    *,
    lock_membership: bool = False,
) -> ProjectAccess:
    """Resolve the current project access for the task's actual project."""

    project = await get_workspace_project(
        db, actor_id=actor_id, project_id=task.project_id
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return await resolve_project_access(
        db, actor_id=actor_id, project=project, lock_membership=lock_membership
    )


def assert_annotation_write_allowed(
    task: Task,
    access: ProjectAccess,
    *,
    allow_review_adjustment: bool = True,
) -> None:
    """Require annotation-phase capability, independently of task editability."""

    if not _task_write_allowed(
        task, access, allow_review_adjustment=allow_review_adjustment
    ):
        raise HTTPException(status_code=403, detail="缺少项目权限: annotation.write")


async def assert_review_adjustment_evidence(
    db: AsyncSession,
    task: Task,
    actor_id: uuid.UUID,
    access: ProjectAccess,
) -> None:
    """Require current, complete, non-self frozen evidence for review edits.

    Annotators are blocked in review by ``assert_task_editable``. A manager or
    project reviewer adjusting that round must not be a contributor, submitter
    or effective annotator. Unknown legacy evidence fails closed.
    """

    if task.status != "review":
        return
    if not (access.is_manager or access.project_role == ProjectRole.REVIEWER.value):
        return
    effective_annotator_id = await resolve_effective_task_assignee_id(db, task)
    assert_review_evidence_current(
        task, actor_id, effective_annotator_id=effective_annotator_id
    )


async def assert_task_visible(
    db: AsyncSession,
    task: Task,
    actor_id: uuid.UUID,
    *,
    access: ProjectAccess | None = None,
) -> None:
    """Enforce project-bound batch/status and effective-assignment visibility."""

    project = await get_workspace_project(
        db, actor_id=actor_id, project_id=task.project_id
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if access is None:
        access = await resolve_project_access(db, actor_id=actor_id, project=project)
    assert_access_binding(access, actor_id=actor_id, project_id=task.project_id)
    if access.is_manager:
        return
    project_role = access.project_role
    if task.file_type == "video" and bool(
        (project.video_collaboration or {}).get("enabled")
    ):
        return
    if task.batch_id is None:
        if (
            project_role == ProjectRole.ANNOTATOR.value and task.assignee_id == actor_id
        ) or (
            project_role == ProjectRole.REVIEWER.value and task.reviewer_id == actor_id
        ):
            return
        raise HTTPException(status_code=404, detail="Task not found")
    batch = await get_core_row(db, batch_row, task.batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Task not found")

    visible_statuses = visible_batch_statuses_for_project_role(project_role)
    if batch.status not in visible_statuses and not annotator_can_rework_task(
        actor_id, batch, task.status, task.assignee_id, project_role=project_role
    ):
        raise HTTPException(status_code=404, detail="Task not found")

    if project_role == ProjectRole.REVIEWER.value:
        return

    is_task_assigned = task.assignee_id is not None and task.assignee_id == actor_id
    is_batch_assigned = task.assignee_id is None and batch.annotator_id == actor_id
    is_assigned = is_task_assigned or is_batch_assigned
    if batch.status == "rejected" and not is_assigned:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.assignee_id is not None and not is_task_assigned:
        raise HTTPException(status_code=404, detail="Task not found")
    if batch.annotator_id is not None and not is_assigned:
        raise HTTPException(status_code=404, detail="Task not found")


async def visible_task_ids(
    db: AsyncSession,
    project,
    actor_id: uuid.UUID,
    task_ids: list[uuid.UUID],
    *,
    access: ProjectAccess | None = None,
) -> set[uuid.UUID]:
    """Filter a bounded task set with the existing bulk visibility semantics."""

    if not task_ids:
        return set()
    try:
        if access is None:
            access = await resolve_project_access(
                db, actor_id=actor_id, project=project
            )
    except HTTPException:
        return set()
    assert_access_binding(access, actor_id=actor_id, project_id=project.id)
    if access.is_manager:
        result = await db.execute(
            select(task_row.id).where(
                task_row.project_id == project.id,
                task_row.id.in_(task_ids),
            )
        )
        return set(result.scalars().all())
    project_role = access.project_role

    rows = (
        await db.execute(
            select(
                task_row.id,
                task_row.batch_id,
                task_row.status,
                task_row.assignee_id,
                task_row.reviewer_id,
            ).where(task_row.project_id == project.id, task_row.id.in_(task_ids))
        )
    ).all()
    batch_ids = {bid for _, bid, _, _, _ in rows if bid is not None}
    batches: dict[uuid.UUID, TaskBatch] = {}
    if batch_ids:
        result = await db.execute(select(batch_row).where(batch_row.id.in_(batch_ids)))
        batches = {b.id: b for b in result.scalars()}

    visible_statuses = visible_batch_statuses_for_project_role(project_role)
    is_reviewer = project_role == ProjectRole.REVIEWER.value
    visible: set[uuid.UUID] = set()
    for tid, bid, task_status, task_assignee_id, task_reviewer_id in rows:
        if bid is None:
            if (
                project_role == ProjectRole.ANNOTATOR.value
                and task_assignee_id == actor_id
            ) or (
                project_role == ProjectRole.REVIEWER.value
                and task_reviewer_id == actor_id
            ):
                visible.add(tid)
            continue
        batch = batches.get(bid)
        if batch is None or (
            batch.status not in visible_statuses
            and not annotator_can_rework_task(
                actor_id,
                batch,
                task_status,
                task_assignee_id,
                project_role=project_role,
            )
        ):
            continue
        if is_reviewer:
            visible.add(tid)
            continue
        is_task_assigned = task_assignee_id is not None and task_assignee_id == actor_id
        is_batch_assigned = task_assignee_id is None and batch.annotator_id == actor_id
        is_assigned = is_task_assigned or is_batch_assigned
        if batch.status == "rejected" and not is_assigned:
            continue
        if task_assignee_id is not None and not is_task_assigned:
            continue
        if batch.annotator_id is not None and not is_assigned:
            continue
        visible.add(tid)
    return visible
