"""Controlled registry. No arbitrary host paths or source writes are exposed."""

import asyncio
from datetime import datetime, timezone
import uuid
from typing import Literal

from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.utils.nodenames import worker_direct
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.enums import UserRole
from app.db.models.dataset_asset import StorageLocation, StorageLocationProbe
from app.db.models.user import User
from app.deps import get_db, require_roles, assert_request_scopes
from app.schemas.storage_location import (
    FilesystemLocationCreate,
    ExportBindingApply,
    ExportBindingCandidates,
    ExportBindingChange,
    ExportBindingPreview,
    MediaCacheBindingApply,
    MediaCacheBindingCandidates,
    MediaCacheBindingChange,
    MediaCacheBindingPreview,
    DatasetBindingApply,
    DatasetBindingCandidates,
    DatasetBindingChange,
    DatasetBindingPreview,
    ImportBindingApply,
    ImportBindingCandidates,
    ImportBindingChange,
    ImportBindingPreview,
    AuditArchiveBindingApply,
    AuditArchiveBindingCandidates,
    AuditArchiveBindingChange,
    AuditArchiveBindingPreview,
    BugAttachmentBindingApply,
    BugAttachmentBindingCandidates,
    BugAttachmentBindingChange,
    BugAttachmentBindingPreview,
    AvatarBindingApply,
    AvatarBindingCandidates,
    AvatarBindingChange,
    AvatarBindingPreview,
    StorageFaultList,
    StorageBindingOut,
    StorageDirectoryPage,
    StorageLocationList,
    StorageLocationOut,
    StorageLocationUpdate,
    StorageLocationApply,
    StorageLocationChangePreview,
    StorageMaintenanceImpact,
    StorageSourceSummary,
    StorageProbeOut,
    StorageProbeRequest,
)
from app.schemas.storage_cache import (
    OriginalCacheCleanup,
    OriginalCacheCleanupResult,
    OriginalCacheUsage,
)
from app.services import storage_location as svc
from app.services.dataset_source_access import require_manager
from app.services.dataset_content import key_authority
from app.services.audit import AuditAction, AuditService
from app.services.content_snapshot import ContentError
from app.services.filesystem_storage import FilesystemStorageError

manager = require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
super_admin = require_roles(UserRole.SUPER_ADMIN)


async def location_scopes(request: Request, _: User = Depends(manager)):
    assert_request_scopes(
        request, "datasets:read" if request.method == "GET" else "datasets:write"
    )


router = APIRouter(dependencies=[Depends(location_scopes)])


def filesystem_error(exc):
    return HTTPException(getattr(exc, "status", 409), detail={"code": exc.code})


async def lock_current_admin(db, actor, request):
    from app.db.models.api_key import ApiKey

    key_id = getattr(request.state, "api_key_id", None)
    proof = await key_authority(db, actor, key_id, "datasets:write")
    actor = await db.scalar(
        select(User)
        .where(User.id == actor.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if not actor or not actor.is_active or actor.role != UserRole.SUPER_ADMIN:
        raise HTTPException(403, "Platform administrator required")
    if key_id:
        await db.scalar(
            select(ApiKey)
            .where(ApiKey.id == key_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    if await key_authority(db, actor, key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return actor


@router.get("/roots", response_model=list[str])
async def roots(_: User = Depends(super_admin)):
    return sorted(settings.storage_filesystem_roots)


@router.get("/bindings", response_model=list[StorageBindingOut])
async def bindings(db: AsyncSession = Depends(get_db), _: User = Depends(super_admin)):
    return await svc.binding_rows(db)


@router.get("/bindings/avatars/candidates", response_model=AvatarBindingCandidates)
async def avatar_binding_candidates(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.services import avatar_binding_change as change

    return await change.candidates(db, limit=limit, offset=offset)


@router.post("/bindings/avatars/change-preview", response_model=AvatarBindingPreview)
async def preview_avatar_binding(
    payload: AvatarBindingChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import avatar_binding_change as change

    try:
        result = await change.preview(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.patch("/bindings/avatars", response_model=StorageBindingOut)
async def apply_avatar_binding(
    payload: AvatarBindingApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import avatar_binding_change as change

    try:
        result = await change.apply(
            db,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.get(
    "/bindings/bug-reports/candidates", response_model=BugAttachmentBindingCandidates
)
async def bug_attachment_binding_candidates(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.services import bug_attachment_binding_change as change

    return await change.candidates(db, limit=limit, offset=offset)


@router.post(
    "/bindings/bug-reports/change-preview", response_model=BugAttachmentBindingPreview
)
async def preview_bug_attachment_binding(
    payload: BugAttachmentBindingChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import bug_attachment_binding_change as change

    try:
        result = await change.preview(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.patch("/bindings/bug-reports", response_model=StorageBindingOut)
async def apply_bug_attachment_binding(
    payload: BugAttachmentBindingApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import bug_attachment_binding_change as change

    try:
        result = await change.apply(
            db,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.get(
    "/bindings/audit-archive/candidates", response_model=AuditArchiveBindingCandidates
)
async def audit_archive_binding_candidates(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.services import audit_archive_binding_change as change

    return await change.candidates(db, limit=limit, offset=offset)


@router.post(
    "/bindings/audit-archive/change-preview", response_model=AuditArchiveBindingPreview
)
async def preview_audit_archive_binding(
    payload: AuditArchiveBindingChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import audit_archive_binding_change as change

    try:
        result = await change.preview(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.patch("/bindings/audit-archive", response_model=StorageBindingOut)
async def apply_audit_archive_binding(
    payload: AuditArchiveBindingApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import audit_archive_binding_change as change

    try:
        result = await change.apply(
            db,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.get("/bindings/export/candidates", response_model=ExportBindingCandidates)
async def export_binding_candidates(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.services import export_binding_change as change

    return await change.candidates(db, limit=limit, offset=offset)


@router.post("/bindings/export/change-preview", response_model=ExportBindingPreview)
async def preview_export_binding(
    payload: ExportBindingChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import export_binding_change as change

    try:
        result = await change.preview(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.patch("/bindings/export", response_model=StorageBindingOut)
async def apply_export_binding(
    payload: ExportBindingApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import export_binding_change as change

    try:
        result = await change.apply(
            db,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.get("/bindings/import/candidates", response_model=ImportBindingCandidates)
async def import_binding_candidates(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.services import import_binding_change as change

    return await change.candidates(db, limit=limit, offset=offset)


@router.post("/bindings/import/change-preview", response_model=ImportBindingPreview)
async def preview_import_binding(
    payload: ImportBindingChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import import_binding_change as change

    try:
        result = await change.preview(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.patch("/bindings/import", response_model=StorageBindingOut)
async def apply_import_binding(
    payload: ImportBindingApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import import_binding_change as change

    try:
        result = await change.apply(
            db,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.get("/bindings/datasets/candidates", response_model=DatasetBindingCandidates)
async def dataset_binding_candidates(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.services import dataset_binding_change as change

    return await change.candidates(db, limit=limit, offset=offset)


@router.post("/bindings/datasets/change-preview", response_model=DatasetBindingPreview)
async def preview_dataset_binding(
    payload: DatasetBindingChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import dataset_binding_change as change

    try:
        result = await change.preview(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.patch("/bindings/datasets", response_model=StorageBindingOut)
async def apply_dataset_binding(
    payload: DatasetBindingApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import dataset_binding_change as change

    try:
        result = await change.apply(
            db,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.get(
    "/bindings/media-cache/candidates", response_model=MediaCacheBindingCandidates
)
async def media_cache_binding_candidates(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(super_admin),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.services import media_cache_binding_change as change

    return await change.candidates(db, limit=limit, offset=offset)


@router.post(
    "/bindings/media-cache/change-preview", response_model=MediaCacheBindingPreview
)
async def preview_media_cache_binding(
    payload: MediaCacheBindingChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import media_cache_binding_change as change

    try:
        result = await change.preview(
            db, payload, actor, api_key_id=getattr(request.state, "api_key_id", None)
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.patch("/bindings/media-cache", response_model=StorageBindingOut)
async def apply_media_cache_binding(
    payload: MediaCacheBindingApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services import media_cache_binding_change as change

    try:
        result = await change.apply(
            db,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except change.PUBLIC_ERRORS as exc:
        raise change.http_error(exc) from None
    await db.commit()
    return result


@router.get("", response_model=StorageLocationList)
async def list_locations(
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    clause = svc.visible_clause(actor)
    total = await db.scalar(
        select(func.count()).select_from(StorageLocation).where(clause)
    )
    rows = list(
        await db.scalars(
            select(StorageLocation)
            .where(clause)
            .order_by(StorageLocation.name, StorageLocation.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return {"items": [await svc.to_out(db, row, actor) for row in rows], "total": total}


@router.post("", response_model=StorageLocationOut, status_code=201)
async def create_location(
    payload: FilesystemLocationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    actor = await lock_current_admin(db, actor, request)
    try:
        location = await svc.create_filesystem(db, payload)
    except (FilesystemStorageError, ContentError) as exc:
        raise filesystem_error(exc) from None
    except IntegrityError:
        raise HTTPException(409, "Storage location name already exists") from None
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.STORAGE_LOCATION_CREATE,
        target_type="storage_location",
        target_id=str(location.id),
        request=request,
        status_code=201,
        detail={"kind": "filesystem"},
    )
    await db.commit()
    return await svc.to_out(db, location, actor)


@router.get("/{location_id}/faults", response_model=StorageFaultList)
async def source_faults(
    location_id: uuid.UUID,
    request: Request,
    state: Literal["active", "recovered", "all"] = "active",
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    from app.services import storage_fault_state

    actor = await require_manager(db, actor.id)
    proof = await key_authority(
        db, actor, getattr(request.state, "api_key_id", None), "datasets:read"
    )
    location = await svc.get_visible(db, location_id, actor)
    initial_role = actor.role
    result = await storage_fault_state.visible_page(
        db, location, actor, state=state, limit=limit, offset=offset
    )
    actor = await require_manager(db, actor.id)
    if actor.role != initial_role:
        raise HTTPException(403, detail={"code": "access_revoked"})
    await svc.get_visible(db, location_id, actor)
    if (
        await key_authority(
            db, actor, getattr(request.state, "api_key_id", None), "datasets:read"
        )
        != proof
    ):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return result


@router.get("/{location_id}", response_model=StorageLocationOut)
async def get_location(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    return await svc.to_out(db, await svc.get_visible(db, location_id, actor), actor)


@router.get("/{location_id}/source-summary", response_model=StorageSourceSummary)
async def source_summary(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    jobs_limit: int = Query(20, ge=1, le=200),
    jobs_offset: int = Query(0, ge=0),
):
    from app.services.storage_source_summary import summary

    return await summary(
        db,
        location_id,
        actor,
        limit=limit,
        offset=offset,
        jobs_limit=jobs_limit,
        jobs_offset=jobs_offset,
    )


async def lock_change_location(db, location_id, actor):
    from app.db.models.storage_connection import StorageConnection

    initial = await svc.get_visible(db, location_id, actor)
    connection_id = initial.connection_id
    if connection_id is not None:
        # Connector changes own Connection -> Location. Keep that order when
        # binding a location review to the connector's current revision.
        await db.scalar(
            select(StorageConnection)
            .where(StorageConnection.id == connection_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    location = await svc.get_visible(db, location_id, actor, lock=True)
    if location.connection_id != connection_id:
        raise HTTPException(409, detail={"code": "storage_change_preview_changed"})
    return location


@router.post(
    "/{location_id}/change-preview", response_model=StorageLocationChangePreview
)
async def preview_location_change(
    location_id: uuid.UUID,
    payload: StorageLocationUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.storage_location_change import preview

    actor = await lock_current_admin(db, actor, request)
    location = await lock_change_location(db, location_id, actor)
    return await preview(
        db,
        location,
        payload,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )


@router.patch("/{location_id}", response_model=StorageLocationOut)
async def update_location(
    location_id: uuid.UUID,
    payload: StorageLocationApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.storage_location_change import apply

    actor = await lock_current_admin(db, actor, request)
    location = await lock_change_location(db, location_id, actor)
    try:
        detail = await apply(
            db,
            location,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except IntegrityError:
        raise HTTPException(409, "Storage location name already exists") from None
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.STORAGE_LOCATION_UPDATE,
        target_type="storage_location",
        target_id=str(location.id),
        request=request,
        status_code=200,
        detail=detail,
    )
    await db.commit()
    return await svc.to_out(db, location, actor)


@router.get(
    "/{location_id}/maintenance-impact", response_model=StorageMaintenanceImpact
)
async def maintenance_impact(
    location_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.storage_maintenance_impact import impact

    return await impact(
        db,
        location_id,
        actor,
        api_key_id=getattr(request.state, "api_key_id", None),
    )


@router.get("/{location_id}/original-cache", response_model=OriginalCacheUsage)
async def original_cache_usage(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.original_cache import usage_snapshot

    location = await svc.get_visible(db, location_id, actor)
    return await usage_snapshot(db, location)


@router.post(
    "/{location_id}/original-cache/cleanup",
    response_model=OriginalCacheCleanupResult,
    status_code=202,
)
async def cleanup_original_cache(
    location_id: uuid.UUID,
    payload: OriginalCacheCleanup,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(super_admin),
):
    from app.services.original_cache import now, request_cleanup

    actor = await lock_current_admin(db, actor, request)
    location = await svc.get_visible(db, location_id, actor, lock=True)
    if payload.expected_revision != location.config_revision:
        raise HTTPException(409, "Storage configuration revision changed")
    if payload.recorded_before > now():
        raise HTTPException(422, "Cache observation must not be in the future")
    result = await request_cleanup(
        db, location, recorded_before=payload.recorded_before, limit=payload.limit
    )
    await AuditService.log(
        db,
        actor=actor,
        request=request,
        action=AuditAction.STORAGE_ORIGINAL_CACHE_CLEANUP,
        target_type="storage_location",
        target_id=str(location.id),
        status_code=202,
        detail={
            "retired_count": result["retired_count"],
            "skipped_leased_count": result["skipped_leased_count"],
        },
    )
    await db.commit()
    return result


@router.get("/{location_id}/directories", response_model=StorageDirectoryPage)
async def directories(
    location_id: uuid.UUID,
    request: Request,
    path: str = Query("", max_length=1000),
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = Query(None, max_length=12288),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    assert_request_scopes(request, "datasets:read")
    proof = await key_authority(
        db, actor, getattr(request.state, "api_key_id", None), "datasets:read"
    )
    actor = await require_manager(db, actor.id)
    location = await svc.get_visible(db, location_id, actor)
    revision = location.config_revision
    from app.services import storage_access

    try:
        maintenance_revision = await storage_access.check(db, location.id)
        result = await svc.browse(
            db,
            location,
            path=path,
            limit=limit,
            cursor=cursor,
            maintenance_revision=maintenance_revision,
        )
    except (FilesystemStorageError, ContentError) as exc:
        raise filesystem_error(exc) from None
    actor = await require_manager(db, actor.id)
    location = await svc.get_visible(db, location_id, actor, lock=True)
    if location.config_revision != revision or not location.enabled:
        raise HTTPException(409, "Storage configuration revision changed")
    if (
        await key_authority(
            db, actor, getattr(request.state, "api_key_id", None), "datasets:read"
        )
        != proof
    ):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    try:
        await storage_access.check(
            db, location.id, expected_revision=maintenance_revision, lock=True
        )
    except ContentError as exc:
        raise filesystem_error(exc) from None
    return result


def _probe_worker(
    location_id,
    revision,
    actor_id,
    node_id,
    sample_file,
    api_key_id=None,
    key_proof=None,
    maintenance_revision=1,
):
    from app.workers.storage_probe import probe_location

    task = None
    try:
        with probe_location.app.connection_for_write(
            connect_timeout=1,
            transport_options={
                "socket_connect_timeout": 1,
                "socket_timeout": 1,
                "max_retries": 0,
            },
        ) as connection:
            task = probe_location.apply_async(
                args=[
                    str(location_id),
                    revision,
                    str(actor_id),
                    node_id,
                    sample_file,
                    str(api_key_id) if api_key_id else None,
                    key_proof,
                    maintenance_revision,
                ],
                queue=worker_direct(node_id),
                connection=connection,
                retry=False,
                expires=10,
            )
        return task.get(timeout=3)
    except CeleryTimeoutError:
        return {"ok": False, "code": "node_timeout", "capabilities": {}}
    except Exception:
        return {"ok": False, "code": "node_unavailable", "capabilities": {}}
    finally:
        if task is not None:
            try:
                task.forget()
            except Exception:
                pass  # Cleanup must not replace the probe outcome.


@router.post("/{location_id}/probe", response_model=StorageProbeOut)
async def probe(
    location_id: uuid.UUID,
    payload: StorageProbeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    assert_request_scopes(request, "datasets:write")
    key_id = getattr(request.state, "api_key_id", None)
    key_proof = await key_authority(db, actor, key_id, "datasets:write")
    actor = await require_manager(db, actor.id)
    location = await svc.get_visible(db, location_id, actor)
    svc.require_reference_backend(location)
    if payload.sample_file is not None and location.kind == "filesystem":
        try:
            from app.services.filesystem_storage import relative_path

            relative_path(payload.sample_file)
        except (FilesystemStorageError, ContentError) as exc:
            raise filesystem_error(exc) from None
    from app.services import storage_access

    try:
        maintenance_revision = await storage_access.check(db, location.id)
    except ContentError as exc:
        raise filesystem_error(exc) from None
    revision, actor_id = location.config_revision, actor.id
    observed_at = datetime.now(timezone.utc)
    node_id = (
        svc.api_node_id() if payload.node_id == "api" else f"worker:{payload.node_id}"
    )
    try:
        result = (
            await svc.perform_probe(
                db,
                location,
                node_id,
                payload.sample_file,
                maintenance_revision=maintenance_revision,
            )
            if payload.node_id == "api"
            else await asyncio.wait_for(
                asyncio.to_thread(
                    _probe_worker,
                    location_id,
                    revision,
                    actor_id,
                    payload.node_id,
                    payload.sample_file,
                    key_id,
                    key_proof,
                    maintenance_revision,
                ),
                timeout=5,
            )
        )
    except TimeoutError:
        result = {"ok": False, "code": "node_timeout", "capabilities": {}}
    except ContentError as exc:
        raise filesystem_error(exc) from None
    if result.get("code") in {
        "storage_maintenance",
        "storage_maintenance_changed",
        "storage_maintenance_busy",
        "storage_operation_unsettled",
    }:
        raise HTTPException(
            503 if result["code"] == "storage_operation_unsettled" else 409,
            detail={"code": result["code"]},
        )
    actor = await require_manager(db, actor.id)
    if await key_authority(db, actor, key_id, "datasets:write") != key_proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    location = await svc.get_visible(db, location_id, actor, lock=True)
    if location.config_revision != revision or not location.enabled:
        raise HTTPException(409, "Storage configuration revision changed")
    try:
        await storage_access.check(
            db, location.id, expected_revision=maintenance_revision, lock=True
        )
        result = {**result, "maintenance_revision": maintenance_revision}
        await svc.store_probe(db, location, node_id, result, observed_at)
    except ContentError as exc:
        raise filesystem_error(exc) from None
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.STORAGE_LOCATION_PROBE,
        target_type="storage_location",
        target_id=str(location_id),
        request=request,
        status_code=200,
        detail={"ok": result["ok"], "code": result["code"]},
    )
    await db.commit()
    row = await db.get(
        StorageLocationProbe, (location_id, node_id), populate_existing=True
    )
    return svc.probe_out(row, location)


@router.get("/{location_id}/probes", response_model=list[StorageProbeOut])
async def probes(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    location = await svc.get_visible(db, location_id, actor)
    rows = await db.scalars(
        select(StorageLocationProbe)
        .where(StorageLocationProbe.location_id == location_id)
        .order_by(StorageLocationProbe.node_id)
    )
    return [svc.probe_out(row, location) for row in rows]
