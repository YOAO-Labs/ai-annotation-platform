"""Project independent image-owner values into the existing public REST models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_images import contract as c
from aap_assets_images.geometry import canonical_manifest_bytes
from aap_assets_images.transport import (
    ImageAssetReferencesRequest,
    ImageGenerationReferenceRequest,
)
from app.observability.metrics import IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL
from app.schemas.image_pyramid import (
    ImagePyramidAssetUrl,
    ImagePyramidAssetUrlsRequest,
    ImagePyramidAssetUrlsResponse,
    ImagePyramidManifest,
    ImagePyramidOverviewUrl,
    ImagePyramidResponse,
    ImagePyramidSummary,
)
from app.services.assets_images import image_gateway, image_policy
from app.services.assets_media import browser_media_url
from app.services.image_pyramid_contract import (
    ImagePyramidApiPort,
    ImagePyramidFailure,
    ImagePyramidManifestResult,
    ImagePyramidRetryPlan,
    ImagePyramidTaskSource,
)


def _failure(exc: c.ImageFailure) -> ImagePyramidFailure:
    status = c.http_error_status(exc.code)
    return ImagePyramidFailure(
        status,
        {"reason": exc.code},
        headers={"Retry-After": "3"} if status in {409, 503} else None,
        metric_outcome=exc.code,
    )


def _etag(reference) -> str:
    manifest = reference.manifest.model_dump(mode="json", by_alias=True)
    digest = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    pyramid = reference.pyramid
    state = json.dumps(
        {
            "source_snapshot": reference.source.source_snapshot,
            "profile_version": pyramid.profile_version,
            "building_generation": pyramid.building_generation,
            "building_status": pyramid.building_status,
            "error_code": pyramid.error_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    suffix = "-" + hashlib.sha256(state).hexdigest()[:16]
    return f'"pyramid-{pyramid.generation}-{digest}{suffix}"'


class AssetsImagePyramidApi(ImagePyramidApiPort):
    def __init__(self, db: AsyncSession, *, actor_id: UUID):
        self.db = db
        self.actor_id = actor_id

    async def task_summaries(
        self,
        tasks: tuple[ImagePyramidTaskSource, ...],
    ) -> dict[UUID, ImagePyramidSummary]:
        selected = [task for task in tasks if task.file_type == "image"]
        found = {}
        if not selected:
            return found
        try:
            async with image_gateway(self.db) as owner:
                for offset in range(0, len(selected), c.BATCH_SIZE):
                    batch = selected[offset : offset + c.BATCH_SIZE]
                    result = await owner.call(
                        "read_image_summaries",
                        c.ReadImageSummariesRequest(
                            actor_id=self.actor_id,
                            selectors=[
                                c.ImageSourceSelector(task_id=task.task_id)
                                for task in batch
                            ],
                            profile_version=image_policy().profile_version,
                        ),
                    )
                    for item in result.items:
                        if item.selector.task_id in found:
                            raise c.ImageFailure("invalid_response")
                        found[item.selector.task_id] = (
                            ImagePyramidSummary.model_validate(
                                {
                                    **item.pyramid.model_dump(mode="json"),
                                    "source_snapshot": item.source.source_snapshot,
                                }
                            )
                        )
        except c.ImageFailure as exc:
            raise _failure(exc) from None
        return found

    async def manifest(
        self,
        task: ImagePyramidTaskSource,
        *,
        if_none_match: str | None,
    ) -> ImagePyramidManifestResult:
        if task.file_type != "image":
            return ImagePyramidManifestResult(
                ImagePyramidResponse(
                    task_id=task.task_id, status="not_available", required=False
                ),
                None,
                "private, no-store",
                "not_available",
            )
        try:
            async with image_gateway(self.db) as owner:
                reference = await owner.generation_reference(
                    ImageGenerationReferenceRequest(
                        actor_id=self.actor_id,
                        task_id=task.task_id,
                        expires_in=image_policy().url_expiry_seconds,
                        profile_version=image_policy().profile_version,
                    )
                )
        except c.ImageFailure as exc:
            raise _failure(exc) from None
        pyramid = reference.pyramid
        etag = _etag(reference) if pyramid.status == "ready" else None
        cache = "private, max-age=60" if etag else "private, no-store"
        if etag is not None and if_none_match == etag:
            return ImagePyramidManifestResult(None, etag, cache, "not_modified")
        body = ImagePyramidResponse(
            task_id=task.task_id,
            status=pyramid.status,
            required=pyramid.required,
            source_snapshot=reference.source.source_snapshot,
            profile_version=pyramid.profile_version,
            width=pyramid.width,
            height=pyramid.height,
            retryable=pyramid.retryable,
            retry_after_ms=pyramid.retry_after_ms,
            generation=pyramid.generation,
            building_generation=pyramid.building_generation,
            building_status=pyramid.building_status,
            error_code=pyramid.error_code,
            manifest=ImagePyramidManifest.model_validate(
                reference.manifest.model_dump(mode="json", by_alias=True)
            )
            if reference.manifest
            else None,
            overview=ImagePyramidOverviewUrl(
                url=browser_media_url(reference.overview_url),
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=reference.expires_in),
            )
            if reference.overview_url
            else None,
        )
        return ImagePyramidManifestResult(body, etag, cache, pyramid.status)

    async def asset_urls(
        self,
        task: ImagePyramidTaskSource,
        payload: ImagePyramidAssetUrlsRequest,
    ) -> ImagePyramidAssetUrlsResponse:
        # The public endpoint historically deduplicates repeated coordinates.
        unique = list(
            {tuple(item.model_dump().items()): item for item in payload.items}.values()
        )
        try:
            request = ImageAssetReferencesRequest(
                actor_id=self.actor_id,
                task_id=task.task_id,
                items=[item.model_dump(mode="json") for item in unique],
                profile_version=image_policy().profile_version,
            )
        except ValueError:
            raise ImagePyramidFailure(422, {"reason": "invalid_coordinate"}) from None
        try:
            async with image_gateway(self.db) as owner:
                references = await owner.asset_references(request)
        except c.ImageFailure as exc:
            raise _failure(exc) from None
        output = [
            ImagePyramidAssetUrl(
                **item.model_dump(exclude={"url"}), url=browser_media_url(item.url)
            )
            for item in references.items
        ]
        for item in output:
            IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL.labels(kind=item.kind).inc()
        return ImagePyramidAssetUrlsResponse(
            task_id=task.task_id,
            generation=references.generation,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=references.expires_in),
            items=output,
        )

    async def retry(
        self,
        task: ImagePyramidTaskSource,
        *,
        operation_id: UUID,
    ) -> ImagePyramidRetryPlan:
        if task.file_type != "image":
            raise ImagePyramidFailure(409, {"reason": "not_image"})
        try:
            async with image_gateway(self.db) as owner:
                try:
                    accepted = await owner.call(
                        "retry_image_media",
                        c.RetryImageMediaRequest(
                            operation_id=operation_id,
                            actor_id=self.actor_id,
                            task_id=task.task_id,
                            purpose="pyramid",
                            policy=image_policy(),
                            force=True,
                        ),
                    )
                except c.ImageFailure as exc:
                    if exc.code != "retry_cooldown":
                        raise
                    state = await owner.call(
                        "read_image_generation",
                        c.ReadImageGenerationRequest(
                            actor_id=self.actor_id,
                            task_id=task.task_id,
                            profile_version=image_policy().profile_version,
                        ),
                    )
                    seconds = max(
                        1, ((state.pyramid.retry_after_ms or 1000) + 999) // 1000
                    )
                    raise ImagePyramidFailure(
                        429,
                        {"reason": "retry_cooldown", "retry_after_seconds": seconds},
                        headers={"Retry-After": str(seconds)},
                    ) from None
        except c.ImageFailure as exc:
            raise _failure(exc) from None
        return ImagePyramidRetryPlan(
            status="building" if accepted.work.status == "running" else "queued",
            owner_kind=accepted.source.owner.kind,
            owner_id=accepted.source.owner.id,
            job_id=accepted.work.job_id,
        )
