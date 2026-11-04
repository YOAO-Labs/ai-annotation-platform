"""Restore the exact annotations belonging to one accepted prediction."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import _load_task_or_404, require_task_annotation_write
from app.deps import get_current_user, get_db, require_scopes
from app.services import acceptance_history, task_access
from app.services.annotation import AnnotationService
from app.services.annotation_evidence import record_annotation_actor
from app.services.audit import AuditAction, AuditService
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import lock_task_for_core_read
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    lock_raster_mask_references,
    verify_mask_payload_content,
)
from app.services.video_collaboration import (
    assert_video_annotation_write_scope,
    heartbeat_task_lock_for_legacy_video,
)
from yoao_workspace_contract.access import ProjectAccess

router = APIRouter()


class AcceptanceHistoryRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["before", "after"]
    expected_versions: dict[uuid.UUID, StrictInt]
    idempotency_key: str = Field(min_length=1, max_length=128)
    video_segment_id: uuid.UUID | None = None


class AcceptanceHistoryRestoreResponse(BaseModel):
    acceptance_key: str
    target: Literal["before", "after"]
    result_versions: dict[uuid.UUID, int]
    active_annotation_ids: list[uuid.UUID]
    restore_expires_at: datetime
    idempotent_replay: bool


@router.post(
    "/{task_id}/acceptance-history/{acceptance_key}/restore",
    response_model=AcceptanceHistoryRestoreResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def restore_acceptance_history(
    task_id: uuid.UUID,
    acceptance_key: str,
    payload: AcceptanceHistoryRestoreRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
) -> AcceptanceHistoryRestoreResponse:
    """Undo or redo one acceptance atomically, preserving its annotation identities."""
    acceptance_history.validate_key(acceptance_key)
    acceptance_history.validate_key(payload.idempotency_key)
    if not payload.expected_versions or any(
        version < 1 for version in payload.expected_versions.values()
    ):
        raise HTTPException(422, detail={"reason": "invalid_expected_versions"})
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    task_access.assert_annotation_write_allowed(task, access)
    await task_access.assert_review_adjustment_evidence(
        db, task, current_user.id, access
    )
    receipt = await acceptance_history.find(
        db, task_id, current_user.id, acceptance_key
    )
    if not receipt.found or receipt.kind != "accept_prediction":
        raise HTTPException(404, detail={"reason": "acceptance_not_found"})
    if (
        receipt.created_at is None
        or receipt.created_at + acceptance_history.SLICE_RESTORE_TTL
        <= datetime.now(timezone.utc)
    ):
        raise HTTPException(410, detail={"reason": "restore_expired"})
    report = receipt.report or {}
    snapshots = report.get(payload.target)
    if not isinstance(snapshots, dict) or not snapshots:
        raise HTTPException(409, detail={"reason": "snapshot_unavailable"})
    geometries = [snapshot["geometry"] for snapshot in snapshots.values()]
    try:
        verified = await verify_mask_payload_content(geometries, db=db)
        final_access = await task_access.resolve_task_access(
            db, task, current_user.id, lock_membership=True
        )
        task = await lock_task_for_core_read(
            db, task_id, lock_mode="update", nowait=False, populate_existing=True
        )
        if task is None:
            raise HTTPException(404, detail={"reason": "task_not_found"})
        await task_access.assert_task_visible(
            db, task, current_user.id, access=final_access
        )
        task_access.assert_task_editable(task, current_user.id, access=final_access)
        task_access.assert_annotation_write_allowed(task, final_access)
        await task_access.assert_review_adjustment_evidence(
            db, task, current_user.id, final_access
        )
        for geometry in geometries:
            await assert_video_annotation_write_scope(
                db,
                task=task,
                user=current_user,
                segment_id=payload.video_segment_id,
                geometry=geometry,
            )
        await lock_raster_mask_references(
            db,
            geometries,
            verify=False,
            content_epochs=dict(verified.content_epochs),
        )
        body = payload.model_dump(mode="json")
        result = await acceptance_history.command(
            db,
            task,
            current_user.id,
            "restore",
            {
                **body,
                "acceptance_key": acceptance_key,
                "request_digest": acceptance_history.digest(
                    {"acceptance_key": acceptance_key, **body}
                ),
            },
        )
        if not result["idempotent_replay"]:
            await record_annotation_actor(db, task, current_user.id)
            await AnnotationService(db)._update_task_stats(task_id)
            await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)
            await AuditService.log(
                db,
                actor=current_user,
                action=AuditAction.ANNOTATION_UPDATE,
                target_type="task",
                target_id=str(task_id),
                request=request,
                status_code=200,
                detail={"operation": "acceptance_restore", "target": payload.target},
            )
        await db.commit()
        return AcceptanceHistoryRestoreResponse.model_validate(result)
    except RasterMaskContractError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    except HTTPException:
        await db.rollback()
        raise
