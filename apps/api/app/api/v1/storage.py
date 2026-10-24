import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.deps import get_current_user, get_db, require_roles, assert_request_scopes
from app.db.enums import PlatformRole
from app.db.models.dataset import DatasetItem, VideoChunk, VideoFrameCache
from app.db.models.video_derivative import VideoDerivativeOutput
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.storage import storage_service
from app.schemas.storage import BucketsResponse, UsageInventoryResponse
from app.services import storage_usage

router = APIRouter()

#: Global media/asset maintenance is a platform-administrator surface.  These
#: endpoints are not project-path scoped; a project-scoped asset is additionally
#: restricted to its legitimate project manager.
_MEDIA_MANAGERS = (PlatformRole.SUPER_ADMIN, PlatformRole.PROJECT_ADMIN)


def _managed_project_scope(user: User) -> list:
    """Extra predicate for project-scoped media rows.

    Super administrators keep global scope.  A platform project administrator
    may keep dataset-only rows (no owning project) but only sees/retries assets
    that belong to a project they legitimately own.
    """

    if user.role == PlatformRole.SUPER_ADMIN.value:
        return []
    return [or_(Project.id.is_(None), Project.owner_id == user.id)]


async def _assert_media_asset_manager(
    db: AsyncSession, item: DatasetItem, user: User
) -> None:
    """Recheck project ownership for a project-scoped dataset asset.

    Mirrors the failures-list scope: a dataset item can be linked to tasks in
    several projects, so the asset is manageable when ANY linked task belongs
    to a project this manager legitimately owns.  Dataset-only media (no linked
    task) keeps the platform-manager policy.
    """

    if user.role == PlatformRole.SUPER_ADMIN.value:
        return
    linked = (
        await db.execute(
            select(Task.id).where(Task.dataset_item_id == item.id).limit(1)
        )
    ).scalar_one_or_none()
    if linked is None:
        return
    managed = (
        await db.execute(
            select(Task.id)
            .join(Project, Project.id == Task.project_id)
            .where(Task.dataset_item_id == item.id, Project.owner_id == user.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if managed is None:
        raise HTTPException(
            status_code=403, detail="仅项目负责人或超级管理员可管理该项目的媒体资源"
        )


VideoAssetKind = Literal["probe", "poster", "frame_timetable", "chunk", "frame"]


@router.get("/health")
async def storage_health(_: User = Depends(get_current_user)):
    try:
        await run_in_threadpool(
            storage_service.client.head_bucket, Bucket=storage_service.bucket
        )
        return {"status": "ok", "bucket": storage_service.bucket}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/buckets", response_model=BucketsResponse)
async def storage_buckets(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    await storage_usage.require_admin(db, current_user.id)
    return await storage_usage.read_usage(db)


@router.post("/usage-inventory", response_model=UsageInventoryResponse, status_code=202)
async def start_usage_inventory(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(PlatformRole.SUPER_ADMIN)),
):
    assert_request_scopes(request, "*")
    from app.workers.storage_usage import dispatch

    job, reused = await storage_usage.start(
        db, current_user, getattr(request.state, "api_key_id", None)
    )
    job_id = job.id
    if reused:
        await db.commit()
    else:
        await dispatch(db, job)
    return UsageInventoryResponse(job_id=job_id, reused=reused)


class VideoAssetFailureItem(BaseModel):
    asset_key: str
    asset_type: VideoAssetKind
    dataset_item_id: uuid.UUID
    file_name: str
    task_id: uuid.UUID | None = None
    task_display_id: str | None = None
    project_id: uuid.UUID | None = None
    project_name: str | None = None
    error: str
    updated_at: datetime | None = None
    chunk_id: int | None = None
    frame_index: int | None = None
    width: int | None = None
    format: str | None = None


class VideoAssetFailuresResponse(BaseModel):
    items: list[VideoAssetFailureItem]
    total: int
    limit: int
    offset: int


class VideoAssetRetryRequest(BaseModel):
    asset_type: VideoAssetKind
    dataset_item_id: uuid.UUID
    chunk_id: int | None = None
    frame_index: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1, le=4096)
    format: Literal["webp", "jpeg"] | None = None


class VideoAssetRetryResponse(BaseModel):
    status: Literal["queued", "dispatch_pending"]
    asset_type: VideoAssetKind
    dataset_item_id: uuid.UUID
    job_id: uuid.UUID


def _task_context(
    task: Task | None, project: Project | None
) -> dict[str, uuid.UUID | str | None]:
    return {
        "task_id": task.id if task else None,
        "task_display_id": task.display_id if task else None,
        "project_id": project.id if project else None,
        "project_name": project.name if project else None,
    }


def _metadata_error_items(
    item: DatasetItem,
    task: Task | None,
    project: Project | None,
    ownership_error: str | None = None,
) -> list[VideoAssetFailureItem]:
    video_meta = (item.metadata_ or {}).get("video") or {}
    mapping: list[tuple[VideoAssetKind, str]] = [
        ("probe", "probe_error"),
        ("poster", "poster_error"),
        ("frame_timetable", "frame_timetable_error"),
    ]
    out: list[VideoAssetFailureItem] = []
    if ownership_error and not video_meta.get("probe_error"):
        video_meta = {**video_meta, "probe_error": ownership_error}
    for asset_type, field_name in mapping:
        raw_error = video_meta.get(field_name)
        if not raw_error:
            continue
        out.append(
            VideoAssetFailureItem(
                asset_key=f"{item.id}:{asset_type}",
                asset_type=asset_type,
                dataset_item_id=item.id,
                file_name=item.file_name,
                error=str(raw_error),
                updated_at=item.updated_at,
                **_task_context(task, project),
            )
        )
    return out


async def _metadata_ownership_error(db, item):
    from app.services import video_derivative_originals as originals
    from app.services import video_derivatives as derivatives
    from app.services import video_derivative_storage as physical
    from app.services.content_snapshot import ContentError

    if item.video_output_id is None and item.video_building_output_id is not None:
        active = await db.get(VideoDerivativeOutput, item.video_building_output_id)
        if (
            active is not None
            and active.state == "building"
            and (
                active.lease_token is None
                or active.lease_expires_at > derivatives.now()
            )
        ):
            return None
    try:
        source = await originals.pin(db, "dataset_item", item.id)
        await derivatives.metadata(db, item, source)
    except ContentError as exc:
        return exc.code
    except (physical.VideoDerivativeStorageError, ValueError):
        return "video_metadata_unpinned"
    return None


async def _retry_metadata(db, item, source, authority):
    from app.services import video_derivatives as derivatives
    from app.services.content_snapshot import ContentError

    try:
        return await derivatives.metadata(db, item, source)
    except ContentError as exc:
        if exc.code not in {
            "video_metadata_unpinned",
            "video_metadata_changed",
            "video_metadata_invalid",
        }:
            raise
    output, needed = await derivatives.prepare(db, item.id, authority, "metadata")
    output_id = output.id
    await db.commit()
    if needed:
        derivatives.dispatch([output_id])
    raise HTTPException(
        503, detail={"code": "video_metadata_preparing"}, headers={"Retry-After": "2"}
    )


@router.get("/video-assets/failures", response_model=VideoAssetFailuresResponse)
async def list_video_asset_failures(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MEDIA_MANAGERS)),
):
    project_scope = _managed_project_scope(current_user)
    rows = (
        await db.execute(
            select(DatasetItem, Task, Project)
            .outerjoin(Task, Task.dataset_item_id == DatasetItem.id)
            .outerjoin(Project, Project.id == Task.project_id)
            .where(DatasetItem.file_type == "video", *project_scope)
        )
    ).all()

    items_by_key: dict[str, VideoAssetFailureItem] = {}
    ownership_errors = {}
    for item, task, project in rows:
        if item.id not in ownership_errors:
            ownership_errors[item.id] = await _metadata_ownership_error(db, item)
        for failure in _metadata_error_items(
            item, task, project, ownership_errors[item.id]
        ):
            items_by_key.setdefault(failure.asset_key, failure)

    chunk_rows = (
        await db.execute(
            select(VideoChunk, DatasetItem, Task, Project)
            .join(DatasetItem, DatasetItem.id == VideoChunk.dataset_item_id)
            .outerjoin(Task, Task.dataset_item_id == DatasetItem.id)
            .outerjoin(Project, Project.id == Task.project_id)
            .outerjoin(
                VideoDerivativeOutput, VideoDerivativeOutput.id == VideoChunk.output_id
            )
            .where(
                or_(
                    VideoChunk.status == "failed",
                    VideoChunk.output_id.is_(None),
                    VideoDerivativeOutput.id.is_(None),
                    VideoDerivativeOutput.state.in_(["retiring", "deleted"]),
                    VideoDerivativeOutput.kind != "chunk",
                    VideoDerivativeOutput.dataset_item_id != DatasetItem.id,
                    DatasetItem.video_output_id.is_(None),
                    VideoDerivativeOutput.metadata_output_id
                    != DatasetItem.video_output_id,
                ),
                *project_scope,
            )
        )
    ).all()
    for chunk, item, task, project in chunk_rows:
        key = f"chunk:{chunk.id}"
        items_by_key.setdefault(
            key,
            VideoAssetFailureItem(
                asset_key=key,
                asset_type="chunk",
                dataset_item_id=item.id,
                file_name=item.file_name,
                error=chunk.error or "video_derivative_unrecorded_or_stale",
                updated_at=chunk.updated_at,
                chunk_id=chunk.chunk_id,
                **_task_context(task, project),
            ),
        )

    frame_rows = (
        await db.execute(
            select(VideoFrameCache, DatasetItem, Task, Project)
            .join(DatasetItem, DatasetItem.id == VideoFrameCache.dataset_item_id)
            .outerjoin(Task, Task.dataset_item_id == DatasetItem.id)
            .outerjoin(Project, Project.id == Task.project_id)
            .outerjoin(
                VideoDerivativeOutput,
                VideoDerivativeOutput.id == VideoFrameCache.output_id,
            )
            .where(
                or_(
                    VideoFrameCache.status == "failed",
                    VideoFrameCache.output_id.is_(None),
                    VideoDerivativeOutput.id.is_(None),
                    VideoDerivativeOutput.state.in_(["retiring", "deleted"]),
                    VideoDerivativeOutput.kind != "frame",
                    VideoDerivativeOutput.dataset_item_id != DatasetItem.id,
                    DatasetItem.video_output_id.is_(None),
                    VideoDerivativeOutput.metadata_output_id
                    != DatasetItem.video_output_id,
                ),
                *project_scope,
            )
        )
    ).all()
    for frame, item, task, project in frame_rows:
        key = f"frame:{frame.id}"
        items_by_key.setdefault(
            key,
            VideoAssetFailureItem(
                asset_key=key,
                asset_type="frame",
                dataset_item_id=item.id,
                file_name=item.file_name,
                error=frame.error or "video_derivative_unrecorded_or_stale",
                updated_at=frame.updated_at,
                frame_index=frame.frame_index,
                width=frame.width,
                format=frame.format,
                **_task_context(task, project),
            ),
        )

    items = sorted(
        items_by_key.values(),
        key=lambda it: it.updated_at.timestamp() if it.updated_at else 0,
        reverse=True,
    )
    return VideoAssetFailuresResponse(
        items=items[offset : offset + limit],
        total=len(items),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/video-assets/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=VideoAssetRetryResponse,
)
async def retry_video_asset(
    payload: VideoAssetRetryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MEDIA_MANAGERS)),
):
    from app.services import video_derivative_originals as originals
    from app.services import video_derivatives as derivatives
    from app.services import video_asset_retry_jobs as retry_jobs
    from app.services.content_snapshot import ContentError

    item = await db.get(DatasetItem, payload.dataset_item_id)
    if not item or item.file_type != "video":
        raise HTTPException(status_code=404, detail="Video item not found")
    await _assert_media_asset_manager(db, item, current_user)
    try:
        authority = await originals.authorize_policy(
            db, "storage", item.id, current_user, request
        )
        source = await originals.check(db, authority)
        kind, spec, slot = "metadata", None, None
        if payload.asset_type == "chunk":
            if payload.chunk_id is None:
                raise HTTPException(status_code=400, detail="chunk_id is required")
            slot = await db.scalar(
                select(VideoChunk).where(
                    VideoChunk.dataset_item_id == item.id,
                    VideoChunk.chunk_id == payload.chunk_id,
                )
            )
            if slot is None:
                raise HTTPException(status_code=404, detail="Video chunk not found")
            generation = await _retry_metadata(db, item, source, authority)
            kind, spec = "chunk", derivatives.chunk_spec(generation, payload.chunk_id)
        elif payload.asset_type == "frame":
            if (
                payload.frame_index is None
                or payload.width is None
                or payload.format is None
            ):
                raise HTTPException(
                    status_code=400,
                    detail="frame_index, width and format are required for frame retry",
                )
            slot = await db.scalar(
                select(VideoFrameCache).where(
                    VideoFrameCache.dataset_item_id == item.id,
                    VideoFrameCache.frame_index == payload.frame_index,
                    VideoFrameCache.width == payload.width,
                    VideoFrameCache.format == payload.format,
                )
            )
            if slot is None:
                raise HTTPException(
                    status_code=404, detail="Video frame cache row not found"
                )
            generation = await _retry_metadata(db, item, source, authority)
            kind, spec = (
                "frame",
                derivatives.frame_spec(
                    generation, payload.frame_index, payload.width, payload.format
                ),
            )
        active_id = (
            item.video_building_output_id if kind == "metadata" else slot.output_id
        )
        active = await db.get(VideoDerivativeOutput, active_id) if active_id else None
        output, needed = await derivatives.prepare(
            db,
            item.id,
            authority,
            kind,
            spec=spec,
            force=not (active is not None and active.state == "building"),
        )
        output_id = output.id
        job = await retry_jobs.observe(db, output, current_user.id, payload.asset_type)
        job_id = job.id
        await db.commit()
        dispatch_status = "queued"
        if needed:
            try:
                derivatives.dispatch([output_id])
            except Exception:
                await retry_jobs.dispatch_pending(db, job_id)
                await db.commit()
                dispatch_status = "dispatch_pending"
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    return VideoAssetRetryResponse(
        status=dispatch_status,
        asset_type=payload.asset_type,
        dataset_item_id=item.id,
        job_id=job_id,
    )
