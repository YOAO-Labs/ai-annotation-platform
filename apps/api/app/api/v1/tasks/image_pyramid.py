from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import _image_pyramid_task_source, _load_task_or_404
from app.application.image_pyramid import bind_image_pyramid_api
from app.config import settings
from app.core.ratelimit import limiter
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_scopes
from app.observability.metrics import IMAGE_PYRAMID_API_REQUESTS_TOTAL
from app.schemas.image_pyramid import (
    ImagePyramidAssetUrlsRequest,
    ImagePyramidAssetUrlsResponse,
    ImagePyramidResponse,
    ImagePyramidRetryResponse,
)
from app.services import task_access
from app.services.image_pyramid_contract import ImagePyramidFailure

router = APIRouter()


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
    await task_access.assert_task_visible(db, task, current_user.id)
    response.headers["Cache-Control"] = "private, no-store"
    image_pyramid = bind_image_pyramid_api(db, actor_id=current_user.id)
    try:
        result = await image_pyramid.manifest(
            _image_pyramid_task_source(task),
            if_none_match=request.headers.get("if-none-match"),
        )
    except ImagePyramidFailure as exc:
        if exc.metric_outcome is not None:
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="manifest", outcome=exc.metric_outcome
            ).inc()
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc

    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
        operation="manifest", outcome=result.outcome
    ).inc()
    response.headers["Cache-Control"] = result.cache_control
    if result.etag is not None:
        response.headers["ETag"] = result.etag
    if result.body is None:
        assert result.etag is not None
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "ETag": result.etag,
                "Cache-Control": result.cache_control,
            },
        )
    return result.body


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
    await task_access.assert_task_visible(db, task, current_user.id)
    image_pyramid = bind_image_pyramid_api(db, actor_id=current_user.id)
    try:
        result = await image_pyramid.asset_urls(
            _image_pyramid_task_source(task), payload
        )
    except ImagePyramidFailure as exc:
        if exc.metric_outcome is not None:
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="asset_urls", outcome=exc.metric_outcome
            ).inc()
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc
    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
        operation="asset_urls", outcome="success"
    ).inc()
    return result


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
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    image_pyramid = bind_image_pyramid_api(db, actor_id=current_user.id)
    try:
        plan = await image_pyramid.retry(
            _image_pyramid_task_source(task),
            operation_id=idempotency_key or uuid.uuid4(),
        )
    except ImagePyramidFailure as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc

    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
        operation="retry", outcome=plan.status
    ).inc()
    return ImagePyramidRetryResponse(
        task_id=task.id,
        status=plan.status,
        job_id=plan.job_id,
    )
