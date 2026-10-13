from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.schemas.data_manager_actions import (
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentItem,
    DataManagerTaskAssignmentRequest,
    DataManagerTaskAssignmentResponse,
)
from app.services.batch import BatchService
from app.services.scheduler import (
    effective_task_assignee_id,
    effective_task_reviewer_id,
)
from app.services.task_lock import TaskLockConflictError, TaskLockService


_ASSIGNABLE_TASK_STATUSES = {"pending", "in_progress", "review", "rejected"}


def normalize_idempotency_key(value: str | None) -> str | None:
    """Normalize the optional action key shared by scoped async operations."""

    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > 128:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must contain 1 to 128 characters",
        )
    return value


def request_digest(payload: object) -> str:
    """Return a stable digest for an idempotent request body."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def lock_idempotency_key(
    db: AsyncSession,
    *,
    action: str,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    key: str,
) -> None:
    """Serialize duplicate submissions without introducing a new ledger table."""

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {
            "key": f"aap:data-manager:{action}:{project_id}:{actor_id}:{key}",
        },
    )


async def find_idempotent_job(
    db: AsyncSession,
    *,
    kind: str,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    key: str,
) -> AsyncJob | None:
    """Find a scoped async job recorded by a Data Manager action."""

    return await db.scalar(
        select(AsyncJob)
        .where(
            AsyncJob.kind == kind,
            AsyncJob.project_id == project_id,
            AsyncJob.user_id == actor_id,
            AsyncJob.payload["data_manager_idempotency_key"].astext == key,
        )
        .order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
        .with_for_update()
    )


def assert_idempotent_request_matches(
    job: AsyncJob,
    *,
    digest: str,
) -> None:
    if (job.payload or {}).get("data_manager_request_digest") != digest:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "message": "Idempotency-Key was already used with another request",
                "job_id": str(job.id),
            },
        )


def _task_version_payload(
    payload: DataManagerTaskAssignmentRequest,
    items: list[DataManagerTaskAssignmentItem],
) -> dict[str, Any]:
    return {
        "task_ids": [str(task_id) for task_id in payload.task_ids],
        "annotator_set": "annotator_id" in payload.model_fields_set,
        "annotator_id": str(payload.annotator_id) if payload.annotator_id else None,
        "reviewer_set": "reviewer_id" in payload.model_fields_set,
        "reviewer_id": str(payload.reviewer_id) if payload.reviewer_id else None,
        "items": [item.model_dump(mode="json") for item in items],
    }


def _preview_version(
    payload: DataManagerTaskAssignmentRequest,
    items: list[DataManagerTaskAssignmentItem],
) -> str:
    encoded = json.dumps(
        _task_version_payload(payload, items),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class DataManagerTaskActionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _load_tasks(
        self,
        project_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        *,
        for_update: bool = False,
    ) -> dict[uuid.UUID, tuple[Task, TaskBatch | None]]:
        stmt = (
            select(Task, TaskBatch)
            .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
            .where(Task.project_id == project_id, Task.id.in_(task_ids))
            .order_by(Task.id)
        )
        if for_update:
            # PostgreSQL cannot apply FOR UPDATE to the nullable side of an
            # outer join. Lock batches first (matching existing batch writes),
            # then lock the task rows and attach the batch objects in memory.
            batch_ids = list(
                (
                    await self.db.execute(
                        select(Task.batch_id)
                        .where(
                            Task.project_id == project_id,
                            Task.id.in_(task_ids),
                            Task.batch_id.is_not(None),
                        )
                        .order_by(Task.batch_id)
                    )
                )
                .scalars()
                .all()
            )
            batches = {}
            if batch_ids:
                batches = {
                    batch.id: batch
                    for batch in (
                        await self.db.execute(
                            select(TaskBatch)
                            .where(TaskBatch.id.in_(set(batch_ids)))
                            .order_by(TaskBatch.id)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                }
            task_rows = (
                (
                    await self.db.execute(
                        select(Task)
                        .where(Task.project_id == project_id, Task.id.in_(task_ids))
                        .order_by(Task.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            return {task.id: (task, batches.get(task.batch_id)) for task in task_rows}
        rows = (await self.db.execute(stmt)).all()
        return {task.id: (task, batch) for task, batch in rows}

    async def _plan(
        self,
        project_id: uuid.UUID,
        payload: DataManagerTaskAssignmentRequest,
        *,
        actor: User,
        for_update: bool = False,
    ) -> DataManagerTaskAssignmentResponse:
        annotator_set = "annotator_id" in payload.model_fields_set
        reviewer_set = "reviewer_id" in payload.model_fields_set
        assignment_targets: list[tuple[str, uuid.UUID]] = []
        if annotator_set and payload.annotator_id is not None:
            assignment_targets.append(("annotator", payload.annotator_id))
        if reviewer_set and payload.reviewer_id is not None:
            assignment_targets.append(("reviewer", payload.reviewer_id))
        await BatchService(self.db)._lock_and_validate_assignment_targets(
            project_id, assignment_targets
        )

        loaded = await self._load_tasks(
            project_id, payload.task_ids, for_update=for_update
        )
        lock_service = TaskLockService(self.db)
        items: list[DataManagerTaskAssignmentItem] = []
        for task_id in payload.task_ids:
            loaded_row = loaded.get(task_id)
            if loaded_row is None:
                items.append(
                    DataManagerTaskAssignmentItem(
                        task_id=task_id,
                        reason="task_not_found_or_outside_project",
                    )
                )
                continue

            task, batch = loaded_row
            before_annotator = task.assignee_id
            before_reviewer = task.reviewer_id
            effective_before_annotator = effective_task_assignee_id(task, batch)
            effective_before_reviewer = effective_task_reviewer_id(task, batch)
            after_annotator = (
                payload.annotator_id if annotator_set else before_annotator
            )
            after_reviewer = payload.reviewer_id if reviewer_set else before_reviewer
            effective_after_annotator = (
                (
                    payload.annotator_id
                    if payload.annotator_id is not None
                    else (batch.annotator_id if batch is not None else None)
                )
                if annotator_set
                else effective_before_annotator
            )
            effective_after_reviewer = (
                (
                    payload.reviewer_id
                    if payload.reviewer_id is not None
                    else (batch.reviewer_id if batch is not None else None)
                )
                if reviewer_set
                else effective_before_reviewer
            )
            item = DataManagerTaskAssignmentItem(
                task_id=task.id,
                task_display_id=task.display_id,
                batch_id=task.batch_id,
                status=task.status,
                task_updated_at=task.updated_at,
                before_annotator_id=before_annotator,
                after_annotator_id=after_annotator,
                before_reviewer_id=before_reviewer,
                after_reviewer_id=after_reviewer,
                effective_before_annotator_id=effective_before_annotator,
                effective_after_annotator_id=effective_after_annotator,
                effective_before_reviewer_id=effective_before_reviewer,
                effective_after_reviewer_id=effective_after_reviewer,
            )
            if batch is not None and batch.admin_locked:
                item.reason = "batch_admin_locked"
            elif task.status not in _ASSIGNABLE_TASK_STATUSES:
                item.reason = "task_status_not_assignable"
            elif (
                reviewer_set
                and payload.reviewer_id is not None
                and task.status != "review"
            ):
                # ``submit`` intentionally clears reviewer fields while
                # starting a review round.  Accept reviewer assignment only
                # once a task is in review so the bulk action has durable
                # claim semantics instead of silently losing the assignment.
                item.reason = "reviewer_assignment_requires_review"
            else:
                active_lock = await lock_service.active_lock(task.id)
                if active_lock is not None:
                    item.reason = "task_locked"
                elif batch is not None and batch.status == "archived":
                    item.reason = "batch_archived"
                elif (
                    before_annotator == after_annotator
                    and before_reviewer == after_reviewer
                    and (
                        not annotator_set
                        or task.assignee_is_override
                        == (payload.annotator_id is not None)
                    )
                    and (
                        not reviewer_set
                        or task.reviewer_is_override
                        == (payload.reviewer_id is not None)
                    )
                ):
                    item.reason = "assignment_unchanged"
                else:
                    item.will_change = True
            items.append(item)

        version = _preview_version(payload, items)
        eligible = sum(1 for item in items if item.will_change)
        skipped = sum(
            1
            for item in items
            if not item.will_change and item.reason == "assignment_unchanged"
        )
        failed = len(items) - eligible - skipped
        return DataManagerTaskAssignmentResponse(
            task_ids=payload.task_ids,
            preview_version=version,
            eligible_count=eligible,
            skipped_count=skipped,
            failed_count=failed,
            items=items,
        )

    async def preview_assignment(
        self,
        project_id: uuid.UUID,
        payload: DataManagerTaskAssignmentRequest,
        *,
        actor: User,
    ) -> DataManagerTaskAssignmentResponse:
        return await self._plan(project_id, payload, actor=actor)

    async def apply_assignment(
        self,
        project_id: uuid.UUID,
        payload: DataManagerTaskAssignmentApplyRequest,
        *,
        actor: User,
    ) -> DataManagerTaskAssignmentResponse:
        plan = await self._plan(project_id, payload, actor=actor, for_update=True)
        if plan.preview_version != payload.preview_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "data_manager_assignment_preview_stale",
                    "message": "任务状态或分派已变化，请重新预览后确认",
                },
            )

        annotator_set = "annotator_id" in payload.model_fields_set
        reviewer_set = "reviewer_id" in payload.model_fields_set
        loaded = await self._load_tasks(project_id, payload.task_ids, for_update=True)
        lock_service = TaskLockService(self.db)
        succeeded: list[uuid.UUID] = []
        for item in plan.items:
            if not item.will_change:
                continue
            loaded_row = loaded.get(item.task_id)
            if loaded_row is None:
                item.will_change = False
                item.reason = "task_not_found_or_outside_project"
                continue
            task, _batch = loaded_row
            try:
                await lock_service.assert_write_allowed(task.id, actor.id)
            except TaskLockConflictError:
                item.will_change = False
                item.reason = "task_locked"
                continue
            # The generic write guard intentionally permits an actor's own
            # editing lock. Assignment must skip every active editor lock,
            # including the actor's, so an open editor cannot keep stale
            # ownership after a reassignment. The advisory lock acquired above
            # serializes this check with lock acquisition.
            if await lock_service.active_lock(task.id) is not None:
                item.will_change = False
                item.reason = "task_locked"
                continue
            if annotator_set:
                task.assignee_id = payload.annotator_id
                task.assignee_is_override = payload.annotator_id is not None
                task.assigned_at = (
                    datetime.now(timezone.utc)
                    if payload.annotator_id is not None
                    else None
                )
            if reviewer_set:
                task.reviewer_id = payload.reviewer_id
                task.reviewer_is_override = payload.reviewer_id is not None
                if item.before_reviewer_id != payload.reviewer_id:
                    task.reviewer_claimed_at = None
            succeeded.append(task.id)

        await self.db.flush()
        plan.succeeded = succeeded
        plan.eligible_count = len(succeeded)
        plan.failed_count = sum(
            1
            for item in plan.items
            if item.reason and item.reason != "assignment_unchanged"
        )
        plan.skipped_count = len(plan.items) - plan.eligible_count - plan.failed_count
        return plan
