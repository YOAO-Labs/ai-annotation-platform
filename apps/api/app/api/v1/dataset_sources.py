"""Administrator inventories, verified content and explicit reference registration."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.dataset_source import (
    DatasetSource,
    DatasetScanRun,
    DatasetScanEntry,
    DatasetSourceItem,
    DatasetSourceRevision,
    DatasetSourcePreview,
    DatasetRegistrationRun,
    DatasetRegistrationEntry,
)
from app.db.models.user import User
from app.db.models.dataset_asset import StorageLocation
from app.deps import get_db, require_roles, require_scopes, assert_request_scopes
from app.schemas.dataset_source import (
    DatasetSourceCreate,
    DatasetSourceUpdate,
    DatasetSourceOut,
    DatasetSourceList,
    SourceExternalProtectionUpdate,
    DatasetScanOut,
    DatasetScanList,
    DatasetScanEntryList,
    VerifiedScanContent,
    RegisteredReferenceOut,
    DatasetRegistrationOut,
    DatasetRegistrationEntryList,
    DatasetRegistrationConfirm,
    DatasetSourceChangeList,
    SourceRevisionAccept,
    SourceRevisionResult,
    SourceRevisionList,
    SourceMoveCandidateList,
    SourceLifecycleImpact,
    SourceLifecycleChange,
    SourceSyncScheduleOut,
    SourceSyncScheduleUpdate,
    SourceSyncScheduleContinue,
    SourceRulesPreview,
    SourceRulesApply,
    SourceRulesImpact,
)
from app.services import dataset_scans as svc
from app.services import dataset_source_access as access
from app.services.audit import AuditAction, AuditService
from app.services.filesystem_storage import FilesystemStorageError
from app.services.content_snapshot import IMAGE_RESPONSES, ContentError

manager = require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


async def source_scopes(request: Request, actor: User = Depends(manager)):
    assert_request_scopes(
        request, "datasets:read" if request.method == "GET" else "datasets:write"
    )


router = APIRouter(dependencies=[Depends(source_scopes)])


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
    return HTTPException(getattr(exc, "status", 409), detail={"code": exc.code})


@router.post("/dataset-sources", response_model=DatasetSourceOut, status_code=201)
async def create_source(
    payload: DatasetSourceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    try:
        source = await svc.create_source(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except (FilesystemStorageError, ContentError) as exc:
        raise conflict(exc) from None
    await audit(db, actor, request, AuditAction.DATASET_SOURCE_CREATE, source.id)
    await db.commit()
    return await svc.source_out(db, source)


@router.get("/dataset-sources", response_model=DatasetSourceList)
async def list_sources(
    dataset_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    actor = await access.require_manager(db, actor.id)
    stmt = select(DatasetSource).where(access.source_scope_clause(actor))
    if dataset_id:
        stmt = stmt.where(DatasetSource.dataset_id == dataset_id)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await db.scalars(
        stmt.order_by(DatasetSource.created_at.desc(), DatasetSource.id)
        .limit(limit)
        .offset(offset)
    )
    rows = list(rows)
    location_map = {
        location.id: location
        for location in await db.scalars(
            select(StorageLocation).where(
                StorageLocation.id.in_({row.location_id for row in rows})
            )
        )
    }
    return {
        "items": [
            await svc.source_out(db, row, location=location_map.get(row.location_id))
            for row in rows
        ],
        "total": total,
    }


@router.get("/dataset-sources/{source_id}", response_model=DatasetSourceOut)
async def get_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    source = await access.require_source(db, source_id, actor)
    return await svc.source_out(db, source)


@router.patch("/dataset-sources/{source_id}", response_model=DatasetSourceOut)
async def patch_source(
    source_id: uuid.UUID,
    payload: DatasetSourceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    source = await svc.update_source(
        db,
        source_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )
    await audit(db, actor, request, AuditAction.DATASET_SOURCE_UPDATE, source_id)
    await db.commit()
    return await svc.source_out(db, source)


@router.put(
    "/dataset-sources/{source_id}/external-protection", response_model=DatasetSourceOut
)
async def update_external_protection(
    source_id: uuid.UUID,
    payload: SourceExternalProtectionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_protection

    source = await dataset_source_protection.update(
        db,
        source_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )
    await audit(db, actor, request, "dataset_source.protection_update", source.id)
    await db.commit()
    return await svc.source_out(db, source)


@router.get(
    "/dataset-sources/{source_id}/lifecycle-impact",
    response_model=SourceLifecycleImpact,
)
async def lifecycle_impact(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services.dataset_source_lifecycle import impact

    source = await access.require_source(db, source_id, actor)
    return await impact(db, source, actor)


@router.post("/dataset-sources/{source_id}/lifecycle", response_model=DatasetSourceOut)
async def source_lifecycle(
    source_id: uuid.UUID,
    payload: SourceLifecycleChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services.dataset_source_lifecycle import change

    source = await change(
        db, source_id, payload, actor, getattr(request.state, "api_key_id", None)
    )
    await audit(db, actor, request, f"dataset_source.{payload.action}", source.id)
    await db.commit()
    return await svc.source_out(db, source)


@router.get(
    "/dataset-sources/{source_id}/entries/{path_digest}/preview",
    response_class=Response,
    responses=IMAGE_RESPONSES,
)
async def read_directory_preview(
    source_id: uuid.UUID,
    path_digest: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services.directory_previews import read_preview
    from app.services.dataset_content import content_http_error
    from app.services.content_snapshot import SnapshotResponse

    if request.headers.get("range") is not None:
        raise HTTPException(416, "Range is not supported for previews")
    try:
        snapshot = await read_preview(
            db,
            source_id,
            path_digest,
            actor,
            getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    return SnapshotResponse(snapshot)


@router.post(
    "/dataset-sources/{source_id}/scans", response_model=DatasetScanOut, status_code=202
)
async def start_scan(
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    try:
        run, created = await svc.start_scan(
            db, source_id, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except (FilesystemStorageError, ContentError) as exc:
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
    complete: bool | None = None,
    active_only: bool = False,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    await access.require_source(db, source_id, actor)
    query = select(DatasetScanRun).where(DatasetScanRun.source_id == source_id)
    if complete is not None:
        query = query.where(DatasetScanRun.complete == complete)
    if active_only:
        query = query.where(DatasetScanRun.status.in_(["pending", "running"]))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    ordering = (
        (
            DatasetScanRun.completed_at.desc().nulls_last(),
            DatasetScanRun.created_at.desc(),
            DatasetScanRun.id,
        )
        if complete is True
        else (DatasetScanRun.created_at.desc(), DatasetScanRun.id)
    )
    rows = await db.scalars(query.order_by(*ordering).limit(limit).offset(offset))
    return {"items": list(rows), "total": total}


@router.get("/dataset-scans/{run_id}", response_model=DatasetScanOut)
async def get_scan(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    run = await access.require_run(db, DatasetScanRun, run_id, actor)
    return run


@router.get(
    "/dataset-sources/{source_id}/active-registration",
    response_model=DatasetRegistrationOut | None,
)
async def active_registration(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    await access.require_source(db, source_id, actor)
    return await db.scalar(
        select(DatasetRegistrationRun).where(
            DatasetRegistrationRun.source_id == source_id,
            DatasetRegistrationRun.status.in_(["pending", "running"]),
        )
    )


@router.get("/dataset-scans/{run_id}/entries", response_model=DatasetScanEntryList)
async def list_entries(
    run_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    run = await access.require_run(db, DatasetScanRun, run_id, actor)
    query = select(DatasetScanEntry).where(
        DatasetScanEntry.run_id == run_id, DatasetScanEntry.relative_path != ""
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(
        await db.scalars(
            query.order_by(DatasetScanEntry.path_digest).limit(limit).offset(offset)
        )
    )
    registered = {
        r.path_digest: r
        for r in await db.scalars(
            select(DatasetSourceItem).where(
                DatasetSourceItem.source_id == run.source_id,
                DatasetSourceItem.path_digest.in_([row.path_digest for row in rows]),
            )
        )
    }
    baseline_observations = {
        e.path_digest: (
            e.verified_content["observation"] if e.verified_content else None,
            scan.snapshot["anchor"],
        )
        for e, scan in await db.execute(
            select(DatasetScanEntry, DatasetScanRun)
            .join(DatasetScanRun, DatasetScanRun.id == DatasetScanEntry.run_id)
            .join(
                DatasetSourceItem,
                (DatasetSourceItem.run_id == DatasetScanEntry.run_id)
                & (DatasetSourceItem.path_digest == DatasetScanEntry.path_digest),
            )
            .where(
                DatasetSourceItem.source_id == run.source_id,
                DatasetSourceItem.path_digest.in_([row.path_digest for row in rows]),
            )
        )
    }
    historical = {
        r.path_digest: r.dataset_item_id
        for r in await db.scalars(
            select(DatasetSourceRevision).where(
                DatasetSourceRevision.source_id == run.source_id,
                DatasetSourceRevision.run_id == run.id,
                DatasetSourceRevision.path_digest.in_(
                    [row.path_digest for row in rows]
                ),
            )
        )
    }
    accepted = {
        row.path_digest: historical[row.path_digest]
        if row.path_digest in historical
        else (
            registered[row.path_digest].dataset_item_id
            if row.verified_content
            and row.path_digest in registered
            and registered[row.path_digest].content_sha256
            == row.verified_content["sha256"]
            and baseline_observations.get(row.path_digest)
            == (row.verified_content["observation"], run.snapshot["anchor"])
            else None
        )
        for row in rows
    }
    previews = {
        p.path_digest: p
        for p in await db.scalars(
            select(DatasetSourcePreview).where(
                DatasetSourcePreview.source_id == run.source_id,
                DatasetSourcePreview.path_digest.in_([row.path_digest for row in rows]),
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
                "registered_item_id": accepted.get(row.path_digest),
                "preview_state": previews[row.path_digest].state
                if (
                    row.path_digest in previews
                    and row.path_digest in registered
                    and accepted.get(row.path_digest)
                    == registered[row.path_digest].dataset_item_id
                )
                else None,
                "preview_bytes": previews[row.path_digest].size_bytes
                if (
                    row.path_digest in previews
                    and row.path_digest in registered
                    and accepted.get(row.path_digest)
                    == registered[row.path_digest].dataset_item_id
                )
                else None,
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
    actor: User = Depends(manager),
):
    try:
        run = await svc.resume_scan(
            db, run_id, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except (FilesystemStorageError, ContentError) as exc:
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
    actor: User = Depends(manager),
):
    from app.services.dataset_content import scan_snapshot, content_http_error

    async def dataset_guard(current_db, *, lock=True):
        from app.services import dataset_source_access as access

        run = await access.require_run(current_db, DatasetScanRun, run_id, actor)
        source = await access.require_source(current_db, run.source_id, actor)
        await svc.require_dataset_write(
            current_db, source, snapshot=run.snapshot, lock=lock
        )

    try:
        # API verification publishes a receipt. Plain GET content keeps the
        # existing read context and remains available for archived datasets.
        await dataset_guard(db, lock=False)
        snapshot = await scan_snapshot(
            db,
            run_id,
            path_digest,
            actor,
            verify=True,
            before_io=dataset_guard,
            before_commit=dataset_guard,
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
    actor: User = Depends(manager),
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
    actor: User = Depends(manager),
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
    actor: User = Depends(manager),
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


@router.get(
    "/dataset-scans/{scan_id}/registration",
    response_model=DatasetRegistrationOut | None,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_scan_registration(
    scan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    await access.require_run(db, DatasetScanRun, scan_id, actor)
    return await db.scalar(
        select(DatasetRegistrationRun).where(
            DatasetRegistrationRun.scan_run_id == scan_id
        )
    )


async def dispatch_registration(db, run, created):
    await db.commit()
    if created:
        from app.workers.dataset_registration import dispatch

        await dispatch(db, run.id)
    await db.refresh(run)
    return run


@router.post(
    "/dataset-scans/{scan_id}/registration",
    response_model=DatasetRegistrationOut,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def preflight_registration(
    scan_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_registration as registration
    from app.services.dataset_content import content_http_error

    try:
        run, created = await registration.start_preflight(
            db, scan_id, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
        await audit(db, actor, request, "dataset_registration.preflight", run.id)
        return await dispatch_registration(db, run, created)
    except Exception as exc:
        raise content_http_error(exc) from None


@router.post(
    "/dataset-registrations/{registration_id}/confirm",
    response_model=DatasetRegistrationOut,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def confirm_registration(
    registration_id: uuid.UUID,
    request: Request,
    payload: DatasetRegistrationConfirm | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_registration as registration
    from app.services.dataset_content import content_http_error

    try:
        run, created = await registration.confirm(
            db,
            registration_id,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            scope=payload.scope if payload else "all_safe",
        )
        await audit(db, actor, request, "dataset_registration.confirm", run.id)
        return await dispatch_registration(db, run, created)
    except Exception as exc:
        raise content_http_error(exc) from None


@router.post(
    "/dataset-registrations/{registration_id}/resume",
    response_model=DatasetRegistrationOut,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def resume_registration(
    registration_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_registration as registration
    from app.services.dataset_content import content_http_error

    try:
        run = await registration.resume(
            db,
            registration_id,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
        await audit(db, actor, request, "dataset_registration.resume", run.id)
        return await dispatch_registration(db, run, True)
    except Exception as exc:
        raise content_http_error(exc) from None


@router.post(
    "/dataset-registrations/{registration_id}/entries/{path_digest}/retry",
    response_model=DatasetRegistrationOut,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def retry_registration_entry(
    registration_id: uuid.UUID,
    path_digest: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_registration as registration
    from app.services.dataset_content import content_http_error

    try:
        run = await registration.resume(
            db,
            registration_id,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            path_digest=path_digest,
        )
        await audit(db, actor, request, "dataset_registration.retry_entry", run.id)
        return await dispatch_registration(db, run, True)
    except Exception as exc:
        raise content_http_error(exc) from None


@router.get(
    "/dataset-registrations/{registration_id}/entries",
    response_model=DatasetRegistrationEntryList,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def registration_entries(
    registration_id: uuid.UUID,
    state: Literal["pending", "ready", "registered", "unchanged", "error"]
    | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    run = await access.require_run(db, DatasetRegistrationRun, registration_id, actor)
    entry = DatasetRegistrationEntry
    stmt = (
        select(entry, DatasetScanEntry)
        .join(
            DatasetScanEntry,
            (DatasetScanEntry.run_id == run.scan_run_id)
            & (DatasetScanEntry.path_digest == entry.path_digest),
        )
        .where(entry.run_id == run.id)
    )
    if state:
        stmt = stmt.where(entry.state == state)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.execute(stmt.order_by(entry.path_digest).offset(offset).limit(limit))
    ).all()
    return {
        "total": total,
        "items": [
            {
                "path_digest": unit.path_digest,
                "relative_path": observed.relative_path,
                "state": unit.state,
                "item_id": unit.item_id,
                "error_code": unit.error_code,
                "attempts": unit.attempts,
                "size_bytes": observed.observed.get("size"),
                "sha256": (observed.verified_content or {}).get("sha256"),
                "preview_estimated_bytes": unit.preview_estimated_bytes,
                "preview_error": unit.preview_error,
            }
            for unit, observed in rows
        ],
    }


@router.get(
    "/dataset-registrations/{registration_id}/changes",
    response_model=DatasetSourceChangeList,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def registration_changes(
    registration_id: uuid.UUID,
    kind: Literal["added", "unchanged", "changed", "not_seen", "excluded", "unreadable"]
    | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_changes as changes

    run = await access.require_run(db, DatasetRegistrationRun, registration_id, actor)
    return await changes.page(db, run, kind, limit, offset)


@router.get(
    "/dataset-sources/{source_id}/sync-schedule",
    response_model=SourceSyncScheduleOut | None,
)
async def get_sync_schedule(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.db.models.dataset_source import DatasetSourceSyncSchedule

    await access.require_source(db, source_id, actor)
    return await db.get(DatasetSourceSyncSchedule, source_id)


@router.put(
    "/dataset-sources/{source_id}/sync-schedule", response_model=SourceSyncScheduleOut
)
async def configure_sync_schedule(
    source_id: uuid.UUID,
    payload: SourceSyncScheduleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_sync as sync

    try:
        row = await sync.configure(
            db, source_id, payload, actor, getattr(request.state, "api_key_id", None)
        )
        await audit(
            db, actor, request, "dataset_source.sync_schedule.configure", source_id
        )
        await db.commit()
        return row
    except (FilesystemStorageError, ContentError) as exc:
        raise conflict(exc) from None


@router.post(
    "/dataset-sources/{source_id}/sync-schedule/continue",
    response_model=SourceSyncScheduleOut,
)
async def continue_sync_schedule(
    source_id: uuid.UUID,
    payload: SourceSyncScheduleContinue,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_sync as sync

    try:
        row = await sync.continue_cycle(
            db, source_id, payload, actor, getattr(request.state, "api_key_id", None)
        )
    except (FilesystemStorageError, ContentError) as exc:
        raise conflict(exc) from None
    await audit(db, actor, request, "dataset_source.sync_schedule.continue", source_id)
    await db.commit()
    return row


@router.post(
    "/dataset-registrations/{registration_id}/changes/{path_digest}/accept-revision",
    response_model=SourceRevisionResult,
)
async def accept_content_revision(
    registration_id: uuid.UUID,
    path_digest: str,
    payload: SourceRevisionAccept,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_revisions as revisions
    from app.services.dataset_content import content_http_error

    try:
        return await revisions.accept(
            db,
            registration_id,
            path_digest,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        await db.rollback()
        raise content_http_error(exc) from None


@router.post(
    "/dataset-registrations/{registration_id}/changes/{path_digest}/revalidate",
    response_model=SourceRevisionResult,
)
async def revalidate_observation(
    registration_id: uuid.UUID,
    path_digest: str,
    payload: SourceRevisionAccept,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_revisions as revisions
    from app.services.dataset_content import content_http_error

    try:
        return await revisions.accept(
            db,
            registration_id,
            path_digest,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            revalidate=True,
        )
    except Exception as exc:
        await db.rollback()
        raise content_http_error(exc) from None


@router.post(
    "/dataset-registrations/{registration_id}/changes/{path_digest}/relocate",
    response_model=SourceRevisionResult,
)
async def relocate_observation(
    registration_id: uuid.UUID,
    path_digest: str,
    payload: SourceRevisionAccept,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_revisions as revisions
    from app.services.dataset_content import content_http_error

    try:
        return await revisions.accept(
            db,
            registration_id,
            path_digest,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            relocate=True,
        )
    except Exception as exc:
        await db.rollback()
        raise content_http_error(exc) from None


@router.get(
    "/dataset-registrations/{registration_id}/changes/{path_digest}/move-candidates",
    response_model=SourceMoveCandidateList,
)
async def list_move_candidates(
    registration_id: uuid.UUID,
    path_digest: str,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_revisions as revisions

    run = await access.require_run(db, DatasetRegistrationRun, registration_id, actor)
    return await revisions.move_candidates(db, run, path_digest, limit, offset)


@router.get("/dataset-sources/{source_id}/revisions", response_model=SourceRevisionList)
async def source_revision_history(
    source_id: uuid.UUID,
    path_digest: str | None = Query(None, pattern=r"^[0-9a-f]{64}$"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_revisions as revisions

    await access.require_source(db, source_id, actor)
    return await revisions.history(db, source_id, path_digest, limit, offset)


@router.post(
    "/dataset-sources/{source_id}/rules/preview", response_model=SourceRulesImpact
)
async def preview_source_rules(
    source_id: uuid.UUID,
    payload: SourceRulesPreview,
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_rules

    return await dataset_source_rules.preview(
        db,
        source_id,
        payload,
        actor,
        limit=limit,
        offset=offset,
        api_key_id=getattr(request.state, "api_key_id", None),
    )


@router.post("/dataset-sources/{source_id}/rules", response_model=DatasetSourceOut)
async def apply_source_rules(
    source_id: uuid.UUID,
    payload: SourceRulesApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import dataset_source_rules

    source = await dataset_source_rules.apply(
        db,
        source_id,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )
    await db.commit()
    return await svc.source_out(db, source)
