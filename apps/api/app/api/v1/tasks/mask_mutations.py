import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import require_task_annotation_write
from app.application.mask_annotation_slices import _MaskMutationSqlAdapter
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_scopes
from app.schemas.mask_mutation import (
    MaskMutationCommitRequest,
    MaskMutationCommitResponse,
)
from app.services.workspace_contract import ProjectAccess
from app.services.audit import audit_origin_from_request
from app.services.mask_mutation_contract import MaskMutationError


router = APIRouter()


@router.post(
    "/{task_id}/annotations/mask-mutations:commit",
    response_model=MaskMutationCommitResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def commit_mask_mutations(
    task_id: uuid.UUID,
    payload: MaskMutationCommitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
) -> MaskMutationCommitResponse:
    """Commit one versioned multi-annotation Mask operation atomically."""

    try:
        response = await _MaskMutationSqlAdapter(
            db, task_id=task_id, actor=current_user, access=access
        ).commit(
            task_id=task_id,
            actor_id=current_user.id,
            payload=payload,
            audit_origin=audit_origin_from_request(request),
        )
    except MaskMutationError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await db.commit()
    return response
