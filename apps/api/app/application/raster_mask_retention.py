from __future__ import annotations

import logging

from aap_labeling_mask_gc import contract as c

from sqlalchemy.ext.asyncio import AsyncSession
from aap_labeling.quality_data_contract import (
    CheckRasterMaskReference,
    QUALITY_DATA_PROTOCOL,
    ReadRasterMaskReferences,
)

from app.observability.raster_mask import (
    refresh_raster_mask_active_geometries_safely,
)
from app.services import ai_mask_accept, annotation_conversion
from app.services.mask_slice_restore import expire_mask_annotation_revisions
from app.services.mask_gc_collection import collect_raster_masks
from app.services.video_tracking.jobs import expire_stale_tracker_candidates
from app.services.labeling_quality_data import quality_data_command

log = logging.getLogger(__name__)


async def referenced_raster_mask_keys(db: AsyncSession) -> set[str]:
    """Return the complete live cross-domain root set for content-addressed masks."""
    rows = await quality_data_command(
        db,
        ReadRasterMaskReferences(
            schema=QUALITY_DATA_PROTOCOL,
            operation="read_all_references",
        ),
    )
    return {str(key) for key in rows if key} if isinstance(rows, list) else set()


async def is_raster_mask_key_referenced(db: AsyncSession, key: str) -> bool:
    value = await quality_data_command(
        db,
        CheckRasterMaskReference(
            schema=QUALITY_DATA_PROTOCOL,
            operation="check_reference",
            object_key=key,
        ),
    )
    return value is True


class RasterMaskRetentionWorkflow:
    """Compose domain retention commands with shared raster-mask storage GC."""

    def __init__(self, db: AsyncSession, *, session_factory) -> None:
        self.db = db
        self.session_factory = session_factory

    async def purge_unreferenced_raster_masks(
        self,
        *,
        dry_run: bool = False,
        scan_pages: int = 1,
        delete_limit: int = 100,
        after_key: str | None = None,
    ) -> dict:
        request = c.RunRequest(
            dry_run=dry_run,
            scan_pages=scan_pages,
            delete_limit=delete_limit,
            after_key=after_key,
        )

        await ai_mask_accept.expire_accept_decisions(self.db)
        expired_tracker_candidates = await expire_stale_tracker_candidates(self.db)
        expired_conversion_plans = await annotation_conversion.expire_conversion_plans(
            self.db
        )
        expired_mask_revisions = await expire_mask_annotation_revisions(self.db)
        await self.db.commit()
        await refresh_raster_mask_active_geometries_safely(self.db)
        # Metrics may open another read transaction. No caller connection or
        # business lock stays borrowed while the collector contacts either owner.
        await self.db.rollback()
        storage_result = await collect_raster_masks(self.session_factory, request)
        result = {
            **storage_result.model_dump(mode="json"),
            "errors": (
                storage_result.scan_errors
                + storage_result.finish_errors
                + storage_result.data_errors
                + storage_result.durable_uncertainties
            ),
            "expired_tracker_candidates": expired_tracker_candidates,
            "expired_conversion_plans": expired_conversion_plans,
            "expired_mask_revisions": expired_mask_revisions,
        }
        log.info("purge_unreferenced_raster_masks done: %s", result)
        return result
