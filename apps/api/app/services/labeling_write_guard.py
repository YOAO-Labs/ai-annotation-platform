"""Local final-write admission for immutable inference-to-labeling facts."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import WORKBENCH_AI_EDITABLE_TASK_STATUSES
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import task_access
from app.services.batch_permissions import allows_bulk_preannotation
from app.services.labeling_write_scope import (
    LabelingBatchWriteScope,
    LabelingWriteOperation,
    LabelingWriteScope,
)
from app.services.project_access import ProjectAccess, lock_current_project_access
from app.services.task_lock import TaskLockService

_MANAGER_OPERATIONS = frozenset(
    {"preannotation", "prediction_import", "prediction_retry"}
)
_JOB_KINDS = {
    "preannotation": "batch_predict",
    "prediction_import": "predictions_import",
    "prediction_retry": "prediction_retry",
    "mask_repair": AsyncJobKind.MASK_REPAIR.value,
}


def _conflict(reason: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"reason": reason})


async def _locked_row(db: AsyncSession, statement, *, reason: str):
    try:
        return (await db.execute(statement)).scalar_one_or_none()
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in {"55P03", "40001", "40P01"}:
            raise _conflict(reason) from exc
        raise


async def _assert_execution_current(
    db: AsyncSession,
    scope: LabelingWriteScope | LabelingBatchWriteScope,
    *,
    task_id: uuid.UUID | None,
    batch_id: uuid.UUID | None,
) -> None:
    if scope.job_id is None:
        return
    job = await _locked_row(
        db,
        select(AsyncJob)
        .where(AsyncJob.id == scope.job_id)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True),
        reason="job_busy",
    )
    if (
        job is None
        or job.project_id != scope.project_id
        or job.user_id != scope.actor_id
        or job.kind != _JOB_KINDS.get(scope.operation)
    ):
        raise _conflict("prediction_execution_scope_mismatch")
    payload = job.payload or {}
    # A running item's cooperative cancellation is observed at its existing
    # item boundary; an actual terminal job can never admit a late result.
    if job.status not in {"pending", "running"} or (
        job.status == "pending" and payload.get("cancel_requested")
    ):
        raise _conflict("prediction_execution_not_writable")
    if scope.operation != "preannotation":
        return
    if "batch_id" not in payload or "execution_scope" not in payload:
        raise _conflict("labeling_write_scope_unknown")
    if payload["execution_scope"] != scope.execution_scope:
        raise _conflict("prediction_execution_scope_mismatch")
    saved_batch = payload["batch_id"]
    if saved_batch is not None and str(saved_batch) != str(batch_id):
        raise _conflict("prediction_execution_scope_mismatch")
    if task_id is None and saved_batch is None:
        raise _conflict("prediction_execution_scope_mismatch")
    selected = payload.get("task_ids")
    if selected is not None and (
        not isinstance(selected, list)
        or (task_id is not None and str(task_id) not in selected)
    ):
        raise _conflict("prediction_execution_scope_mismatch")


async def lock_labeling_write(
    db: AsyncSession,
    scope: LabelingWriteScope,
    *,
    operations: tuple[LabelingWriteOperation, ...],
) -> tuple[Task, User, ProjectAccess]:
    """Hold current authority and target locks through the caller's transaction.

    A captured scope is a precondition, never a grant. Known jobless commands
    remain jobless; unknown actors/scopes never become system authorization.
    """
    if scope.operation not in operations:
        raise _conflict("labeling_write_scope_mismatch")
    if not scope.actor_known or scope.actor_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if (
        not scope.execution_scope_known
        or not scope.expected_batch_id_known
        or not scope.job_id_known
        or scope.task_version is None
    ):
        raise _conflict("labeling_write_scope_unknown")
    with db.no_autoflush:
        actor, _project, access = await lock_current_project_access(
            db, actor_id=scope.actor_id, project_id=scope.project_id
        )
        if scope.operation in _MANAGER_OPERATIONS and not access.is_manager:
            raise HTTPException(status_code=403, detail="项目管理权限已变化")
        batch = None
        if scope.expected_batch_id is not None:
            batch = await _locked_row(
                db,
                select(TaskBatch)
                .where(
                    TaskBatch.id == scope.expected_batch_id,
                    TaskBatch.project_id == scope.project_id,
                )
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True),
                reason="batch_busy",
            )
            if batch is None:
                raise _conflict("prediction_task_scope_changed")
        task = await _locked_row(
            db,
            select(Task)
            .where(Task.id == scope.task_id, Task.project_id == scope.project_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True),
            reason="task_busy",
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if (
            task.version != scope.task_version
            or task.status != scope.task_status
            or task.assignee_id != scope.assignee_id
            or task.review_round_id != scope.review_round_id
            or task.batch_id != scope.expected_batch_id
        ):
            raise _conflict("prediction_task_scope_changed")
        await task_access.assert_task_visible(db, task, actor, access=access)
        if scope.operation == "preannotation":
            if scope.execution_scope not in {"bulk", "workbench"}:
                raise _conflict("labeling_write_scope_unknown")
            if batch is not None and batch.admin_locked:
                raise _conflict("batch_admin_locked")
            if scope.execution_scope == "bulk":
                if task.status != "pending" or (
                    batch is not None and not allows_bulk_preannotation(batch)
                ):
                    raise _conflict("preannotation_task_not_eligible")
            elif task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES:
                raise _conflict("preannotation_task_not_eligible")
        elif scope.operation not in {"prediction_import", "prediction_retry"}:
            task_access.assert_annotation_write_allowed(
                task,
                access,
                allow_review_adjustment=scope.operation != "secondary_inference",
            )
            task_access.assert_task_editable(task, actor, access=access)
            await task_access.assert_review_adjustment_evidence(db, task, actor, access)
            if task.status != "review":
                task_access.assert_effective_task_assignee(
                    actor,
                    await task_access.resolve_effective_task_assignee_id(db, task),
                    action="write",
                    project_id=task.project_id,
                    allow_open_pool=True,
                    access=access,
                )
        if scope.operation != "prediction_import":
            await TaskLockService(db).assert_final_write_available(
                task.id,
                actor.id,
                allow_own=not (
                    scope.operation == "preannotation"
                    and scope.execution_scope == "bulk"
                ),
            )
        if scope.operation in {"secondary_inference", "mask_repair"}:
            if (
                scope.source_annotation_id is None
                or scope.source_annotation_version is None
            ):
                raise _conflict("labeling_write_scope_unknown")
            source = await _locked_row(
                db,
                select(Annotation)
                .where(
                    Annotation.id == scope.source_annotation_id,
                    Annotation.task_id == task.id,
                    Annotation.project_id == task.project_id,
                )
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True),
                reason="annotation_busy",
            )
            if source is None or not source.is_active or source.was_cancelled:
                raise HTTPException(status_code=404, detail="annotation not found")
            if source.version != scope.source_annotation_version:
                raise _conflict("annotation_version_conflict")
        await _assert_execution_current(
            db, scope, task_id=task.id, batch_id=task.batch_id
        )
    return task, actor, access


async def lock_labeling_batch_write(
    db: AsyncSession, scope: LabelingBatchWriteScope
) -> TaskBatch:
    """Revalidate a batch prediction's current authority before lifecycle advance."""
    if scope.operation != "preannotation":
        raise _conflict("labeling_write_scope_mismatch")
    if not scope.actor_known or scope.actor_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if (
        not scope.execution_scope_known
        or not scope.job_id_known
        or scope.execution_scope not in {"bulk", "workbench"}
    ):
        raise _conflict("labeling_write_scope_unknown")
    with db.no_autoflush:
        _actor, _project, access = await lock_current_project_access(
            db, actor_id=scope.actor_id, project_id=scope.project_id
        )
        if not access.is_manager:
            raise HTTPException(status_code=403, detail="项目管理权限已变化")
        batch = await _locked_row(
            db,
            select(TaskBatch)
            .where(
                TaskBatch.id == scope.batch_id,
                TaskBatch.project_id == scope.project_id,
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True),
            reason="batch_busy",
        )
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found")
        await _assert_execution_current(db, scope, task_id=None, batch_id=batch.id)
    return batch
