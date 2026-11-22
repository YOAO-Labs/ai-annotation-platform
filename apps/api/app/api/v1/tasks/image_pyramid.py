from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yoao_assets_images.contract import (
    ImageFailure,
    PrepareOwnedPyramidRequest,
    RetryOwnedPyramidRequest,
    http_error_status,
)
from yoao_assets_images.transport import ImageOverviewRequest, ImageTileRequest

from app.api.v1.tasks._shared import (
    _load_task_or_404,
)
from app.config import settings
from app.core.ratelimit import limiter
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services import task_access
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
    get_active_generation_for_task,
    manifest_etag,
    native_generation,
    owner_asset_references,
    owner_generation_read,
    owner_generation_reference,
    pyramid_levels,
    pyramid_eligible,
    pyramid_required,
    source_matches_generation,
    utcnow,
)
from app.services import (
    image_pyramid_originals as originals,
    image_pyramid_objects as objects,
)
from app.services import image_pyramid_storage as physical, storage_access
from app.services.assets_images import image_gateway, image_policy
from app.services.content_snapshot import ContentError
from app.services.storage import storage_service
from functools import wraps
from sqlalchemy.exc import DBAPIError

router = APIRouter()


def _errors(call):
    @wraps(call)
    async def guarded(*args, **kwargs):
        try:
            return await call(*args, **kwargs)
        except ContentError as exc:
            raise HTTPException(exc.status, {"reason": exc.code}) from None
        except ImagePyramidError as exc:
            raise HTTPException(409, {"reason": exc.code}) from None
        except physical.ImagePyramidStorageError as exc:
            raise HTTPException(exc.status_code, {"reason": exc.code}) from None
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                raise HTTPException(409, {"reason": "content_busy"}) from None
            raise

    return guarded


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


async def _invalidate_ready(
    db: AsyncSession,
    asset: ImagePyramidAsset,
    generation: ImagePyramidGeneration,
    error_code: str,
) -> None:
    expected_output_id = generation.output_id
    locked_asset = (
        await db.execute(
            select(ImagePyramidAsset)
            .where(ImagePyramidAsset.id == asset.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_asset is None:
        return
    locked_generation = (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == generation.asset_id,
                ImagePyramidGeneration.generation == generation.generation,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        locked_generation is None
        or locked_generation.output_id != expected_output_id
        or locked_generation.status != "ready"
    ):
        return
    if locked_asset.active_generation == locked_generation.generation:
        locked_asset.active_generation = None
    locked_generation.status = "failed"
    locked_generation.error_code = error_code
    await db.commit()


def _status_response(
    *,
    task_id: uuid.UUID,
    status_value: str,
    required: bool,
    generation: ImagePyramidGeneration | None = None,
    retryable: bool = False,
    retry_after_ms: int | None = None,
    error_code: str | None = None,
    profile_version: str | None = None,
    width: int | None = None,
    height: int | None = None,
    building_generation: int | None = None,
    building_status: str | None = None,
    source_snapshot: str | None = None,
) -> ImagePyramidResponse:
    return ImagePyramidResponse(
        task_id=task_id,
        status=status_value,  # type: ignore[arg-type]
        required=required,
        retryable=retryable,
        retry_after_ms=retry_after_ms,
        source_snapshot=(
            source_snapshot
            if source_snapshot is not None
            else (generation.source_snapshot if generation else None)
        ),
        profile_version=profile_version,
        width=width,
        height=height,
        generation=generation.generation if generation else None,
        building_generation=building_generation,
        building_status=building_status,  # type: ignore[arg-type]
        error_code=error_code,
    )


def _owner_refusal(exc: ImageFailure) -> HTTPException:
    """An owner refusal is never evidence that the source changed."""
    return HTTPException(http_error_status(exc.code), {"reason": exc.code})


_OWNER_UNAVAILABLE = {
    "asset_missing": "missing",
    "source_missing": "missing",
    "not_image": "not_available",
    "source_changed": "stale",
}


async def _owner_projection(db: AsyncSession, task, generation, actor_id):
    """Exact-generation projection from the Images owner.

    Returns the owner projection, or a status value when the owner proves the
    generation is unavailable.  Any other refusal stays an HTTP error.
    """
    try:
        result = await owner_generation_read(
            db,
            actor_id=actor_id,
            task_id=task.id,
            dataset_item_id=None,
            generation=generation.generation,
        )
        return result.pyramid
    except ImageFailure as exc:
        unavailable = _OWNER_UNAVAILABLE.get(exc.code)
        if unavailable is not None:
            return unavailable
        raise _owner_refusal(exc) from None


def _summary_response(*, task_id: uuid.UUID, summary) -> ImagePyramidResponse:
    """Project the Images owner's exact Data projection into the public schema."""
    return ImagePyramidResponse(
        task_id=task_id,
        status=summary.status,  # type: ignore[arg-type]
        required=summary.required,
        source_snapshot=summary.source_snapshot,
        profile_version=summary.profile_version,
        width=summary.width,
        height=summary.height,
        retryable=summary.retryable,
        retry_after_ms=summary.retry_after_ms,
        generation=summary.generation,
        building_generation=summary.building_generation,
        building_status=summary.building_status,  # type: ignore[arg-type]
        error_code=summary.error_code,
    )


def _native_manifest_etag(generation, projection) -> str:
    digest = (projection.manifest_digest or "missing").replace("sha256:", "")
    return f'"pyramid-{generation.generation}-{digest}"'


def _native_asset_items(generation, payload) -> list:
    """One deduplicated owner request for the exact saved generation."""
    items: list = []
    seen: set[tuple[Any, ...]] = set()
    for item in payload.items:
        identity = (
            ("overview", item.generation)
            if item.kind == "overview"
            else ("tile", item.generation, item.level, item.x, item.y)
        )
        if identity in seen:
            continue
        seen.add(identity)
        if item.generation != generation.generation:
            raise HTTPException(
                409,
                {
                    "reason": "stale_generation",
                    "active_generation": generation.generation,
                },
            )
        try:
            if item.kind == "overview":
                items.append(ImageOverviewRequest(generation=item.generation))
            else:
                items.append(
                    ImageTileRequest(
                        generation=item.generation,
                        level=item.level,
                        x=item.x,
                        y=item.y,
                    )
                )
        except ValueError:
            raise HTTPException(422, {"reason": "invalid_coordinate"}) from None
    return items


def _native_read_refusal(
    exc: ImageFailure,
    *,
    task,
    generation,
    projection,
    status_response,
) -> ImagePyramidResponse | Response | None:
    """Translate an owner refusal without mutating Native ORM state.

    The Images owner already owns missing-member invalidation/retry for saved
    Native outputs.  The API reports that explicit failure and never rewrites
    the Native generation/output rows itself.
    """
    if exc.code == "source_changed":
        return status_response(
            task_id=task.id,
            status_value="stale",
            required=projection.required,
            generation=generation,
            retryable=True,
            error_code="source_changed",
            profile_version=projection.profile_version,
            width=projection.width,
            height=projection.height,
            source_snapshot=projection.source_snapshot,
        )
    if exc.code in {"object_missing", "invalid_response"}:
        return status_response(
            task_id=task.id,
            status_value="inconsistent",
            required=projection.required,
            generation=generation,
            retryable=True,
            error_code=(
                "image_pyramid_storage_missing"
                if exc.code == "object_missing"
                else "image_pyramid_storage_invalid_receipt"
            ),
            profile_version=projection.profile_version,
            width=projection.width,
            height=projection.height,
            source_snapshot=projection.source_snapshot,
        )
    return None


async def _native_ready_response(
    db,
    task,
    generation,
    projection,
    *,
    authority,
    request,
    response: Response,
    actor_id,
):
    """Publish the saved Native manifest/overview through the Images owner."""
    try:
        await originals.check(db, authority, lock=True)
        await db.commit()
        reference = await owner_generation_reference(
            db,
            actor_id=actor_id,
            task_id=task.id,
            dataset_item_id=None,
            generation=generation.generation,
            expected_source_snapshot=projection.source_snapshot,
            profile_version=projection.profile_version,
        )
    except ImageFailure as exc:
        refusal = _native_read_refusal(
            exc,
            task=task,
            generation=generation,
            projection=projection,
            status_response=_status_response,
        )
        if refusal is not None:
            return refusal
        raise _owner_refusal(exc) from None

    await originals.check(db, authority, lock=True)
    await db.commit()
    if (
        reference.pyramid.status != "ready"
        or reference.manifest is None
        or reference.overview_url is None
    ):
        return _summary_response(task_id=task.id, summary=reference.pyramid)

    etag = _native_manifest_etag(generation, projection)
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

    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(operation="manifest", outcome="ready").inc()
    return ImagePyramidResponse(
        task_id=task.id,
        status="ready",
        required=projection.required,
        source_snapshot=projection.source_snapshot,
        profile_version=projection.profile_version,
        width=projection.width,
        height=projection.height,
        generation=projection.generation,
        building_generation=projection.building_generation,
        building_status=projection.building_status,
        manifest=ImagePyramidManifest.model_validate(
            reference.manifest.model_dump(by_alias=True)
        ),
        overview=ImagePyramidOverviewUrl(
            url=reference.overview_url,
            expires_at=utcnow() + timedelta(seconds=reference.expires_in),
        ),
    )


async def _native_asset_url_response(
    db,
    task,
    generation,
    projection,
    payload,
    *,
    authority,
    actor_id,
):
    """Publish exact saved Native tiles through one Images owner read permit."""
    items = _native_asset_items(generation, payload)
    try:
        await originals.check(db, authority, lock=True)
        await db.commit()
        references = await owner_asset_references(
            db,
            actor_id=actor_id,
            task_id=task.id,
            dataset_item_id=None,
            generation=generation.generation,
            expected_source_snapshot=projection.source_snapshot,
            items=items,
            profile_version=projection.profile_version,
        )
    except ImageFailure as exc:
        if exc.code == "source_changed":
            raise HTTPException(
                409, {"reason": "stale_generation", "active_generation": None}
            ) from None
        if exc.code in {"object_missing", "invalid_response"}:
            raise HTTPException(409, {"reason": "inconsistent_ready"}) from None
        if exc.code == "invalid_coordinate":
            raise HTTPException(422, {"reason": "invalid_coordinate"}) from None
        if exc.code in {
            "asset_missing",
            "source_missing",
            "not_image",
            "source_not_ready",
        }:
            raise HTTPException(409, {"reason": "asset_not_ready"}) from None
        raise _owner_refusal(exc) from None

    await originals.check(db, authority, lock=True)
    await db.commit()
    by_identity = {
        (item.kind, item.generation, item.level, item.x, item.y): item
        for item in references.items
    }
    output: list[ImagePyramidAssetUrl] = []
    for item in items:
        identity = (
            ("overview", item.generation, None, None, None)
            if item.kind == "overview"
            else ("tile", item.generation, item.level, item.x, item.y)
        )
        signed = by_identity.get(identity)
        if signed is None:
            raise _owner_refusal(ImageFailure("invalid_response")) from None
        output.append(
            ImagePyramidAssetUrl(
                kind=signed.kind,
                generation=signed.generation,
                level=signed.level,
                x=signed.x,
                y=signed.y,
                url=signed.url,
            )
        )
        IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL.labels(kind=signed.kind).inc()
    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
        operation="asset_urls", outcome="success"
    ).inc()
    return ImagePyramidAssetUrlsResponse(
        task_id=task.id,
        generation=generation.generation,
        expires_at=utcnow() + timedelta(seconds=references.expires_in),
        items=output,
    )


async def _admit_native_pyramid(
    db: AsyncSession,
    task,
    authority,
    *,
    retry: bool,
    force: bool = True,
):
    """Admit exactly one native attempt through the current Images owner.

    The saved-output allocation happens under the claim lease, so no capacity is
    reserved here.  A failed read-time admission does not turn a GET into an
    error; an explicit retry surfaces the owner's failure instead.
    """
    operation_id = uuid.uuid4()
    request = (RetryOwnedPyramidRequest if retry else PrepareOwnedPyramidRequest)(
        operation_id=operation_id,
        actor_id=authority.actor_id,
        task_id=task.id,
        purpose="pyramid",
        policy=image_policy(),
        force=force,
        delegation=originals.authorization(authority),
    )
    try:
        async with image_gateway(db) as images:
            accepted = await images.call(
                "retry_owned_pyramid" if retry else "prepare_owned_pyramid",
                request,
            )
    except ImageFailure as exc:
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="admit", outcome=exc.code
        ).inc()
        if retry:
            raise
        return None
    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(operation="admit", outcome="queued").inc()
    return accepted


@router.get(
    "/{task_id}/image-pyramid",
    response_model=ImagePyramidResponse,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
@_errors
async def get_image_pyramid(
    task_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if task.file_type != "image":
        return _status_response(
            task_id=task.id, status_value="not_available", required=False
        )
    authority = await originals.authorize(db, task.id, current_user, request)
    try:
        owner, asset, generation = await get_active_generation_for_task(db, task)
    except ImagePyramidError as exc:
        raise HTTPException(409, detail={"reason": exc.code}) from None
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

    if generation is None:
        generation = await _latest_generation(db, asset)
    if not native_generation(generation) and (
        generation is None or generation.status != "ready"
    ):
        latest = generation
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
            profile_version=asset.profile_version,
            width=(latest.width if latest is not None else None) or owner.width,
            height=(latest.height if latest is not None else None) or owner.height,
        )

    projection = None
    if native_generation(generation):
        projection = await _owner_projection(db, task, generation, current_user.id)
        if isinstance(projection, str):
            if projection == "stale" and settings.image_pyramid_auto_generate:
                await originals.check(db, authority, lock=True)
                await db.commit()
                await _admit_native_pyramid(db, task, authority, retry=False)
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="manifest", outcome=projection
            ).inc()
            return _status_response(
                task_id=task.id,
                status_value=projection,
                required=required,
                generation=generation,
                retryable=projection != "not_available",
                error_code="source_changed" if projection == "stale" else None,
                profile_version=asset.profile_version,
                width=generation.width or owner.width,
                height=generation.height or owner.height,
            )
        width = projection.width
        height = projection.height
        required = projection.required
        if projection.status != "ready":
            if projection.status == "stale" and settings.image_pyramid_auto_generate:
                await originals.check(db, authority, lock=True)
                await db.commit()
                await _admit_native_pyramid(db, task, authority, retry=False)
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="manifest", outcome=projection.status
            ).inc()
            return ImagePyramidResponse(
                task_id=task.id,
                status=projection.status,
                required=required,
                source_snapshot=projection.source_snapshot,
                profile_version=projection.profile_version,
                width=width,
                height=height,
                retryable=projection.retryable,
                retry_after_ms=projection.retry_after_ms,
                generation=projection.generation,
                building_generation=projection.building_generation,
                building_status=projection.building_status,
                error_code=projection.error_code,
            )
    elif not source_matches_generation(owner, generation):
        await originals.check(db, authority, lock=True)
        await _invalidate_ready(db, asset, generation, "source_changed")
        if settings.image_pyramid_auto_generate:
            await db.commit()
            await _admit_native_pyramid(db, task, authority, retry=False)
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
            profile_version=asset.profile_version,
            width=generation.width or owner.width,
            height=generation.height or owner.height,
        )

    if native_generation(generation):
        if projection.native is not True or projection.native_output is None:
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="manifest", outcome="unavailable"
            ).inc()
            return _status_response(
                task_id=task.id,
                status_value="inconsistent",
                required=required,
                generation=generation,
                retryable=True,
                error_code="inconsistent_ready",
                profile_version=projection.profile_version,
                width=projection.width,
                height=projection.height,
                source_snapshot=projection.source_snapshot,
            )
        return await _native_ready_response(
            db,
            task,
            generation,
            projection,
            authority=authority,
            request=request,
            response=response,
            actor_id=current_user.id,
        )

    if generation.output_id is None:
        if settings.image_pyramid_auto_generate:
            await db.commit()
            await _admit_native_pyramid(db, task, authority, retry=False)
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="manifest", outcome="unavailable"
        ).inc()
        return _status_response(
            task_id=task.id,
            status_value="missing",
            required=required,
            generation=generation,
            retryable=True,
            profile_version=asset.profile_version,
            width=generation.width or owner.width,
            height=generation.height or owner.height,
        )
    url_expiry_seconds = objects.url_expiry_seconds(generation)
    expiry = utcnow() + timedelta(seconds=url_expiry_seconds)

    await db.commit()
    try:
        async with objects.reading(
            db,
            owner,
            generation,
            authority=authority,
            keys=(generation.manifest_key, generation.overview_key),
        ) as output_owner:
            async with objects.transport(db, output_owner) as storage:
                manifest = await objects.manifest(storage, output_owner, generation)
                await originals.physical(
                    physical.verify_object,
                    storage,
                    output_owner,
                    generation.overview_key,
                )
                overview_url = physical.signed_url(
                    storage,
                    output_owner,
                    generation.overview_key,
                    url_expiry_seconds,
                )
    except (ContentError, physical.ImagePyramidStorageError) as exc:
        if exc.code not in {
            "image_pyramid_storage_missing",
            "image_pyramid_storage_integrity_mismatch",
            "image_pyramid_storage_invalid_receipt",
        }:
            raise
        await originals.check(db, authority, lock=True)
        await _invalidate_ready(db, asset, generation, exc.code)
        if settings.image_pyramid_auto_generate:
            await originals.check(db, authority, lock=True)
            await db.commit()
            await _admit_native_pyramid(db, task, authority, retry=False)
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

    building = await _building_generation(db, asset)
    if (
        generation.last_accessed_at is None
        or generation.last_accessed_at < utcnow() - timedelta(minutes=10)
    ):
        generation.last_accessed_at = utcnow()
        await db.commit()
    await objects.check(
        db,
        owner,
        asset.id,
        generation.generation,
        generation.output_id,
        authority=authority,
    )
    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(operation="manifest", outcome="ready").inc()
    ready_width = (
        projection.width if projection is not None else generation.width
    ) or owner.width
    ready_height = (
        projection.height if projection is not None else generation.height
    ) or owner.height
    return ImagePyramidResponse(
        task_id=task.id,
        status="ready",
        required=(
            projection.required
            if projection is not None
            else pyramid_required(ready_width, ready_height)
        ),
        source_snapshot=(
            projection.source_snapshot
            if projection is not None
            else generation.source_snapshot
        ),
        profile_version=(
            projection.profile_version
            if projection is not None
            else asset.profile_version
        ),
        width=ready_width,
        height=ready_height,
        generation=(
            projection.generation if projection is not None else generation.generation
        ),
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
@_errors
async def create_image_pyramid_asset_urls(
    task_id: uuid.UUID,
    payload: ImagePyramidAssetUrlsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    if task.file_type != "image":
        raise HTTPException(409, {"reason": "asset_not_ready"})
    authority = await originals.authorize(db, task.id, current_user, request)
    try:
        owner, asset, generation = await get_active_generation_for_task(db, task)
    except ImagePyramidError as exc:
        raise HTTPException(409, detail={"reason": exc.code}) from None
    if asset is None or generation is None:
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="asset_urls", outcome="not_ready"
        ).inc()
        raise HTTPException(status_code=409, detail={"reason": "asset_not_ready"})
    if native_generation(generation):
        projection = await _owner_projection(db, task, generation, current_user.id)
        if isinstance(projection, str):
            if projection == "stale":
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "stale_generation", "active_generation": None},
                )
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="asset_urls", outcome=projection
            ).inc()
            raise HTTPException(status_code=409, detail={"reason": "asset_not_ready"})
        if projection.native is not True or projection.native_output is None:
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="asset_urls", outcome="unavailable"
            ).inc()
            raise HTTPException(status_code=409, detail={"reason": "asset_not_ready"})
        if projection.status != "ready":
            if projection.status == "stale":
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "stale_generation", "active_generation": None},
                )
            IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
                operation="asset_urls", outcome=projection.status
            ).inc()
            raise HTTPException(status_code=409, detail={"reason": "asset_not_ready"})
        return await _native_asset_url_response(
            db,
            task,
            generation,
            projection,
            payload,
            authority=authority,
            actor_id=current_user.id,
        )

    if generation.status != "ready":
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="asset_urls", outcome="not_ready"
        ).inc()
        raise HTTPException(status_code=409, detail={"reason": "asset_not_ready"})
    if not source_matches_generation(owner, generation):
        await originals.check(db, authority, lock=True)
        await _invalidate_ready(db, asset, generation, "source_changed")
        raise HTTPException(
            status_code=409,
            detail={"reason": "stale_generation", "active_generation": None},
        )
    if generation.output_id is None:
        IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(
            operation="asset_urls", outcome="unavailable"
        ).inc()
        raise HTTPException(status_code=409, detail={"reason": "asset_not_ready"})

    await db.commit()
    try:
        keys = _requested_keys(generation, payload)
        async with objects.reading(
            db, owner, generation, authority=authority, keys=keys
        ) as output_owner:
            async with objects.transport(db, output_owner) as storage:
                manifest = await objects.manifest(storage, output_owner, generation)
                return await _asset_urls(
                    db,
                    task,
                    asset,
                    generation,
                    manifest,
                    payload,
                    output_owner,
                    storage=storage,
                )
    except (ContentError, physical.ImagePyramidStorageError) as exc:
        if exc.code not in {
            "image_pyramid_storage_missing",
            "image_pyramid_storage_integrity_mismatch",
            "image_pyramid_storage_invalid_receipt",
        }:
            raise
        await originals.check(db, authority, lock=True)
        await _invalidate_ready(db, asset, generation, exc.code)
        raise HTTPException(409, {"reason": "inconsistent_ready"}) from None


def _requested_keys(generation, payload):
    if (
        type(generation.width) is not int
        or type(generation.height) is not int
        or generation.width <= 0
        or generation.height <= 0
        or not isinstance(generation.manifest_key, str)
        or not generation.manifest_key.endswith("/manifest.json")
    ):
        raise physical.ImagePyramidStorageError("invalid_receipt")
    levels = {
        level["level"]: level
        for level in pyramid_levels(generation.width, generation.height)
    }
    keys = [generation.manifest_key]
    for item in payload.items:
        if item.generation != generation.generation:
            raise HTTPException(
                409,
                {
                    "reason": "stale_generation",
                    "active_generation": generation.generation,
                },
            )
        if item.kind == "overview":
            if (
                not isinstance(generation.overview_key, str)
                or not generation.overview_key
            ):
                raise physical.ImagePyramidStorageError("invalid_receipt")
            keys.append(generation.overview_key)
        else:
            level = levels.get(item.level)
            if level is None or item.x >= level["columns"] or item.y >= level["rows"]:
                raise HTTPException(422, {"reason": "invalid_coordinate"})
            suffix = f"tiles/{item.level}/{item.x}/{item.y}.webp"
            keys.append(generation.manifest_key.removesuffix("manifest.json") + suffix)
    return tuple(dict.fromkeys(keys))


async def _asset_urls(
    db,
    task,
    asset,
    generation,
    manifest,
    payload,
    output_owner,
    *,
    storage=storage_service,
):
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

    url_expiry_seconds = objects.url_expiry_seconds(generation)
    expiry = utcnow() + timedelta(seconds=url_expiry_seconds)
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
            key = physical.resolve(output_owner).key(
                f"tiles/{item.level}/{item.x}/{item.y}.webp"
            )
            output_item = {
                "kind": "tile",
                "generation": generation.generation,
                "level": item.level,
                "x": item.x,
                "y": item.y,
            }
        await originals.physical(physical.verify_object, storage, output_owner, key)
        url = physical.signed_url(storage, output_owner, key, url_expiry_seconds)
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
@_errors
async def retry_image_pyramid(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if task.file_type != "image":
        raise HTTPException(409, {"reason": "not_image"})
    authority = await originals.authorize(db, task.id, current_user, request)
    try:
        owner, asset, generation = await get_active_generation_for_task(db, task)
    except ImagePyramidError as exc:
        raise HTTPException(409, detail={"reason": exc.code}) from None
    if owner.file_type != "image":
        raise HTTPException(status_code=409, detail={"reason": "not_image"})
    if (
        owner.width is not None
        and owner.height is not None
        and not pyramid_eligible(owner.width, owner.height)
    ):
        raise HTTPException(status_code=409, detail={"reason": "not_eligible"})

    if generation is None and asset is not None:
        generation = await _latest_generation(db, asset)
    invalidated_stale = False
    if native_generation(generation):
        projection = await _owner_projection(db, task, generation, current_user.id)
        if (
            not isinstance(projection, str)
            and projection.status == "ready"
            and generation.output_id is not None
        ):
            raise HTTPException(status_code=409, detail={"reason": "already_ready"})
    elif generation is not None and generation.status == "ready":
        if (
            source_matches_generation(owner, generation)
            and generation.output_id is not None
        ):
            raise HTTPException(status_code=409, detail={"reason": "already_ready"})
        elif generation.output_id is None:
            invalidated_stale = True
        elif asset is not None:
            await _invalidate_ready(db, asset, generation, "source_changed")
            invalidated_stale = True

    if asset is not None and not native_generation(generation):
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

    await originals.check(db, authority, lock=True)
    await db.commit()
    try:
        accepted = await _admit_native_pyramid(db, task, authority, retry=True)
    except ImageFailure as exc:
        raise HTTPException(http_error_status(exc.code), {"reason": exc.code}) from None
    IMAGE_PYRAMID_API_REQUESTS_TOTAL.labels(operation="retry", outcome="queued").inc()
    return ImagePyramidRetryResponse(
        task_id=task.id,
        status="building" if accepted.work.status == "running" else "queued",
        job_id=accepted.work.job_id,
    )
