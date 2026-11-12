"""Resolve verified video source and poster URLs through Assets."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from yoao_assets_media.contract import ReadSourceRequest
from yoao_assets_media.transport import SourceReference
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assets_media import browser_media_url, media_gateway

_BATCH_SIZE = 4


async def video_source_references(
    db: AsyncSession, requests: Sequence[ReadSourceRequest]
) -> list[SourceReference | None]:
    """Return strict owner references in request order, isolating item failures."""
    resolved: list[SourceReference | None] = [None] * len(requests)
    if not requests:
        return resolved

    try:
        async with media_gateway(db) as gateway:
            for start in range(0, len(requests), _BATCH_SIZE):
                batch = requests[start : start + _BATCH_SIZE]
                batch_references = await asyncio.gather(
                    *(gateway.source_reference(request) for request in batch),
                    return_exceptions=True,
                )
                failures = [
                    value
                    for value in batch_references
                    if isinstance(value, BaseException)
                ]
                if any(
                    getattr(value, "storage_operation_unsettled", False)
                    for value in failures
                ):
                    if len(failures) == 1:
                        raise failures[0]
                    combined = BaseExceptionGroup(
                        "Video source references failed", failures
                    )
                    combined.storage_operation_unsettled = True
                    raise combined
                for index, (request, reference) in enumerate(
                    zip(batch, batch_references, strict=True), start=start
                ):
                    if isinstance(reference, BaseException):
                        if not isinstance(reference, Exception):
                            raise reference
                        continue
                    source = reference.source
                    if (
                        source.dataset_item_id != request.dataset_item_id
                        or source.file_type != "video"
                        or source.source_drifted
                    ):
                        continue
                    materialized = (
                        source.materialized_generation == source.source_generation
                    )
                    resolved[index] = reference.model_copy(
                        update={
                            "original_url": (
                                browser_media_url(reference.original_url)
                                if reference.original_url is not None
                                else None
                            ),
                            "video_url": (
                                browser_media_url(reference.video_url)
                                if materialized and reference.video_url
                                else browser_media_url(reference.original_url)
                                if not materialized and reference.original_url
                                else None
                            ),
                            "poster_url": (
                                browser_media_url(reference.poster_url)
                                if materialized and reference.poster_url
                                else None
                            ),
                        }
                    )
    except Exception as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        return resolved
    return resolved


async def video_poster_urls(
    db: AsyncSession, requests: Sequence[ReadSourceRequest]
) -> list[str | None]:
    """Return only current verified posters, preserving the caller's batch order."""
    references = await video_source_references(db, requests)
    return [
        reference.poster_url if reference is not None else None
        for reference in references
    ]
