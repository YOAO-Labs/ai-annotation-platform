import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from aap_assets_media.contract import (
    FailuresRequest,
    MediaFailure,
    RetryRequest as MediaRetryRequest,
    RetrySpec,
)

from app.deps import (
    get_current_user,
    get_db,
    require_roles,
)
from app.db.enums import PlatformRole
from app.services.storage import storage_service
from app.services.assets_media import media_gateway, media_http_error
from app.schemas.storage import BucketSummary, BucketsResponse
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()

#: Global media/asset maintenance is a platform-administrator surface.  These
#: endpoints are not project-path scoped; a project-scoped asset is additionally
#: restricted to its legitimate project manager.
_MEDIA_MANAGERS = (PlatformRole.SUPER_ADMIN, PlatformRole.PROJECT_ADMIN)


VideoAssetKind = Literal["probe", "poster", "frame_timetable", "chunk", "frame"]


@router.get("/health")
async def storage_health(_: IdentityActorFacts = Depends(get_current_user)):
    try:
        storage_service.client.head_bucket(Bucket=storage_service.bucket)
        return {"status": "ok", "bucket": storage_service.bucket}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/buckets", response_model=BucketsResponse)
async def storage_buckets(_: IdentityActorFacts = Depends(get_current_user)):
    bucket_roles = {
        storage_service.bucket: "annotations",
        storage_service.datasets_bucket: "datasets",
        storage_service.bug_reports_bucket: "bug-reports",
        storage_service.media_cache_bucket: "media-cache",
        storage_service.audit_archive_bucket: "audit-archive",
        storage_service.avatars_bucket: "avatars",
    }
    items: list[BucketSummary] = []
    for b, role in bucket_roles.items():
        summary = storage_service.summarize_bucket(b)
        items.append(BucketSummary(role=role, **summary))

    return BucketsResponse(
        items=items,
        total_object_count=sum(i.object_count for i in items),
        total_size_bytes=sum(i.total_size_bytes for i in items),
    )


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
    status: Literal["queued"]
    asset_type: VideoAssetKind
    dataset_item_id: uuid.UUID


@router.get("/video-assets/failures", response_model=VideoAssetFailuresResponse)
async def list_video_asset_failures(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MEDIA_MANAGERS)),
):
    try:
        async with media_gateway(db) as gateway:
            page = await gateway.call(
                "read_video_failures",
                FailuresRequest(actor_id=current_user.id, limit=limit, offset=offset),
            )
    except MediaFailure as exc:
        raise media_http_error(exc) from None
    return VideoAssetFailuresResponse(
        items=[
            VideoAssetFailureItem.model_validate(item.model_dump())
            for item in page.items
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/video-assets/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=VideoAssetRetryResponse,
)
async def retry_video_asset(
    payload: VideoAssetRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MEDIA_MANAGERS)),
):
    if payload.asset_type == "chunk":
        if payload.chunk_id is None:
            raise HTTPException(status_code=400, detail="chunk_id is required")
    elif payload.asset_type == "frame" and (
        payload.frame_index is None or payload.width is None or payload.format is None
    ):
        raise HTTPException(
            status_code=400,
            detail="frame_index, width and format are required for frame retry",
        )
    request = MediaRetryRequest(
        operation_id=uuid.uuid4(),
        actor_id=current_user.id,
        spec=RetrySpec(
            asset_type=payload.asset_type,
            dataset_item_id=payload.dataset_item_id,
            chunk_id=payload.chunk_id,
            frame_index=payload.frame_index,
            width=payload.width,
            format=payload.format,
        ),
    )
    try:
        async with media_gateway(db) as gateway:
            await gateway.call("retry_video_media", request)
        await db.commit()
    except MediaFailure as exc:
        raise media_http_error(exc) from None
    return VideoAssetRetryResponse(
        status="queued",
        asset_type=payload.asset_type,
        dataset_item_id=payload.dataset_item_id,
    )
