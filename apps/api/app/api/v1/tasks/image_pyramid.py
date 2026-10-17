from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_access
from app.api.v1.tasks._shared import (
    _load_task_or_404,
)
from app.config import settings
from app.core.ratelimit import limiter
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
)
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_scopes
from app.observability.metrics import (
    IMAGE_PYRAMID_API_REQUESTS_TOTAL,
    IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL,
)
from app.schemas.image_pyramid import (
    ImagePyramidAssetUrl,
    ImagePyramidAssetUrlsRequest,
    ImagePyramidAssetUrlsResponse,
    ImagePyramidManifest,
    ImagePyramidOverviewUrl,
    ImagePyramidResponse,
    ImagePyramidRetryResponse,
)
from app.services.image_pyramid import (
    ImagePyramidError,
    generation_manifest,
    get_active_generation_for_task,
    invalidate_ready_generation,
    manifest_etag,
    pyramid_eligible,
    pyramid_required,
    source_matches_generation,
    tile_key,
    utcnow,
)
from app.services.storage import storage_service
from app.workers.image_pyramid import enqueue_image_pyramid

router = APIRouter()


async def _latest_generation(
    db: AsyncSession, asset: ImagePyramidAsset
) -> ImagePyramidGeneration | None:
    return (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(ImagePyramidGeneration.asset_id == asset.id)
            .order_by(ImagePyramidGeneration.generation.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _building_generation(
    db: AsyncSession, asset: ImagePyramidAsset
) -> ImagePyramidGeneration | None:
    if asset.building_generation is None:
        return None
    return await db.get(ImagePyramidGeneration, (asset.id, asset.building_generation))


def _status_response(
    *,
    task_id: uuid.UUID,
    status_value: str,
    required: bool,
    generation: ImagePyramidGeneration | None = None,
    retryable: bool = False,
    retry_after_ms: int | None = None,
    error_code: str | None = None,
) -> ImagePyramidResponse:
    return ImagePyramidResponse(
        task_id=task_id,
        status=status_value,  # type: ignore[arg-type]
        required=required,
        retryable=retryable,
        retry_after_ms=retry_after_ms,
        generation=generation.generation if generation else None,
        error_code=error_code,
    )


@router.get(
    "/{task_id}/image-pyramid",
    response_model=ImagePyramidResponse,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_image_pyramid(
    task_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user)
    owner, asset, generation = await get_active_generation_for_task(db, task)
    required = pyramid_required(owner.width, owner.height)
    response.headers["Cache-Control"] = "private, no-store"

    if owner.file_type != "image":
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="manifest", outcome="not_available"
        ).inc()
        return _status_response(
            task_id=task.id, status_value="not_available", required=False
        )

    if asset is None:
        outcome = (
            "missing"
            if pyramid_eligible(owner.width, owner.height)
            else "not_available"
        )
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="manifest", outcome=outcome
        ).inc()
        return _status_response(
            task_id=task.id,
            status_value=outcome,
            required=required,
            retryable=outcome == "missing",
        )

    if generation is None or generation.status != "ready":
        latest = await _latest_generation(db, asset)
        if latest is None:
            status_value = "missing"
            retryable = True
            retry_after_ms = None
            error_code = None
        elif latest.status in {"pending", "building"}:
            status_value = latest.status
            retryable = False
            retry_after_ms = 2_000
            error_code = None
        else:
            status_value = "failed"
            retryable = True
            elapsed = max(0.0, (utcnow() - latest.updated_at).total_seconds())
            retry_after_ms = max(
                0,
                int((settings.image_pyramid_retry_cooldown_seconds - elapsed) * 1000),
            )
            error_code = latest.error_code
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="manifest", outcome=status_value
        ).inc()
        return _status_response(
            task_id=task.id,
            status_value=status_value,
            required=required,
            generation=latest,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            error_code=error_code,
        )

    if not source_matches_generation(owner, generation):
        await invalidate_ready_generation(db, asset, generation, "source_changed")
        if settings.image_pyramid_auto_generate:
            enqueue_image_pyramid(owner.kind, owner.id, force=True)
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="manifest", outcome="stale"
        ).inc()
        return _status_response(
            task_id=task.id,
            status_value="stale",
            required=required,
            generation=generation,
            retryable=True,
            error_code="source_changed",
        )

    try:
        manifest = generation_manifest(generation)
        if not generation.overview_key or not storage_service.verify_upload(
            generation.overview_key,
            bucket=storage_service.media_cache_bucket,
        ):
            raise ImagePyramidError("object_missing")
    except ImagePyramidError as exc:
        await invalidate_ready_generation(db, asset, generation, exc.code)
        if settings.image_pyramid_auto_generate:
            enqueue_image_pyramid(owner.kind, owner.id, force=True)
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="manifest", outcome="inconsistent"
        ).inc()
        return _status_response(
            task_id=task.id,
            status_value="inconsistent",
            required=required,
            generation=generation,
            retryable=True,
            error_code=exc.code,
        )

    etag = manifest_etag(generation)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=60"
    if request.headers.get("if-none-match") == etag:
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="manifest", outcome="not_modified"
        ).inc()
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "private, max-age=60"},
        )

    expiry = utcnow() + timedelta(seconds=settings.image_pyramid_url_expiry_seconds)
    overview_url = storage_service.generate_download_url(
        generation.overview_key,
        expires_in=settings.image_pyramid_url_expiry_seconds,
        bucket=storage_service.media_cache_bucket,
        align=False,
    )
    building = await _building_generation(db, asset)
    if (
        generation.last_accessed_at is None
        or generation.last_accessed_at < utcnow() - timedelta(minutes=10)
    ):
        generation.last_accessed_at = utcnow()
        await db.commit()
    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(operation="manifest", outcome="ready").inc()
    return ImagePyramidResponse(
        task_id=task.id,
        status="ready",
        required=pyramid_required(generation.width, generation.height),
        generation=generation.generation,
        building_generation=building.generation if building else None,
        building_status=(
            building.status
            if building is not None and building.status in {"pending", "building"}
            else None
        ),
        manifest=ImagePyramidManifest.model_validate(manifest),
        overview=ImagePyramidOverviewUrl(url=overview_url, expires_at=expiry),
    )


@router.post(
    "/{task_id}/image-pyramid/asset-urls",
    response_model=ImagePyramidAssetUrlsResponse,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
@limiter.limit("120/minute")
async def create_image_pyramid_asset_urls(
    task_id: uuid.UUID,
    payload: ImagePyramidAssetUrlsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if len(payload.items) > settings.image_pyramid_asset_url_batch_max:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "batch_too_large",
                "max_items": settings.image_pyramid_asset_url_batch_max,
            },
        )
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user)
    owner, asset, generation = await get_active_generation_for_task(db, task)
    if asset is None or generation is None or generation.status != "ready":
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="asset_urls", outcome="not_ready"
        ).inc()
        raise HTTPException(status_code=409, detail={"reason": "asset_not_ready"})
    if not source_matches_generation(owner, generation):
        await invalidate_ready_generation(db, asset, generation, "source_changed")
        raise HTTPException(
            status_code=409,
            detail={"reason": "stale_generation", "active_generation": None},
        )

    try:
        manifest = generation_manifest(generation)
    except ImagePyramidError as exc:
        await invalidate_ready_generation(db, asset, generation, exc.code)
        raise HTTPException(
            status_code=409, detail={"reason": "inconsistent_ready"}
        ) from exc
    levels = {int(level["level"]): level for level in manifest["levels"]}
    unique: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    for item in payload.items:
        if item.kind == "overview":
            identity = ("overview", item.generation)
        else:
            identity = ("tile", item.generation, item.level, item.x, item.y)
        if identity not in seen:
            seen.add(identity)
            unique.append(item)

    expiry = utcnow() + timedelta(seconds=settings.image_pyramid_url_expiry_seconds)
    output: list[ImagePyramidAssetUrl] = []
    for item in unique:
        if item.generation != generation.generation:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stale_generation",
                    "active_generation": generation.generation,
                },
            )
        if item.kind == "overview":
            key = generation.overview_key
            if not key:
                raise HTTPException(
                    status_code=409, detail={"reason": "inconsistent_ready"}
                )
            output_item = {
                "kind": "overview",
                "generation": generation.generation,
            }
        else:
            level = levels.get(item.level)
            if (
                level is None
                or item.x >= int(level["columns"])
                or item.y >= int(level["rows"])
            ):
                raise HTTPException(
                    status_code=422, detail={"reason": "invalid_coordinate"}
                )
            key = tile_key(
                asset.id,
                generation.generation,
                item.level,
                item.x,
                item.y,
            )
            output_item = {
                "kind": "tile",
                "generation": generation.generation,
                "level": item.level,
                "x": item.x,
                "y": item.y,
            }
        if not storage_service.verify_upload(
            key, bucket=storage_service.media_cache_bucket
        ):
            await invalidate_ready_generation(db, asset, generation, "object_missing")
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="asset_urls", outcome="object_missing"
            ).inc()
            raise HTTPException(
                status_code=409, detail={"reason": "inconsistent_ready"}
            )
        url = storage_service.generate_download_url(
            key,
            expires_in=settings.image_pyramid_url_expiry_seconds,
            bucket=storage_service.media_cache_bucket,
            align=False,
        )
        output.append(ImagePyramidAssetUrl(**output_item, url=url))
        IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL.labels(kind=item.kind).inc()

    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
        operation="asset_urls", outcome="success"
    ).inc()
    return ImagePyramidAssetUrlsResponse(
        task_id=task.id,
        generation=generation.generation,
        expires_at=expiry,
        items=output,
    )


@router.post(
    "/{task_id}/image-pyramid/retry",
    response_model=ImagePyramidRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
@limiter.limit("10/minute")
async def retry_image_pyramid(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user)
    owner, asset, generation = await get_active_generation_for_task(db, task)
    if owner.file_type != "image":
        raise HTTPException(status_code=409, detail={"reason": "not_image"})
    if (
        owner.width is not None
        and owner.height is not None
        and not pyramid_eligible(owner.width, owner.height)
    ):
        raise HTTPException(status_code=409, detail={"reason": "not_eligible"})

    invalidated_stale = False
    if generation is not None and generation.status == "ready":
        if source_matches_generation(owner, generation):
            raise HTTPException(status_code=409, detail={"reason": "already_ready"})
        if asset is not None:
            await invalidate_ready_generation(db, asset, generation, "source_changed")
            invalidated_stale = True

    if asset is not None:
        latest = await _latest_generation(db, asset)
        if latest is not None and latest.status in {"pending", "building"}:
            return ImagePyramidRetryResponse(
                task_id=task.id,
                status=latest.status,
            )
        if latest is not None and latest.status == "failed" and not invalidated_stale:
            next_retry = latest.updated_at + timedelta(
                seconds=settings.image_pyramid_retry_cooldown_seconds
            )
            if next_retry > utcnow():
                retry_after = max(1, int((next_retry - utcnow()).total_seconds()))
                raise HTTPException(
                    status_code=429,
                    detail={
                        "reason": "retry_cooldown",
                        "retry_after_seconds": retry_after,
                    },
                    headers={"Retry-After": str(retry_after)},
                )

    celery_task_id = enqueue_image_pyramid(owner.kind, owner.id, force=True)
    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(operation="retry", outcome="queued").inc()
    return ImagePyramidRetryResponse(
        task_id=task.id,
        status="queued",
        celery_task_id=celery_task_id,
    )
