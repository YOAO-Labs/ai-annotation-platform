"""Apply secondary inference and its HTTP-side effects in one caller transaction."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.annotation import AnnotationOut, SecondaryInferenceResponse
from app.services.annotation import AnnotationService
from app.services.annotation_mutation_contract import AnnotationMutationError
from app.services.annotation_secondary_inference_contract import (
    SecondaryInferenceWritePort,
)
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.labeling_write_scope import LabelingWriteScope
from app.services.secondary_inference_result import SecondaryInferenceResult
from app.services.video_collaboration import heartbeat_task_lock_for_legacy_video
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import annotation_row, refresh_core_row


class _AnnotationSecondaryInferenceSqlAdapter(SecondaryInferenceWritePort):
    """Bind the existing inference result to its pre-authorized rows/session."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        task: Task,
        annotation: Annotation,
        actor: User | IdentityActorFacts,
    ) -> None:
        self._db = db
        self._task = task
        self._annotation = annotation
        self._actor = actor
        self._annotations = AnnotationService(db)

    def _assert_bound(self, write_scope: LabelingWriteScope) -> None:
        if (
            write_scope.task_id != self._task.id
            or write_scope.project_id != self._task.project_id
            or write_scope.source_annotation_id != self._annotation.id
            or (
                write_scope.actor_id is not None
                and write_scope.actor_id != self._actor.id
            )
        ):
            raise ValueError(
                "secondary inference command identity does not match binding"
            )

    async def apply_secondary_inference(
        self,
        *,
        write_scope: LabelingWriteScope,
        result: SecondaryInferenceResult,
        write_target: Literal["attributes", "geometry"],
        audit_origin: AuditOriginContext,
    ) -> SecondaryInferenceResponse:
        self._assert_bound(write_scope)
        provenance = result.execution_provenance
        try:
            updated, children = await self._annotations._apply_secondary_inference(
                task=self._task,
                annotation=self._annotation,
                result=result,
                write_scope=write_scope,
            )
            await heartbeat_task_lock_for_legacy_video(
                self._db, self._task, self._actor.id
            )
            await AuditService.log(
                self._db,
                actor=self._actor,
                action=AuditAction.ANNOTATION_UPDATE,
                target_type="annotation",
                target_id=str(self._annotation.id),
                origin=audit_origin,
                status_code=201,
                detail={
                    "task_id": str(self._task.id),
                    "secondary_inference": True,
                    "ml_backend_id": (
                        str(provenance.instance_id) if provenance is not None else None
                    ),
                    "ml_backend_pool_id": (
                        str(provenance.pool_id)
                        if provenance is not None and provenance.pool_id is not None
                        else None
                    ),
                    "write_target": write_target,
                    "created_children": len(children),
                },
            )
            await self._db.commit()
            await refresh_core_row(self._db, annotation_row, updated)
            for child in children:
                await refresh_core_row(self._db, annotation_row, child)
            return SecondaryInferenceResponse(
                annotation=AnnotationOut.model_validate(updated),
                created_children=[
                    AnnotationOut.model_validate(child) for child in children
                ],
            )
        except HTTPException as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc
