"""Private fixed-source migration preparation, without copy or cutover."""

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.storage_migration_inventory import (
    StorageMigrationInventoryHistory,
    StorageMigrationInventoryRequest,
    StorageMigrationInventoryStart,
)
from app.services import storage_migration_inventories as service

router = APIRouter()


@router.post("", response_model=StorageMigrationInventoryStart, status_code=202)
async def start_inventory(
    body: StorageMigrationInventoryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    from app.workers.storage_migration_inventory import dispatch

    row, job, reused = await service.start(
        db,
        actor,
        body,
        api_key_id=getattr(request.state, "api_key_id", None),
        request=request,
    )
    response = StorageMigrationInventoryStart(item=service.item(row), reused=reused)
    # Reuse is observational. Only explicit redispatch retries uncertain publish.
    if not reused:
        await dispatch(db, job)
    return response


@router.get("", response_model=StorageMigrationInventoryHistory)
async def history(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    return await service.history(
        db,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{job_id}/dispatch", response_model=StorageMigrationInventoryStart, status_code=202
)
async def redispatch(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    from app.workers.storage_migration_inventory import dispatch

    row, job = await service.redispatch(
        db, job_id, actor, api_key_id=getattr(request.state, "api_key_id", None)
    )
    response = StorageMigrationInventoryStart(item=service.item(row), reused=True)
    await dispatch(db, job)
    return response


@router.get(
    "/{job_id}/report",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/x-ndjson": {
                    "schema": {"type": "string", "format": "binary"}
                }
            }
        }
    },
)
async def report(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    raw, sha = await service.download(
        db, job_id, actor, api_key_id=getattr(request.state, "api_key_id", None)
    )
    return Response(
        raw,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="storage-migration-inventory.jsonl"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{sha}"',
            "X-Content-SHA256": sha,
        },
    )
