"""Read task evidence and route its durable writes through Labeling's owner."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_audit.read_relation_postgres import audit_events
from aap_labeling.review_evidence_policy import (
    ReviewActorIneligible,
    ReviewEvidenceUnknown,
    assert_review_evidence_current as _assert_review_evidence_current,
    canonical_actor_id,
    canonical_actor_set,
    capture_first_review_contributor_snapshot,
    clear_review_contributor_evidence,
    current_review_evidence,
    ensure_review_round,
    freeze_review_contributor_evidence,
    known_annotation_contributor_ids,
    record_first_review_fact,
    start_review_round,
)
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.audit_read_relations import execute_audit_relation_read

_BUSY_SQLSTATES = {"55P03", "40P01", "40001"}
__all__ = [
    "assert_review_evidence_current",
    "canonical_actor_id",
    "canonical_actor_set",
    "capture_first_review_contributor_snapshot",
    "clear_review_contributor_evidence",
    "current_review_evidence",
    "ensure_review_round",
    "freeze_review_contributor_evidence",
    "known_annotation_contributor_ids",
    "lock_tasks_for_evidence",
    "record_annotation_actor",
    "record_annotation_actor_for_task",
    "record_annotation_actors_for_tasks",
    "record_first_review_fact",
    "refresh_task_evidence",
    "review_round_contributor_snapshot",
    "start_review_round",
    "task_contributor_snapshot",
]


async def task_contributor_snapshot(
    db: AsyncSession,
    task: Task,
    extra_user_ids: Iterable[uuid.UUID | None] = (),
) -> list[str]:
    """Read the current assignee and surviving annotation authors."""
    ids = {value for value in extra_user_ids if value is not None}
    if task.assignee_id is not None:
        ids.add(task.assignee_id)
    rows = await db.execute(
        select(Annotation.user_id)
        .where(
            Annotation.task_id == task.id,
            Annotation.user_id.is_not(None),
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .distinct()
    )
    ids.update(value for (value,) in rows if value is not None)
    return sorted(str(value) for value in ids)


async def review_round_contributor_snapshot(db: AsyncSession, task: Task) -> list[str]:
    """Read this round's submit evidence or its retention-safe task snapshot."""
    if task.review_round_id is None:
        return []
    row = (
        await execute_audit_relation_read(
            db,
            select(audit_events.c.detail_json)
            .where(
                audit_events.c.action.in_(("task.submit", "task.skip")),
                audit_events.c.target_id == str(task.id),
                audit_events.c.status_code == 200,
                audit_events.c.detail_json["project_id"].astext == str(task.project_id),
                audit_events.c.detail_json["review_round_id"].astext
                == str(task.review_round_id),
            )
            .order_by(audit_events.c.created_at.desc(), audit_events.c.id.desc())
            .limit(1),
        )
    ).scalar_one_or_none()
    values = row.get("contributor_ids") if isinstance(row, dict) else None
    if not isinstance(values, list):
        values = (
            task.first_review_contributor_ids
            if task.first_reviewed_at is None
            else None
        )
    if not isinstance(values, list):
        return []
    return sorted({str(value) for value in values if value})


def assert_review_evidence_current(
    task: Task,
    actor_id: object,
    *,
    effective_annotator_id: object | None = None,
) -> None:
    """Map pure owner-policy failures to the existing API status and detail."""
    try:
        _assert_review_evidence_current(
            task, actor_id, effective_annotator_id=effective_annotator_id
        )
    except ReviewEvidenceUnknown:
        raise HTTPException(
            status_code=409,
            detail={"reason": "review_contributors_unknown"},
        ) from None
    except ReviewActorIneligible:
        raise HTTPException(
            status_code=403,
            detail={"reason": "self_review_denied"},
        ) from None


async def lock_tasks_for_evidence(
    db: AsyncSession,
    task_ids: Iterable[uuid.UUID | None],
    *,
    nowait: bool = False,
) -> None:
    """Lock task rows in stable order before mixed-order annotation writes."""
    ids = sorted({task_id for task_id in task_ids if task_id is not None}, key=str)
    if not ids:
        return
    statement = (
        select(Task.id)
        .where(Task.id.in_(ids))
        .order_by(Task.id)
        .with_for_update(nowait=nowait)
    )
    try:
        with db.no_autoflush:
            await db.execute(statement)
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in _BUSY_SQLSTATES:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "task_evidence_busy",
                    "message": "task is being updated; refresh and retry",
                },
            ) from exc
        raise


async def refresh_task_evidence(
    db: AsyncSession, task: Task, *, nowait: bool = False
) -> None:
    """Lock and refresh command scope before the Native owner rechecks it."""
    if nowait:
        await lock_tasks_for_evidence(db, [task.id], nowait=True)
    await db.flush()
    await db.refresh(
        task,
        [
            "project_id",
            "batch_id",
            "status",
            "review_round_id",
            "annotation_contributor_ids",
        ],
        with_for_update=True,
    )


async def record_annotation_actor(
    db: AsyncSession,
    task: Task,
    actor_id: uuid.UUID | None,
    *,
    lock: bool = True,
    nowait: bool = False,
) -> bool:
    """Persist one actor through Labeling; ``lock=False`` requires a held task lock."""
    actor = canonical_actor_id(actor_id)
    if actor is None:
        return False
    if lock:
        from app.services.identity_account_lock_data import IdentityAccountLockData

        try:
            if nowait:
                with db.no_autoflush:
                    await IdentityAccountLockData(db).lock_accounts(
                        [uuid.UUID(actor)], mode="share", nowait=True
                    )
            else:
                await IdentityAccountLockData(db).lock_accounts(
                    [uuid.UUID(actor)], mode="share", nowait=False
                )
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
                exc.orig, "pgcode", None
            )
            if sqlstate in _BUSY_SQLSTATES:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "task_evidence_busy",
                        "message": "task is being updated; refresh and retry",
                    },
                ) from exc
            raise
        # Mixed-order producers acquire both locks without flushing first.
        await refresh_task_evidence(db, task, nowait=nowait)
    from app.services.labeling_tasks import LabelingTaskService
    from app.application.task_workflow_errors import TaskWorkflowError

    try:
        return await LabelingTaskService(db).record_annotation_actor(
            task, uuid.UUID(actor)
        )
    except TaskWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def record_annotation_actor_for_task(
    db: AsyncSession,
    task_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    nowait: bool = False,
) -> bool:
    if canonical_actor_id(actor_id) is None:
        return False
    with db.no_autoflush:
        task = await db.get(Task, task_id)
    if task is None:
        return False
    return await record_annotation_actor(db, task, actor_id, nowait=nowait)


async def record_annotation_actors_for_tasks(
    db: AsyncSession,
    task_ids: Iterable[uuid.UUID],
    actor_id: uuid.UUID | None,
    *,
    nowait: bool = False,
) -> set[uuid.UUID]:
    ids = sorted(set(task_ids), key=str)
    if nowait:
        await lock_tasks_for_evidence(db, ids, nowait=True)
    touched: set[uuid.UUID] = set()
    for task_id in ids:
        await record_annotation_actor_for_task(db, task_id, actor_id, nowait=nowait)
        touched.add(task_id)
    return touched
