"""Resolve current video poster URLs through the Assets media owner."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from aap_assets_media.contract import ReadSourceRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assets_media import browser_media_url, media_gateway

_BATCH_SIZE = 4


async def video_poster_urls(
    db: AsyncSession, requests: Sequence[ReadSourceRequest]
) -> list[str | None]:
    """Return owner-verified posters in request order; failures omit only that URL."""
    urls: list[str | None] = [None] * len(requests)
    if not requests:
        return urls

    try:
        async with media_gateway(db) as gateway:
            for start in range(0, len(requests), _BATCH_SIZE):
                batch = requests[start : start + _BATCH_SIZE]
                references = await asyncio.gather(
                    *(gateway.source_reference(request) for request in batch),
                    return_exceptions=True,
                )
                for index, (request, reference) in enumerate(
                    zip(batch, references, strict=True), start=start
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
                        or source.materialized_generation != source.source_generation
                        or not reference.poster_url
                    ):
                        continue
                    urls[index] = browser_media_url(reference.poster_url)
    except Exception:
        return urls
    return urls
