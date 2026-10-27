"""Explicit archive/restore operations preserve all dataset relationships."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.user import User
from app.deps import get_db, require_roles, require_scopes
from app.schemas.dataset_lifecycle import (
    DatasetLifecycleApply,
    DatasetLifecyclePreview,
    DatasetLifecyclePreviewRequest,
    DatasetLifecycleResult,
)
from app.services import dataset_lifecycle as svc

router = APIRouter(
    prefix="/datasets",
    dependencies=[Depends(require_scopes("datasets:write"))],
)
manager = require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


@router.post("/{dataset_id}/lifecycle-preview", response_model=DatasetLifecyclePreview)
async def preview_lifecycle(
    dataset_id: uuid.UUID,
    payload: DatasetLifecyclePreviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    return await svc.preview(
        db,
        dataset_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )


@router.post("/{dataset_id}/lifecycle", response_model=DatasetLifecycleResult)
async def change_lifecycle(
    dataset_id: uuid.UUID,
    payload: DatasetLifecycleApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    result = await svc.apply(
        db,
        dataset_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        request=request,
    )
    await db.commit()
    return result
