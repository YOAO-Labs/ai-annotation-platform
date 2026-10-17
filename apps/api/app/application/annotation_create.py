"""Transactional annotation create command, including its historical receipt."""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.annotation import AnnotationCreate, AnnotationOut
from app.services.annotation import (
    AnnotationService,
    annotation_create_request_digest,
)
from app.services.audit import AuditAction, AuditService
from app.services.video_collaboration import heartbeat_task_lock_for_legacy_video


class AnnotationCreateWorkflow:
    """Own create-receipt semantics and the existing annotation write ordering."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.annotations = AnnotationService(db)

    async def create(
        self,
        *,
        task: Task,
        data: AnnotationCreate,
        actor: User,
        request: Request,
        idempotency_key: str | None,
    ) -> Annotation | AnnotationOut:
        request_digest: str | None = None
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 128:
                raise HTTPException(
                    status_code=400,
                    detail="Idempotency-Key must contain 1 to 128 characters",
                )
            request_digest = annotation_create_request_digest(
                data.model_dump(mode="json")
            )
            response_json = await self.annotations.annotation_create_receipt_response(
                task_id=task.id,
                actor_id=actor.id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if response_json is not None:
                return AnnotationOut.model_validate(response_json)

        annotation = await self.annotations.create(
            task_id=task.id,
            user_id=actor.id,
            annotation_type=data.annotation_type,
            tool_unit_id=data.tool_unit_id,
            class_name=data.class_name,
            geometry=data.geometry.model_dump(),
            confidence=data.confidence,
            parent_prediction_id=data.parent_prediction_id,
            parent_annotation_id=data.parent_annotation_id,
            lead_time=data.lead_time,
            attributes=data.attributes,
            video_segment_id=data.video_segment_id,
        )
        await heartbeat_task_lock_for_legacy_video(self.db, task, actor.id)

        if idempotency_key is not None:
            # AnnotationService.create flushes the annotation and task stats;
            # refresh server defaults before persisting the historical response.
            await self.db.refresh(annotation)
            annotation_response = AnnotationOut.model_validate(annotation)
            self.annotations.record_annotation_create_receipt(
                task_id=task.id,
                actor_id=actor.id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response_json=annotation_response.model_dump(mode="json"),
            )

        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.ANNOTATION_CREATE,
            target_type="annotation",
            target_id=str(annotation.id),
            request=request,
            status_code=201,
            detail={
                "task_id": str(task.id),
                "class_name": annotation.class_name,
                "annotation_type": annotation.annotation_type,
                "source": annotation.source,
            },
        )
        await self.db.commit()
        await self.db.refresh(annotation)
        return annotation
