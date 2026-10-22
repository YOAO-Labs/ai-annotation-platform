"""Super-admin inventory preview; no sample ingestion or original-byte delivery."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.dataset_source import DatasetSource, DatasetScanRun, DatasetScanEntry
from app.db.models.user import User
from app.deps import get_db, require_roles
from app.schemas.dataset_source import (
    DatasetSourceCreate,
    DatasetSourceUpdate,
    DatasetSourceOut,
    DatasetSourceList,
    DatasetScanOut,
    DatasetScanList,
    DatasetScanEntryList,
)
from app.services import dataset_scans as svc
from app.services.audit import AuditAction, AuditService
from app.services.filesystem_storage import FilesystemStorageError

router = APIRouter()
super_admin = require_roles(UserRole.SUPER_ADMIN)


async def audit(db, actor, request, action, target_id):
    await AuditService.log(
        db,
        actor=actor,
        action=action,
        target_type="dataset_source",
        target_id=str(target_id),
        request=request,
        status_code=200,
    )


def conflict(exc):
    return HTTPException(409, detail={"code": exc.code})


@router.post("/dataset-sources", response_model=DatasetSourceOut, status_code=201)
async def create_source(
    payload: DatasetSourceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    try:
        source = await svc.create_source(db, payload, actor)
    except FilesystemStorageError as exc:
        raise conflict(exc) from None
    await audit(db, actor, request, AuditAction.DATASET_SOURCE_CREATE, source.id)
    await db.commit()
    return svc.source_out(source)


@router.get("/dataset-sources", response_model=DatasetSourceList)
async def list_sources(
    dataset_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
):
    stmt = select(DatasetSource)
    if dataset_id:
        stmt = stmt.where(DatasetSource.dataset_id == dataset_id)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await db.scalars(
        stmt.order_by(DatasetSource.created_at.desc(), DatasetSource.id)
        .limit(limit)
        .offset(offset)
    )
    return {"items": [svc.source_out(row) for row in rows], "total": total}


@router.get("/dataset-sources/{source_id}", response_model=DatasetSourceOut)
async def get_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
):
    source = await db.get(DatasetSource, source_id)
    if source is None:
        raise HTTPException(404, "Dataset source not found")
    return svc.source_out(source)


@router.patch("/dataset-sources/{source_id}", response_model=DatasetSourceOut)
async def patch_source(
    source_id: uuid.UUID,
    payload: DatasetSourceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    source = await svc.update_source(db, source_id, payload)
    await audit(db, actor, request, AuditAction.DATASET_SOURCE_UPDATE, source_id)
    await db.commit()
    return svc.source_out(source)


@router.post(
    "/dataset-sources/{source_id}/scans", response_model=DatasetScanOut, status_code=202
)
async def start_scan(
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    try:
        run, created = await svc.start_scan(db, source_id, actor)
    except FilesystemStorageError as exc:
        raise conflict(exc) from None
    await audit(db, actor, request, AuditAction.DATASET_SCAN_START, source_id)
    await db.commit()
    if created:
        from app.workers.dataset_scan import dispatch

        await dispatch(db, run.id)
    return run


@router.get("/dataset-sources/{source_id}/scans", response_model=DatasetScanList)
async def list_scans(
    source_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
):
    if await db.get(DatasetSource, source_id) is None:
        raise HTTPException(404, "Dataset source not found")
    query = select(DatasetScanRun).where(DatasetScanRun.source_id == source_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.scalars(
        query.order_by(DatasetScanRun.created_at.desc(), DatasetScanRun.id)
        .limit(limit)
        .offset(offset)
    )
    return {"items": list(rows), "total": total}


@router.get("/dataset-scans/{run_id}", response_model=DatasetScanOut)
async def get_scan(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
):
    run = await db.get(DatasetScanRun, run_id)
    if run is None:
        raise HTTPException(404, "Dataset scan not found")
    return run


@router.get("/dataset-scans/{run_id}/entries", response_model=DatasetScanEntryList)
async def list_entries(
    run_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
):
    if await db.get(DatasetScanRun, run_id) is None:
        raise HTTPException(404, "Dataset scan not found")
    query = select(DatasetScanEntry).where(
        DatasetScanEntry.run_id == run_id, DatasetScanEntry.relative_path != ""
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.scalars(
        query.order_by(DatasetScanEntry.path_digest).limit(limit).offset(offset)
    )
    return {
        "items": [
            {
                "relative_path": row.relative_path,
                "kind": row.kind,
                "state": row.state,
                "size_bytes": row.observed.get("size"),
                "content_verified": False,
            }
            for row in rows
        ],
        "total": total,
    }


@router.post(
    "/dataset-scans/{run_id}/resume", response_model=DatasetScanOut, status_code=202
)
async def resume_scan(
    run_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    try:
        run = await svc.resume_scan(db, run_id, actor)
    except FilesystemStorageError as exc:
        raise conflict(exc) from None
    await audit(db, actor, request, AuditAction.DATASET_SCAN_RESUME, run.source_id)
    await db.commit()
    from app.workers.dataset_scan import dispatch

    await dispatch(db, run.id)
    return run
