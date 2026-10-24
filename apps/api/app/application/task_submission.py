from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_errors import TaskWorkflowError
from app.services.project_access import get_workspace_project
from app.application.task_workflow_locks import (
    lock_current_batch_target,
    lock_current_task_target,
    lock_rows_nowait,
)
from app.db.enums import BatchStatus
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.mask_qc import MaskQCRun
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.schemas.mask_qc import MaskQCRunRequest
from app.services.annotation_evidence import task_contributor_snapshot
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.batch import BatchService
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_tasks import LabelingTaskService
from app.services.mask_qc.config import load_mask_qc_config
from aap_workspace_contract.access import ProjectAccess
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services import task_access

_SUBMIT_REVIEW_CHUNK_SIZE = 200


def _assert_batch_submit_authority(
    actor: IdentityActorFacts, access: ProjectAccess, batch: TaskBatch
) -> bool:
    actor_is_owner = access.is_manager
    actor_is_assigned_annotator = (
        access.project_role == "annotator"
        and batch.annotator_id is not None
        and batch.annotator_id == actor.id
    )
    if not (actor_is_owner or actor_is_assigned_annotator):
        raise TaskWorkflowError(
            403,
            "only the assigned annotator or a project owner can submit the batch",
        )
    return actor_is_owner


def _assert_batch_submittable(batch: TaskBatch) -> None:
    if batch.status not in (BatchStatus.ANNOTATING, BatchStatus.REVIEWING):
        raise TaskWorkflowError(
            409,
            {
                "reason": "batch_not_submittable",
                "status": batch.status,
                "message": "只有「标注中」或「审核中」的批次可以整批送审",
            },
        )


async def _prepare_submission(
    db: AsyncSession, task: Task, *, actor: IdentityActorFacts, now: datetime
) -> dict[str, Any]:
    """Apply shared labeling transition, quality preparation and evidence freeze."""
    labeling = LabelingTaskService(db)
    review_round_id = await labeling.submit_task(task, actor_id=actor.id, now=now)

    mask_qc_run: MaskQCRun | None = None
    mask_qc_job: AsyncJob | None = None
    mask_qc_created = False
    mask_qc_status: str | None = None
    from app.services.mask_qc.service import MaskQCError, create_mask_qc_run

    project = await get_workspace_project(db, project_id=task.project_id)
    config = load_mask_qc_config(project.mask_qc_config if project else None)
    has_mask = (
        await db.execute(
            select(Annotation.id)
            .where(
                Annotation.task_id == task.id,
                Annotation.is_active.is_(True),
                Annotation.was_cancelled.is_(False),
                Annotation.geometry["type"].astext.in_(
                    ("raster_mask", "video_track_mask")
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None
    if (
        project is not None
        and has_mask
        and config.enabled
        and config.auto_run == "on_review_submit"
    ):
        try:
            mask_qc_run, mask_qc_job, mask_qc_created = await create_mask_qc_run(
                db,
                project=project,
                actor_id=actor.id,
                request=MaskQCRunRequest(scope="task_ids", task_ids=[task.id]),
            )
        except MaskQCError as exc:
            mask_qc_status = str(exc.detail.get("reason") or "enqueue_failed")
        else:
            mask_qc_status = mask_qc_run.status

    contributor_ids = await task_contributor_snapshot(db, task)
    await labeling.capture_first_review_contributor_snapshot(
        task, actor_id=actor.id, contributor_ids=contributor_ids
    )
    await labeling.freeze_review_contributor_evidence(
        task,
        submitter_id=actor.id,
        contributor_ids=contributor_ids,
    )
    return {
        "review_round_id": review_round_id,
        "contributor_ids": contributor_ids,
        "review_contributor_ids": task.review_contributor_ids,
        "review_submitter_id": task.review_submitter_id,
        "mask_qc_run": mask_qc_run,
        "mask_qc_job": mask_qc_job,
        "mask_qc_created": mask_qc_created,
        "mask_qc_status": mask_qc_status,
        "has_mask": has_mask,
    }


class TaskSubmissionWorkflow:
    """Compose labeling submission with audit, counters and post-commit QC work."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def submit_task(
        self,
        task_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        access: ProjectAccess,
        origin: AuditOriginContext,
        quality_dispatcher: QualityJobDispatcher,
    ) -> dict[str, Any]:
        task, actor, _project, access = await lock_current_task_target(
            self.db,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=access,
        )
        await task_access.assert_task_visible(self.db, task, actor.id, access=access)
        task_access.assert_effective_task_assignee(
            actor.id,
            await task_access.resolve_effective_task_assignee_id(self.db, task),
            action="submit",
            project_id=task.project_id,
            allow_open_pool=True,
            access=access,
        )
        if task.status not in ("pending", "in_progress"):
            raise TaskWorkflowError(
                409,
                {"reason": "task_not_submittable", "status": task.status},
            )
        task_access.assert_task_editable(task, actor.id, access=access)

        result = await _prepare_submission(
            self.db, task, actor=actor, now=datetime.now(timezone.utc)
        )
        batch_service = BatchService(self.db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)

        mask_qc_run = result["mask_qc_run"]
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.TASK_SUBMIT,
            target_type="task",
            target_id=str(task_id),
            origin=origin,
            status_code=200,
            detail={
                "project_id": str(task.project_id),
                "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                "contributor_ids": result["contributor_ids"],
                "review_contributor_ids": result["review_contributor_ids"],
                "review_submitter_id": (
                    str(result["review_submitter_id"])
                    if result["review_submitter_id"]
                    else None
                ),
                "review_round_id": str(result["review_round_id"]),
                "result": "submitted",
                "mask_qc_run_id": str(mask_qc_run.id) if mask_qc_run else None,
                "mask_qc_status": result["mask_qc_status"],
            },
        )
        await self.db.commit()
        mask_qc_status = result["mask_qc_status"] if result["has_mask"] else None
        if (
            result["mask_qc_created"]
            and mask_qc_run is not None
            and result["mask_qc_job"] is not None
        ):
            from app.services.mask_qc.service import MaskQCError, dispatch_mask_qc_run

            try:
                await dispatch_mask_qc_run(
                    self.db,
                    run_id=mask_qc_run.id,
                    async_job_id=result["mask_qc_job"].id,
                    dispatcher=quality_dispatcher,
                )
            except MaskQCError:
                mask_qc_status = "failed"
        return {
            "status": "submitted",
            "task_id": str(task_id),
            "mask_qc_run_id": str(mask_qc_run.id) if mask_qc_run else None,
            "mask_qc_status": mask_qc_status,
        }

    async def submit_batch_review(
        self,
        project_id: uuid.UUID,
        batch_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        access: ProjectAccess,
        origin: AuditOriginContext,
        quality_dispatcher: QualityJobDispatcher,
        chunk_size: int = _SUBMIT_REVIEW_CHUNK_SIZE,
    ) -> dict[str, Any]:
        batch_service = BatchService(self.db)
        actor, _project, access, batch = await lock_current_batch_target(
            self.db,
            actor_id=actor_id,
            project_id=project_id,
            batch_id=batch_id,
            expected_access=access,
        )
        actor_is_owner = _assert_batch_submit_authority(actor, access, batch)
        _assert_batch_submittable(batch)

        before_status = batch.status
        candidate_ids = [
            row[0]
            for row in (
                await self.db.execute(
                    select(Task.id)
                    .where(
                        Task.project_id == project_id,
                        Task.batch_id == batch_id,
                        Task.status.in_(("pending", "in_progress")),
                    )
                    .order_by(Task.created_at, Task.id)
                )
            ).all()
        ]
        now = datetime.now(timezone.utc)
        submitted = 0
        for chunk_index, start in enumerate(range(0, len(candidate_ids), chunk_size)):
            if chunk_index > 0:
                actor, _project, access, batch = await lock_current_batch_target(
                    self.db,
                    actor_id=actor_id,
                    project_id=project_id,
                    batch_id=batch_id,
                    expected_access=access,
                )
                actor_is_owner = _assert_batch_submit_authority(actor, access, batch)
                _assert_batch_submittable(batch)

            chunk_ids = candidate_ids[start : start + chunk_size]
            tasks = await lock_rows_nowait(
                self.db,
                select(Task)
                .where(
                    Task.id.in_(chunk_ids),
                    Task.project_id == project_id,
                    Task.batch_id == batch_id,
                )
                .order_by(Task.created_at, Task.id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True),
                reason="task_busy",
            )
            quality_dispatches: list[tuple[uuid.UUID, uuid.UUID]] = []
            for task in tasks:
                if task.status not in ("pending", "in_progress"):
                    continue
                if (
                    not actor_is_owner
                    and task.assignee_id is not None
                    and task.assignee_id != actor.id
                ):
                    continue

                result = await _prepare_submission(self.db, task, actor=actor, now=now)
                mask_qc_run = result["mask_qc_run"]
                await AuditService.log(
                    self.db,
                    actor=actor,
                    action=AuditAction.TASK_SUBMIT,
                    target_type="task",
                    target_id=str(task.id),
                    origin=origin,
                    status_code=200,
                    detail={
                        "project_id": str(task.project_id),
                        "batch_id": str(batch_id),
                        "assignee_id": str(task.assignee_id)
                        if task.assignee_id
                        else None,
                        "contributor_ids": result["contributor_ids"],
                        "review_contributor_ids": result["review_contributor_ids"],
                        "review_submitter_id": (
                            str(result["review_submitter_id"])
                            if result["review_submitter_id"]
                            else None
                        ),
                        "review_round_id": str(result["review_round_id"]),
                        "result": "submitted",
                        "trigger": "batch_submit_review",
                        "mask_qc_run_id": str(mask_qc_run.id) if mask_qc_run else None,
                        "mask_qc_status": result["mask_qc_status"],
                    },
                )
                submitted += 1
                if (
                    result["mask_qc_created"]
                    and mask_qc_run is not None
                    and result["mask_qc_job"] is not None
                ):
                    quality_dispatches.append(
                        (mask_qc_run.id, result["mask_qc_job"].id)
                    )

            await self.db.commit()
            if quality_dispatches:
                from app.services.mask_qc.service import (
                    MaskQCError,
                    dispatch_mask_qc_run,
                )

                for run_id, job_id in quality_dispatches:
                    try:
                        await dispatch_mask_qc_run(
                            self.db,
                            run_id=run_id,
                            async_job_id=job_id,
                            dispatcher=quality_dispatcher,
                        )
                    except MaskQCError:
                        continue

        actor, _project, access, batch = await lock_current_batch_target(
            self.db,
            actor_id=actor_id,
            project_id=project_id,
            batch_id=batch_id,
            expected_access=access,
        )
        _assert_batch_submit_authority(actor, access, batch)
        _assert_batch_submittable(batch)
        await batch_service.check_auto_transitions(batch_id)
        await batch_service.recalculate_counters(batch_id)
        await self.db.refresh(batch)
        if batch.status != before_status:
            await AuditService.log(
                self.db,
                actor=actor,
                action=AuditAction.BATCH_STATUS_CHANGED,
                target_type="batch",
                target_id=str(batch_id),
                origin=origin,
                status_code=200,
                detail={
                    "before": before_status,
                    "after": batch.status,
                    "submitted_tasks": submitted,
                    "trigger": "batch_submit_review",
                },
            )
        await self.db.commit()
        await self.db.refresh(batch)
        remaining = (
            await self.db.scalar(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.batch_id == batch_id,
                    Task.status.in_(("pending", "in_progress")),
                )
            )
        ) or 0
        return {
            "batch_id": batch_id,
            "status": batch.status,
            "submitted_tasks": submitted,
            "skipped_tasks": max(0, len(candidate_ids) - submitted),
            "remaining_tasks": int(remaining),
        }
