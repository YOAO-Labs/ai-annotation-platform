from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.batch_control_contract import ApplyTaskAssignments, TaskAssignment
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.schemas.labeling_assignment import (
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentItem,
    DataManagerTaskAssignmentRequest,
    DataManagerTaskAssignmentResponse,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.batch import BatchService, execute_batch_control
from app.services.scheduler import (
    effective_task_assignee_id,
    effective_task_reviewer_id,
)
from app.services.project_access import resolve_project_access_by_id
from app.services.project_write_guard import lock_project_manager
from app.services.task_lock import TaskLockService


_ASSIGNABLE_TASK_STATUSES = {"pending", "in_progress", "review", "rejected"}


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


def _stale_preview() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "data_manager_assignment_preview_stale",
            "message": "任务状态或分派已变化，请重新预览后确认",
        },
    )


class LabelingTaskAssignmentService:
    """Own selected-task assignment preview and mutation commands."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _load_tasks(
        self,
        project_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        *,
        for_update: bool = False,
    ) -> dict[uuid.UUID, tuple[Task, TaskBatch | None]]:
        if for_update:
            # Batch-before-task locks must still cover the task's current batch
            # after waiting; a changed scope is a stale preview, not an open pool.
            expected_batches = dict(
                (
                    await self.db.execute(
                        select(Task.id, Task.batch_id)
                        .where(Task.project_id == project_id, Task.id.in_(task_ids))
                        .order_by(Task.id)
                    )
                ).all()
            )
            batch_ids = {
                batch_id
                for batch_id in expected_batches.values()
                if batch_id is not None
            }
            batches = {}
            if batch_ids:
                batches = {
                    batch.id: batch
                    for batch in (
                        await self.db.execute(
                            select(TaskBatch)
                            .where(
                                TaskBatch.project_id == project_id,
                                TaskBatch.id.in_(batch_ids),
                            )
                            .order_by(TaskBatch.id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
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
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
            if any(
                task.id not in expected_batches
                or task.batch_id != expected_batches[task.id]
                or (task.batch_id is not None and task.batch_id not in batches)
                for task in task_rows
            ):
                raise _stale_preview()
            return {task.id: (task, batches.get(task.batch_id)) for task in task_rows}
        stmt = (
            select(Task, TaskBatch)
            .outerjoin(
                TaskBatch,
                and_(
                    TaskBatch.id == Task.batch_id,
                    TaskBatch.project_id == project_id,
                ),
            )
            .where(Task.project_id == project_id, Task.id.in_(task_ids))
            .order_by(Task.id)
            .execution_options(populate_existing=True)
        )
        rows = (await self.db.execute(stmt)).all()
        if any(task.batch_id is not None and batch is None for task, batch in rows):
            raise _stale_preview()
        return {task.id: (task, batch) for task, batch in rows}

    async def _plan(
        self,
        project_id: uuid.UUID,
        payload: DataManagerTaskAssignmentRequest,
        *,
        for_update: bool = False,
    ) -> tuple[
        DataManagerTaskAssignmentResponse,
        dict[uuid.UUID, tuple[Task, TaskBatch | None]],
    ]:
        annotator_set = "annotator_id" in payload.model_fields_set
        reviewer_set = "reviewer_id" in payload.model_fields_set
        assignment_targets: list[tuple[str, uuid.UUID]] = []
        if annotator_set and payload.annotator_id is not None:
            assignment_targets.append(("annotator", payload.annotator_id))
        if reviewer_set and payload.reviewer_id is not None:
            assignment_targets.append(("reviewer", payload.reviewer_id))
        await BatchService(self.db).lock_and_validate_assignment_targets(
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
                # starting a review round. Accept reviewer assignment only
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
        plan = DataManagerTaskAssignmentResponse(
            task_ids=payload.task_ids,
            preview_version=version,
            eligible_count=eligible,
            skipped_count=skipped,
            failed_count=failed,
            items=items,
        )
        return plan, loaded

    async def preview_assignment(
        self,
        project_id: uuid.UUID,
        payload: DataManagerTaskAssignmentRequest,
        *,
        actor: User | IdentityActorFacts,
    ) -> DataManagerTaskAssignmentResponse:
        _, access = await resolve_project_access_by_id(
            self.db, actor_id=actor.id, project_id=project_id
        )
        if not access.is_manager:
            raise HTTPException(status_code=403, detail="项目管理权限已变化")
        plan, _ = await self._plan(project_id, payload)
        return plan

    async def apply_assignment(
        self,
        project_id: uuid.UUID,
        payload: DataManagerTaskAssignmentApplyRequest,
        *,
        actor: User | IdentityActorFacts,
    ) -> DataManagerTaskAssignmentResponse:
        await lock_project_manager(self.db, actor.id, project_id)
        plan, loaded = await self._plan(project_id, payload, for_update=True)
        if plan.preview_version != payload.preview_version:
            raise _stale_preview()

        annotator_set = "annotator_id" in payload.model_fields_set
        reviewer_set = "reviewer_id" in payload.model_fields_set
        lock_service = TaskLockService(self.db)
        succeeded: list[uuid.UUID] = []
        assignments: list[TaskAssignment] = []
        for item in plan.items:
            if not item.will_change:
                continue
            task = loaded[item.task_id][0]
            # No editor, including the actor, may retain stale assignment.
            # Try the canonical fence rather than waiting task-row -> advisory.
            try:
                await lock_service.assert_final_write_available(
                    task.id, actor.id, allow_own=False
                )
            except HTTPException as exc:
                if (
                    exc.status_code != 409
                    or not isinstance(exc.detail, dict)
                    or exc.detail.get("reason")
                    not in {"task_busy", "task_locked_for_editing"}
                ):
                    raise
                item.will_change = False
                item.reason = "task_locked"
                continue
            assignments.append(
                TaskAssignment(
                    task_id=task.id,
                    project_id=task.project_id,
                    batch_id=task.batch_id,
                    expected_status=task.status,
                    expected_assignee_id=task.assignee_id,
                    expected_assignee_is_override=task.assignee_is_override,
                    expected_reviewer_id=task.reviewer_id,
                    expected_reviewer_is_override=task.reviewer_is_override,
                    expected_reviewer_claimed_at=task.reviewer_claimed_at,
                    annotator_set=annotator_set,
                    annotator_id=payload.annotator_id,
                    assigned_at=(
                        datetime.now(timezone.utc)
                        if annotator_set and payload.annotator_id is not None
                        else None
                    ),
                    reviewer_set=reviewer_set,
                    reviewer_id=payload.reviewer_id,
                )
            )
            succeeded.append(task.id)
        if assignments:
            await execute_batch_control(
                self.db,
                ApplyTaskAssignments(
                    schema="aap-labeling-batch-control/v1",
                    operation="apply_task_assignments",
                    assignments=assignments,
                ),
            )
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
