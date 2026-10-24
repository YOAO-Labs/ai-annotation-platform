"""Bind task-scoped annotation mutations to the caller's existing transaction."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.annotation import AnnotationBulkUpdateResponse, AnnotationOut
from app.services import task_access
from app.services.annotation import (
    AnnotationService,
    validate_geometry_type_transition,
)
from app.services.annotation_evidence import record_annotation_actor
from app.services.annotation_mutation_contract import (
    AnnotationBulkUpdateCommand,
    AnnotationBulkUpdatePort,
    AnnotationDeleteCommand,
    AnnotationMutationError,
    AnnotationMutationPort,
    AnnotationUpdateCommand,
)
from app.services.audit import AuditAction, AuditService
from app.services.raster_mask_storage import (
    _VerifiedMaskContent,
    RasterMaskContractError,
    prepare_mask_geometry_for_annotation_write,
)
from app.services.video_collaboration import (
    assert_video_annotation_write_scope,
    heartbeat_task_lock_for_legacy_video,
)
from aap_workspace_contract.access import ProjectAccess
from app.services.identity_contracts import IdentityActorFacts


def validate_annotation_update_preconditions(
    existing: Annotation | None,
    *,
    task_id: UUID,
    fields: dict,
    if_match: str | None,
) -> int | None:
    """Reject invalid updates before object I/O and repeat before persistence."""
    if existing is None or not existing.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if existing.task_id != task_id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    if "geometry" in fields and existing.is_locked:
        raise HTTPException(status_code=409, detail={"reason": "annotation_locked"})

    header = if_match.strip() if if_match is not None else ""
    expected_version = None
    geometry = fields.get("geometry")
    if "geometry" in fields:
        previous_type = str((existing.geometry or {}).get("type") or "")
        proposed_type = str((geometry or {}).get("type") or "")
        requires_precondition = previous_type != proposed_type or proposed_type in {
            "raster_mask",
            "video_mask",
        }
        if requires_precondition and not header:
            raise HTTPException(status_code=428, detail={"reason": "if_match_required"})
    if header:
        try:
            expected_version = int(header.removeprefix('W/"').removesuffix('"'))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid If-Match format")
        if existing.version != expected_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "version_mismatch",
                    "current_version": existing.version,
                },
            )
    if isinstance(geometry, dict):
        validate_geometry_type_transition(
            tool_unit_id=existing.tool_unit_id,
            previous_geometry=existing.geometry,
            next_geometry=geometry,
        )
    return expected_version


async def _load_single_task_for_ids(
    db: AsyncSession,
    ids: list[UUID],
) -> Task:
    """所有 ids 必须属于同一 task; 否则 422.
    返回该 task (供 task_access.assert_task_editable 与 project_visible 校验)."""
    if not ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")
    rows = (
        await db.execute(
            select(Annotation.id, Annotation.task_id).where(Annotation.id.in_(ids))
        )
    ).all()
    if len(rows) != len(ids):
        missing = set(ids) - {r.id for r in rows}
        raise HTTPException(
            status_code=404,
            detail=f"annotations not found: {sorted(str(m) for m in missing)}",
        )
    task_ids = {r.task_id for r in rows}
    if len(task_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail="bulk operation requires all annotations belong to a single task",
        )
    task_id = next(iter(task_ids))
    # Lock the task row before any capability/evidence check so the decision is
    # made on the current row and concurrent writers are serialized (the bulk
    # UPDATE itself also takes this lock later).
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return task


class _AnnotationBulkUpdateSqlAdapter(AnnotationBulkUpdatePort):
    """Bind the legacy bulk command to its current actor and write transaction."""

    def __init__(self, db: AsyncSession, *, actor: User | IdentityActorFacts) -> None:
        self._db = db
        self._actor = actor

    async def update(
        self, command: AnnotationBulkUpdateCommand
    ) -> AnnotationBulkUpdateResponse:
        if command.actor_id != self._actor.id:
            raise ValueError("annotation bulk update actor does not match binding")
        try:
            return await self._update(command)
        except HTTPException as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc

    async def _update(
        self, command: AnnotationBulkUpdateCommand
    ) -> AnnotationBulkUpdateResponse:
        payload = command.payload
        task = await _load_single_task_for_ids(self._db, payload.ids)
        access = await task_access.resolve_task_access(
            self._db, task, self._actor.id, lock_membership=True
        )
        task_access.assert_annotation_write_allowed(task, access)
        await task_access.assert_review_adjustment_evidence(
            self._db, task, self._actor.id, access
        )
        await task_access.assert_task_visible(
            self._db, task, self._actor.id, access=access
        )
        task_access.assert_task_editable(task, self._actor.id, access=access)

        await record_annotation_actor(self._db, task, self._actor.id)
        updated = await AnnotationService(self._db).bulk_update(
            payload.ids,
            class_name=payload.patch.class_name,
            attributes=payload.patch.attributes,
            z_order=payload.patch.z_order,
            is_locked=payload.patch.is_locked,
            is_hidden=payload.patch.is_hidden,
            actor_id=self._actor.id,
            task_id=task.id,
            project_id=task.project_id,
        )
        await AuditService.log(
            self._db,
            actor=self._actor,
            action=AuditAction.ANNOTATION_BULK_UPDATE,
            target_type="task",
            target_id=task.id,
            origin=command.audit_origin,
            status_code=200,
            detail={
                "annotation_ids": [str(item) for item in payload.ids],
                "patch": payload.patch.model_dump(exclude_none=True),
                "count": len(updated),
            },
        )
        await self._db.commit()
        return AnnotationBulkUpdateResponse(
            updated_ids=[annotation.id for annotation in updated],
            updated_count=len(updated),
        )


class _AnnotationMutationSqlAdapter(AnnotationMutationPort):
    """Keep current task/actor rows attached to the original write transaction."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        task: Task,
        actor: User | IdentityActorFacts,
        access: ProjectAccess,
        verified_mask_content: _VerifiedMaskContent | None = None,
    ) -> None:
        self._db = db
        self._task = task
        self._actor = actor
        self._access = access
        self._verified_mask_content = verified_mask_content
        self._annotations = AnnotationService(db)

    def _assert_bound(self, task_id, actor_id) -> None:
        if task_id != self._task.id or actor_id != self._actor.id:
            raise ValueError(
                "annotation mutation command identity does not match binding"
            )

    async def update(self, command: AnnotationUpdateCommand) -> AnnotationOut:
        self._assert_bound(command.task_id, command.actor_id)
        try:
            return await self._update(command)
        except RasterMaskContractError as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code, detail=exc.detail
            ) from exc
        except HTTPException as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc

    async def _update(self, command: AnnotationUpdateCommand) -> AnnotationOut:
        task = self._task
        actor = self._actor
        data = command.data
        fields = data.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")

        # The task lock was acquired by HTTP before this initial annotation read.
        existing = (
            await self._db.execute(
                select(Annotation).where(Annotation.id == command.annotation_id)
            )
        ).scalar_one_or_none()
        if existing is None or not existing.is_active:
            raise HTTPException(status_code=404, detail="Annotation not found")
        if existing.task_id != command.task_id:
            raise HTTPException(
                status_code=400, detail="Annotation does not belong to this task"
            )
        await assert_video_annotation_write_scope(
            self._db,
            task=task,
            user=actor,
            segment_id=existing.video_segment_id,
            geometry=fields.get("geometry", existing.geometry),
        )
        # Keep both version checks before mask IO and after the annotation row lock.
        expected_version = validate_annotation_update_preconditions(
            existing,
            task_id=command.task_id,
            fields=fields,
            if_match=command.if_match,
        )

        mask_payload_prepared = False
        next_geometry = fields.get("geometry")
        if isinstance(next_geometry, dict) and next_geometry.get("type") in {
            "raster_mask",
            "video_mask",
            "video_track_mask",
        }:
            await prepare_mask_geometry_for_annotation_write(
                self._db,
                task,
                next_geometry,
                verified_content=self._verified_mask_content,
                require_verified_content=True,
            )
            mask_payload_prepared = True

        existing = (
            await self._db.execute(
                select(Annotation)
                .where(Annotation.id == command.annotation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            existing is None
            or not existing.is_active
            or existing.task_id != command.task_id
        ):
            raise HTTPException(status_code=404, detail="Annotation not found")
        if "geometry" in fields and existing.is_locked:
            raise HTTPException(
                status_code=409,
                detail={"reason": "annotation_locked"},
            )

        before_attributes: dict | None = None
        if "attributes" in fields:
            before_attributes = dict(existing.attributes or {})
        if expected_version is not None and existing.version != expected_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "version_mismatch",
                    "current_version": existing.version,
                },
            )

        annotation = await self._annotations.update(
            command.annotation_id,
            mask_payload_prepared=mask_payload_prepared,
            actor_id=actor.id,
            **fields,
        )
        if annotation is None:
            raise HTTPException(status_code=404, detail="Annotation not found")

        await record_annotation_actor(self._db, task, actor.id)
        await heartbeat_task_lock_for_legacy_video(self._db, task, actor.id)
        audit_action = (
            AuditAction.TASK_REVIEWER_EDIT
            if task.status == "review"
            and (self._access.is_manager or self._access.project_role == "reviewer")
            else AuditAction.ANNOTATION_UPDATE
        )
        await AuditService.log(
            self._db,
            actor=actor,
            action=audit_action,
            target_type="annotation",
            target_id=str(annotation.id),
            origin=command.audit_origin,
            status_code=200,
            detail={"task_id": str(command.task_id), "fields": list(fields.keys())},
        )
        if before_attributes is not None:
            after_attributes = dict(annotation.attributes or {})
            all_keys = set(before_attributes) | set(after_attributes)
            change_items = []
            for key in sorted(all_keys):
                before_value = before_attributes.get(key)
                after_value = after_attributes.get(key)
                if before_value == after_value:
                    continue
                change_items.append(
                    {
                        "target_id": str(annotation.id),
                        "detail": {
                            "task_id": str(command.task_id),
                            "field_key": key,
                            "before": before_value,
                            "after": after_value,
                        },
                    }
                )
            if change_items:
                await AuditService.log_many(
                    self._db,
                    actor=actor,
                    action=AuditAction.ANNOTATION_ATTRIBUTE_CHANGE,
                    target_type="annotation",
                    origin=command.audit_origin,
                    status_code=200,
                    items=change_items,
                )
        await self._db.commit()
        await self._db.refresh(annotation)
        return AnnotationOut.model_validate(annotation)

    async def delete(self, command: AnnotationDeleteCommand) -> bool:
        self._assert_bound(command.task_id, command.actor_id)
        try:
            task = self._task
            actor = self._actor
            pre = await self._db.get(Annotation, command.annotation_id)
            if pre is None or pre.task_id != command.task_id or not pre.is_active:
                raise HTTPException(status_code=404, detail="Annotation not found")
            if pre.is_locked:
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "annotation_locked"},
                )
            await assert_video_annotation_write_scope(
                self._db,
                task=task,
                user=actor,
                segment_id=pre.video_segment_id,
                geometry=pre.geometry,
            )
            pre_class = pre.class_name
            deleted = await self._annotations.delete(
                command.annotation_id, actor_id=actor.id
            )
            if not deleted:
                return False
            await record_annotation_actor(self._db, task, actor.id)
            await heartbeat_task_lock_for_legacy_video(self._db, task, actor.id)
            await AuditService.log(
                self._db,
                actor=actor,
                action=AuditAction.ANNOTATION_DELETE,
                target_type="annotation",
                target_id=str(command.annotation_id),
                origin=command.audit_origin,
                status_code=204,
                detail={
                    "task_id": str(command.task_id),
                    "soft": True,
                    "class_name": pre_class,
                },
            )
            await self._db.commit()
            return True
        except RasterMaskContractError as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code, detail=exc.detail
            ) from exc
        except HTTPException as exc:
            raise AnnotationMutationError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc
