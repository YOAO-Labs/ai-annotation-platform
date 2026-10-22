"""Administrator inventories, verified content and explicit reference registration."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.dataset_source import (
    DatasetSource,
    DatasetScanRun,
    DatasetScanEntry,
    DatasetReference,
)
from app.db.models.user import User
from app.deps import get_db, require_roles, require_scopes
from app.schemas.dataset_source import (
    DatasetSourceCreate,
    DatasetSourceUpdate,
    DatasetSourceOut,
    DatasetSourceList,
    DatasetScanOut,
    DatasetScanList,
    DatasetScanEntryList,
    VerifiedScanContent,
    RegisteredReferenceOut,
)
from app.services import dataset_scans as svc
from app.services.audit import AuditAction, AuditService
from app.services.filesystem_storage import FilesystemStorageError
from app.services.content_snapshot import IMAGE_RESPONSES

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
    rows = list(
        await db.scalars(
            query.order_by(DatasetScanEntry.path_digest).limit(limit).offset(offset)
        )
    )
    run = await db.get(DatasetScanRun, run_id)
    registered = {
        r.path_digest: r.dataset_item_id
        for r in await db.scalars(
            select(DatasetReference).where(
                DatasetReference.source_id == run.source_id,
                DatasetReference.path_digest.in_([row.path_digest for row in rows]),
            )
        )
    }
    return {
        "items": [
            {
                "path_digest": row.path_digest,
                "relative_path": row.relative_path,
                "kind": row.kind,
                "state": row.state,
                "size_bytes": row.observed.get("size"),
                "content_verified": row.verified_content is not None,
                "registered_item_id": registered.get(row.path_digest),
                "verified_content": {
                    k: row.verified_content[k]
                    for k in (
                        "sha256",
                        "size_bytes",
                        "media_type",
                        "width",
                        "height",
                        "verified_at",
                    )
                }
                if row.verified_content
                else None,
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


@router.post(
    "/dataset-scans/{run_id}/entries/{path_digest}/verify",
    response_model=VerifiedScanContent,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def verify_scan_content(
    run_id: uuid.UUID,
    path_digest: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.dataset_content import scan_snapshot, content_http_error

    try:
        snapshot = await scan_snapshot(
            db,
            run_id,
            path_digest,
            actor,
            verify=True,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    try:
        return snapshot.public()
    finally:
        snapshot.close()


@router.get(
    "/dataset-scans/{run_id}/entries/{path_digest}/content",
    response_class=Response,
    responses=IMAGE_RESPONSES,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def read_scan_content(
    run_id: uuid.UUID,
    path_digest: str,
    request: Request,
    sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.dataset_content import scan_snapshot, content_http_error
    from app.services.content_snapshot import SnapshotResponse

    if request.headers.get("range") is not None:
        raise HTTPException(416, "Range is not supported for verified snapshots")
    try:
        snapshot = await scan_snapshot(
            db,
            run_id,
            path_digest,
            actor,
            expected_sha256=sha256,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    return SnapshotResponse(snapshot)


@router.post(
    "/dataset-scans/{run_id}/entries/{path_digest}/register",
    response_model=RegisteredReferenceOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def register_reference(
    run_id: uuid.UUID,
    path_digest: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.dataset_references import register_entry
    from app.services.dataset_content import content_http_error

    try:
        return await register_entry(
            db,
            run_id,
            path_digest,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None


@router.get(
    "/dataset-items/{item_id}/content",
    response_class=Response,
    responses=IMAGE_RESPONSES,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def reference_item_content(
    item_id: uuid.UUID,
    request: Request,
    sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.dataset_references import reference_snapshot
    from app.services.dataset_content import content_http_error
    from app.services.content_snapshot import SnapshotResponse

    if request.headers.get("range") is not None:
        raise HTTPException(416, "Range is not supported for verified snapshots")
    try:
        snapshot = await reference_snapshot(
            db,
            item_id,
            actor,
            expected_sha256=sha256,
            api_key_id=getattr(request.state, "api_key_id", None),
            scope="datasets:read",
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    return SnapshotResponse(snapshot)
