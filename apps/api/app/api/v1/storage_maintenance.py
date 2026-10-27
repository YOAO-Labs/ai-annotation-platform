"""Location-scoped maintenance is separate from physical cleanup permission."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.storage_maintenance import (
    StorageMaintenanceApply,
    StorageMaintenancePreview,
    StorageMaintenanceRequest,
    StorageMaintenanceStatus,
)
from app.services import storage_maintenance as svc

super_admin = require_roles(UserRole.SUPER_ADMIN)


async def scopes(request: Request, _: User = Depends(super_admin)):
    assert_request_scopes(
        request, "datasets:read" if request.method == "GET" else "datasets:write"
    )


router = APIRouter(prefix="/storage-locations", dependencies=[Depends(scopes)])


@router.get("/{location_id}/maintenance", response_model=StorageMaintenanceStatus)
async def status(
    location_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    return await svc.status(
        db, location_id, actor, api_key_id=getattr(request.state, "api_key_id", None)
    )


@router.post(
    "/{location_id}/maintenance-preview", response_model=StorageMaintenancePreview
)
async def preview(
    location_id: uuid.UUID,
    payload: StorageMaintenanceRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    return await svc.preview(
        db,
        location_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )


@router.post("/{location_id}/maintenance", response_model=StorageMaintenanceStatus)
async def apply(
    location_id: uuid.UUID,
    payload: StorageMaintenanceApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    result = await svc.apply(
        db,
        location_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        request=request,
    )
    await db.commit()
    return result
