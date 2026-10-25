import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import require_task_annotation_write
from app.application.mask_annotation_slices import _AnnotationSliceSqlAdapter
from app.deps import get_current_user, get_db, require_scopes
from app.schemas.annotation_slice import (
    AnnotationSliceResponse,
    AnnotationSliceRestoreRequest,
    PolygonSliceCommitRequest,
)
from app.services.audit import audit_origin_from_request
from app.services.annotation_slice_contracts import AnnotationSliceError
from aap_workspace_contract.access import ProjectAccess
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()


@router.post(
    "/{task_id}/annotations/polygon-slices:commit",
    response_model=AnnotationSliceResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def commit_polygon_slice(
    task_id: uuid.UUID,
    payload: PolygonSliceCommitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
) -> AnnotationSliceResponse:
    """Split one saved simple image Polygon in an atomic, idempotent transaction."""
    try:
        response = await _AnnotationSliceSqlAdapter(
            db, task_id=task_id, actor=current_user, access=access
        ).commit_polygon(
            task_id=task_id,
            actor_id=current_user.id,
            payload=payload,
            audit_origin=audit_origin_from_request(request),
        )
    except AnnotationSliceError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await db.commit()
    return response


@router.post(
    "/{task_id}/annotations/slices/{operation_id}:restore",
    response_model=AnnotationSliceResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def restore_annotation_slice(
    task_id: uuid.UUID,
    operation_id: uuid.UUID,
    payload: AnnotationSliceRestoreRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
) -> AnnotationSliceResponse:
    """Restore ledger-owned slice snapshots with the complete last result versions."""
    try:
        response = await _AnnotationSliceSqlAdapter(
            db, task_id=task_id, actor=current_user, access=None
        ).restore(
            task_id=task_id,
            operation_id=operation_id,
            actor_id=current_user.id,
            payload=payload,
            audit_origin=audit_origin_from_request(request),
        )
    except AnnotationSliceError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await db.commit()
    return response
