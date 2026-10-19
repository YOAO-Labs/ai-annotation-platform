"""Select the durable Labeling collector and its independently owned byte port."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from uuid import uuid4

from aap_assets_mask_content import gc_contract as byte
from aap_assets_mask_content.gc_client import MaskGcClient
from aap_assets_mask_content.gc_storage import S3MaskGcStore
from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc.client import (
    LabelingMaskGcClient,
    LabelingMaskGcClientFailure,
)
from aap_labeling_mask_gc.coordinator import run_cycle

from app.config import settings
from app.services.mask_gc import IndependentMaskGcDatabase
from app.services.raster_mask_contract import RasterMaskContractError
from app.services.storage import storage_service


@contextmanager
def mask_gc_storage_owner(storage):
    """Remote byte configuration is authoritative, including missing cleanup data."""
    if not (settings.assets_mask_content_url or settings.assets_mask_cleanup_url):
        yield S3MaskGcStore(storage.client, storage.bucket)
        return
    with MaskGcClient(
        settings.assets_mask_cleanup_url,
        settings.assets_mask_cleanup_token,
        timeout_seconds=settings.assets_mask_content_timeout_seconds,
    ) as owner:
        yield owner


def _remote_cycle(request: c.RunRequest) -> c.CycleStats:
    with LabelingMaskGcClient(
        settings.labeling_mask_gc_url,
        settings.labeling_mask_gc_token,
        timeout_seconds=settings.labeling_mask_gc_timeout_seconds,
    ) as owner:
        return owner.run(request)


async def collect_raster_masks(session_factory, request: c.RunRequest) -> c.CycleStats:
    """Run the same coordinator locally or through its explicit HTTP owner.

    A remote timeout keeps the remote journal and native deletion fences intact.
    Cancellation waits for the in-flight call before closing its transport.
    """
    if type(request) is not c.RunRequest:
        raise c.MaskGcFailure("invalid_request")
    request = c.RunRequest.model_validate(request.model_dump(mode="python"))
    try:
        if settings.labeling_mask_gc_url or settings.labeling_mask_gc_token:
            task = asyncio.create_task(asyncio.to_thread(_remote_cycle, request))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except Exception:
                    pass
                raise
        with mask_gc_storage_owner(storage_service) as storage:
            return await run_cycle(
                IndependentMaskGcDatabase(session_factory),
                storage,
                worker_id=uuid4(),
                **request.model_dump(mode="python"),
            )
    except (c.MaskGcFailure, byte.MaskGcFailure, LabelingMaskGcClientFailure):
        raise RasterMaskContractError(
            status_code=503,
            reason="mask_gc_unavailable",
            message="Mask collection authority is unavailable",
            retryable=True,
        ) from None
