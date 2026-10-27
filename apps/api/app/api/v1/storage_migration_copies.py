"""Explicit private prepare, review, copy and recovery control plane."""

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.storage_migration_copy import (
    StorageMigrationCopyAttemptRequest,
    StorageMigrationCopyFailedObjects,
    StorageMigrationCopyGenerationRequest,
    StorageMigrationCopyHistory,
    StorageMigrationCopyItem,
    StorageMigrationCopyRequest,
    StorageMigrationCopyStart,
    StorageMigrationCopyTargets,
)
from app.services import storage_migration_copies as service

router = APIRouter()


def key(request):
    assert_request_scopes(request, "*")
    return getattr(request.state, "api_key_id", None)


@router.get("/migration-copy-targets", response_model=StorageMigrationCopyTargets)
async def targets(
    request: Request,
    inventory_job_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    return await service.candidates(
        db, inventory_job_id, actor, api_key_id=key(request), limit=limit, offset=offset
    )


@router.post(
    "/migration-copies", response_model=StorageMigrationCopyStart, status_code=202
)
async def start(
    body: StorageMigrationCopyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    from app.workers.storage_migration_copy import dispatch

    row, job, reused = await service.start(
        db, actor, body, api_key_id=key(request), request=request
    )
    value = StorageMigrationCopyStart(item=await service.item(db, row), reused=reused)
    if not reused:
        await dispatch(db, job)
    return value


@router.post(
    "/migration-copies/{parent_id}/generations",
    response_model=StorageMigrationCopyStart,
    status_code=202,
)
async def generation(
    parent_id: uuid.UUID,
    body: StorageMigrationCopyGenerationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    from app.workers.storage_migration_copy import dispatch

    row, job, reused = await service.generation(
        db, parent_id, actor, body, api_key_id=key(request), request=request
    )
    value = StorageMigrationCopyStart(item=await service.item(db, row), reused=reused)
    if not reused:
        await dispatch(db, job)
    return value


@router.get("/migration-copies", response_model=StorageMigrationCopyHistory)
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


@router.get("/migration-copies/{copy_id}", response_model=StorageMigrationCopyItem)
async def detail(
    copy_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    return await service.view(db, copy_id, actor, api_key_id=key(request))


@router.post(
    "/migration-copies/{copy_id}/attempts",
    response_model=StorageMigrationCopyStart,
    status_code=202,
)
async def attempt(
    copy_id: uuid.UUID,
    body: StorageMigrationCopyAttemptRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    from app.workers.storage_migration_copy import dispatch

    row, job, reused = await service.attempt(
        db, copy_id, actor, body, api_key_id=key(request), request=request
    )
    value = StorageMigrationCopyStart(item=await service.item(db, row), reused=reused)
    if not reused:
        await dispatch(db, job)
    return value


@router.post(
    "/migration-copies/{copy_id}/dispatch",
    response_model=StorageMigrationCopyStart,
    status_code=202,
)
async def dispatch(
    copy_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    from app.workers.storage_migration_copy import dispatch as publish

    row, job = await service.redispatch(db, copy_id, actor, api_key_id=key(request))
    value = StorageMigrationCopyStart(item=await service.item(db, row), reused=True)
    await publish(db, job)
    return value


@router.get(
    "/migration-copies/{copy_id}/failed-objects",
    response_model=StorageMigrationCopyFailedObjects,
)
async def failed(
    copy_id: uuid.UUID,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    return await service.failed_objects(
        db, copy_id, actor, api_key_id=key(request), limit=limit, offset=offset
    )


@router.get(
    "/migration-copies/{copy_id}/checkpoint",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/vnd.sqlite3": {
                    "schema": {"type": "string", "format": "binary"}
                }
            }
        }
    },
)
async def checkpoint(
    copy_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    raw, digest = await service.download(db, copy_id, actor, api_key_id=key(request))
    return Response(
        raw,
        media_type="application/vnd.sqlite3",
        headers={
            "Content-Disposition": 'attachment; filename="storage-migration-copy.sqlite"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Content-SHA256": digest,
        },
    )
