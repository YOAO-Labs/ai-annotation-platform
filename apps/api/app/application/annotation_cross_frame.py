"""Bind cross-frame annotation commands to the caller's current transaction."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.annotation import (
    AnnotationOut,
    InterpolateRangeRequest,
    InterpolateRangeResponse,
    PropagateBatchItem,
    PropagateBatchRequest,
    PropagateBatchResponse,
    PropagateRequest,
    PropagateResponse,
)
from app.application.task_workflow_errors import TaskWorkflowError
from app.application.task_workflow_locks import lock_current_task_target
from app.services.annotation import AnnotationService
from app.services.annotation_cross_frame_contract import (
    AnnotationCrossFramePort,
    CrossFrameBatchStager,
)
from app.services.annotation_mutation_contract import AnnotationMutationError
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services import task_access
from app.services.task_lock import TaskLockService
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    task_row,
)
from aap_workspace_contract.access import ProjectAccess
from app.services.identity_contracts import IdentityActorFacts


async def _load_task_or_404(db: AsyncSession, task_id: UUID) -> Task:
    task = await get_core_row(db, task_row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _refresh_annotation(db: AsyncSession, annotation: Annotation) -> None:
    """Refresh a committed annotation through Labeling's public row relation."""
    db.expire(annotation)
    await get_core_row(db, annotation_row, annotation.id)


class _AnnotationCrossFrameSqlAdapter(AnnotationCrossFramePort):
    """Bind the already-authorized source task and current actor to one Session."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        source_task: Task,
        actor: User | IdentityActorFacts,
        access: ProjectAccess,
    ) -> None:
        self._db = db
        self._source_task = source_task
        self._actor = actor
        self._access = access
        self._annotations = AnnotationService(db)

    def _assert_bound(self, task_id: UUID, actor_id: UUID) -> None:
        if task_id != self._source_task.id or actor_id != self._actor.id:
            raise ValueError("cross-frame command identity does not match binding")

    async def _lock_current_tasks_for_write(
        self, task_ids: set[UUID]
    ) -> dict[UUID, Task]:
        """Recheck cross-frame authority against ordered, freshly locked rows."""
        locked: dict[UUID, Task] = {}
        for task_id in sorted(task_ids, key=str):
            task, actor, _project, access = await lock_current_task_target(
                self._db,
                task_id=task_id,
                actor_id=self._actor.id,
                expected_access=self._access,
            )
            await task_access.assert_task_visible(
                self._db, task, actor.id, access=access
            )
            task_access.assert_annotation_write_allowed(task, access)
            task_access.assert_task_editable(task, actor.id, access=access)
            await task_access.assert_review_adjustment_evidence(
                self._db, task, actor.id, access
            )
            await TaskLockService(self._db).assert_final_write_available(
                task.id, actor.id, allow_own=True
            )
            locked[task.id] = task
        return locked

    async def _target_task_for_write(self, target_task_id: UUID) -> Task:
        target_task = await _load_task_or_404(self._db, target_task_id)
        target_access = await task_access.resolve_task_access(
            self._db, target_task, self._actor.id
        )
        await task_access.assert_task_visible(
            self._db, target_task, self._actor.id, access=target_access
        )
        task_access.assert_task_editable(
            target_task, self._actor.id, access=target_access
        )
        return target_task

    async def propagate(
        self,
        *,
        source_task_id: UUID,
        annotation_id: UUID,
        actor_id: UUID,
        payload: PropagateRequest,
        audit_origin: AuditOriginContext,
    ) -> PropagateResponse:
        self._assert_bound(source_task_id, actor_id)
        try:
            source_annotation = await get_core_row(
                self._db, annotation_row, annotation_id
            )
            if (
                source_annotation is None
                or source_annotation.task_id != self._source_task.id
            ):
                raise HTTPException(
                    status_code=404, detail="source annotation not found"
                )

            target_task = await self._target_task_for_write(payload.target_task_id)
            if target_task.project_id != self._source_task.project_id:
                raise HTTPException(
                    status_code=422, detail="跨 project propagate 不被允许"
                )
            locked_tasks = await self._lock_current_tasks_for_write(
                {self._source_task.id, target_task.id}
            )
            target_task = locked_tasks[target_task.id]
            new_annotation, motion_compensated = await self._annotations._propagate(
                source_annotation_id=annotation_id,
                target_task_id=target_task.id,
                user_id=self._actor.id,
                override_psr=payload.override_psr,
            )
            await TaskLockService(self._db).heartbeat(target_task.id, self._actor.id)
            await AuditService.log(
                self._db,
                actor=self._actor,
                action=AuditAction.ANNOTATION_CREATE,
                target_type="annotation",
                target_id=str(new_annotation.id),
                origin=audit_origin,
                status_code=201,
                detail={
                    "task_id": str(target_task.id),
                    "source_task_id": str(self._source_task.id),
                    "source_annotation_id": str(annotation_id),
                    "propagated": True,
                    "motion_compensated": motion_compensated,
                    "track_id": new_annotation.track_id,
                    "class_name": new_annotation.class_name,
                },
            )
            await self._db.commit()
            await _refresh_annotation(self._db, new_annotation)
            return PropagateResponse(
                annotation=AnnotationOut.model_validate(new_annotation),
                motion_compensated=motion_compensated,
            )
        except (HTTPException, TaskWorkflowError) as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers if isinstance(exc, HTTPException) else None,
            ) from exc

    async def propagate_batch(
        self,
        *,
        source_task_id: UUID,
        actor_id: UUID,
        payload: PropagateBatchRequest,
        audit_origin: AuditOriginContext,
    ) -> PropagateBatchResponse:
        self._assert_bound(source_task_id, actor_id)
        try:
            target_task = await self._target_task_for_write(payload.target_task_id)
            if target_task.project_id != self._source_task.project_id:
                raise HTTPException(
                    status_code=422, detail="跨 project propagate 不被允许"
                )
            locked_tasks = await self._lock_current_tasks_for_write(
                {self._source_task.id, target_task.id}
            )
            target_task = locked_tasks[target_task.id]
            results, motion_compensated = await self._annotations._propagate_batch(
                source_task_id=self._source_task.id,
                target_task_id=target_task.id,
                annotation_ids=payload.annotation_ids,
                user_id=self._actor.id,
            )
            await TaskLockService(self._db).heartbeat(target_task.id, self._actor.id)
            await AuditService.log(
                self._db,
                actor=self._actor,
                action=AuditAction.ANNOTATION_CREATE,
                target_type="task",
                target_id=target_task.id,
                origin=audit_origin,
                status_code=201,
                detail={
                    "source_task_id": str(self._source_task.id),
                    "propagated_batch": True,
                    "motion_compensated": motion_compensated,
                    "count": len(results),
                    "created_annotation_ids": [str(ann.id) for _, ann in results],
                },
            )
            await self._db.commit()
            for _, annotation in results:
                await _refresh_annotation(self._db, annotation)
            return PropagateBatchResponse(
                items=[
                    PropagateBatchItem(
                        source_annotation_id=source_annotation_id,
                        annotation=AnnotationOut.model_validate(annotation),
                    )
                    for source_annotation_id, annotation in results
                ],
                motion_compensated=motion_compensated,
            )
        except (HTTPException, TaskWorkflowError) as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers if isinstance(exc, HTTPException) else None,
            ) from exc

    async def interpolate_range(
        self,
        *,
        from_task_id: UUID,
        actor_id: UUID,
        payload: InterpolateRangeRequest,
        audit_origin: AuditOriginContext,
    ) -> InterpolateRangeResponse:
        self._assert_bound(from_task_id, actor_id)
        try:
            target_task = await self._target_task_for_write(payload.to_task_id)
            if target_task.project_id != self._source_task.project_id:
                raise HTTPException(status_code=422, detail="跨 project 插值不被允许")
            locked_tasks = await self._lock_current_tasks_for_write(
                {self._source_task.id, target_task.id}
            )
            target_task = locked_tasks[target_task.id]

            async def assert_middle_task_visible(task: Task) -> None:
                await task_access.assert_task_visible(
                    self._db,
                    task,
                    self._actor.id,
                    access=self._access,
                )

            def assert_middle_task_editable(task: Task) -> None:
                task_access.assert_task_editable(
                    task,
                    self._actor.id,
                    access=self._access,
                )

            async def lock_middle_task_for_write(task: Task) -> Task:
                return (await self._lock_current_tasks_for_write({task.id}))[task.id]

            (
                created,
                motion_compensated,
                skipped_frames,
            ) = await self._annotations._interpolate_range(
                track_id=payload.track_id,
                from_task_id=self._source_task.id,
                to_task_id=target_task.id,
                user_id=self._actor.id,
                assert_task_editable=assert_middle_task_editable,
                assert_task_visible=assert_middle_task_visible,
                lock_task_for_write=lock_middle_task_for_write,
            )
            await AuditService.log(
                self._db,
                actor=self._actor,
                action=AuditAction.ANNOTATION_CREATE,
                target_type="task",
                target_id=self._source_task.id,
                origin=audit_origin,
                status_code=201,
                detail={
                    "interpolate_range": True,
                    "track_id": payload.track_id,
                    "to_task_id": str(target_task.id),
                    "motion_compensated": motion_compensated,
                    "created": len(created),
                    "created_annotation_ids": [
                        str(annotation.id) for annotation in created
                    ],
                    "skipped_frames": skipped_frames,
                },
            )
            await self._db.commit()
            for annotation in created:
                await _refresh_annotation(self._db, annotation)
            return InterpolateRangeResponse(
                annotations=[
                    AnnotationOut.model_validate(annotation) for annotation in created
                ],
                motion_compensated=motion_compensated,
                skipped_frames=skipped_frames,
            )
        except (HTTPException, TaskWorkflowError) as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers if isinstance(exc, HTTPException) else None,
            ) from exc


class _CrossFrameBatchSqlStager(CrossFrameBatchStager):
    """Bind the worker's per-target staging step to its current transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def stage_batch(
        self,
        *,
        source_task_id: UUID,
        target_task_id: UUID,
        annotation_ids: list[UUID],
        actor_id: UUID,
    ) -> int:
        try:
            results, _motion_compensated = await AnnotationService(
                self._db
            )._propagate_batch(
                source_task_id=source_task_id,
                target_task_id=target_task_id,
                annotation_ids=annotation_ids,
                user_id=actor_id,
            )
            return len(results)
        except HTTPException as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc


def bind_cross_frame_batch_stager(db: AsyncSession) -> CrossFrameBatchStager:
    """Bind the worker's batch staging port to its existing Session."""
    return _CrossFrameBatchSqlStager(db)
