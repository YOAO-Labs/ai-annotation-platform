"""Private durable windows for registered migration source access."""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.storage_migration_window import (
    StorageMigrationWindowHistory,
    StorageMigrationWindowItem,
    StorageMigrationWindowRequest,
    StorageMigrationWindowTransition,
)
from app.services import storage_migration_windows as service

router = APIRouter()


def key(request):
    assert_request_scopes(request, "*")
    return getattr(request.state, "api_key_id", None)


@router.get("", response_model=StorageMigrationWindowHistory)
async def history(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    return await service.history(
        db, actor, api_key_id=key(request), limit=limit, offset=offset
    )


@router.post("", response_model=StorageMigrationWindowItem, status_code=201)
async def open_window(
    body: StorageMigrationWindowRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    value = await service.open_window(
        db, actor, body, api_key_id=key(request), request=request
    )
    await db.commit()
    return value


@router.get("/{window_id}", response_model=StorageMigrationWindowItem)
async def detail(
    window_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    return await service.view(db, window_id, actor, api_key_id=key(request))


@router.post("/{window_id}/transitions", response_model=StorageMigrationWindowItem)
async def transition(
    window_id: uuid.UUID,
    body: StorageMigrationWindowTransition,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    value = await service.transition(
        db, window_id, actor, body, api_key_id=key(request), request=request
    )
    await db.commit()
    return value
