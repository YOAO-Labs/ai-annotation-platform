"""Resolve current image references without exposing stale thumbnail pointers."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_images.contract import (
    BATCH_SIZE,
    ImageFailure,
    ReadImageSourceRequest,
    http_error_status,
)
from aap_assets_images.transport import (
    ImageSourceReference,
    ImageSourceReferencesRequest,
    require_source_reference_scope,
)
from app.services.assets_images import image_gateway


async def image_source_references(
    db: AsyncSession, requests: Sequence[ReadImageSourceRequest]
) -> list[ImageSourceReference | None]:
    resolved: list[ImageSourceReference | None] = []
    if not requests:
        return resolved
    async with image_gateway(db) as owner:

        async def read_batch(batch):
            try:
                result = await owner.source_references(
                    ImageSourceReferencesRequest(requests=list(batch))
                )
            except ImageFailure as exc:
                leaf_failure = exc.code in {
                    "source_missing",
                    "source_changed",
                    "source_not_ready",
                    "task_not_visible",
                    "permission_denied",
                    "not_image",
                    "owner_changed",
                    "asset_missing",
                }
                if leaf_failure and len(batch) == 1:
                    return [None]
                if not leaf_failure and not (
                    exc.code == "resource_budget_exceeded" and len(batch) > 1
                ):
                    status = http_error_status(exc.code)
                    raise HTTPException(
                        status_code=status,
                        detail={"reason": exc.code},
                        headers={"Retry-After": "3"} if status in {409, 503} else None,
                    ) from exc
                # Isolate a bad source without turning a healthy page into N RPCs.
                middle = len(batch) // 2
                return await read_batch(batch[:middle]) + await read_batch(
                    batch[middle:]
                )
            if len(result.references) != len(batch):
                raise HTTPException(
                    status_code=502, detail={"reason": "invalid_response"}
                )
            found = []
            for request, reference in zip(batch, result.references, strict=True):
                try:
                    require_source_reference_scope(request, reference)
                except ValueError:
                    found.append(None)
                else:
                    found.append(reference)
            return found

        for offset in range(0, len(requests), BATCH_SIZE):
            resolved.extend(await read_batch(requests[offset : offset + BATCH_SIZE]))
    return resolved
