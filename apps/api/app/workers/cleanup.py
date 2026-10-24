"""v0.7.0 · celery beat 后台清理任务

Comment attachments use saved owners, fixed expiry and a seven-day deletion
window. Unknown historical attachment metadata is retained for reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, text, update

from app.db.models.annotation_conversion_plan import AnnotationConversionPlan
from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
from app.db.models.mask_annotation_revision import MaskAnnotationRevision
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.observability.raster_mask import (
    refresh_raster_mask_active_geometries_safely,
)
from app.services.raster_mask_references import (
    _MASK_REFERENCE_QUERIES as _MASK_REFERENCE_QUERIES,
    _MASK_REFERENCE_EXISTS_QUERIES as _MASK_REFERENCE_EXISTS_QUERIES,
    referenced_keys,
    is_referenced,
)
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

VIDEO_TRACKER_STAGED_TTL = timedelta(hours=24)
_referenced_raster_mask_keys = referenced_keys
_is_raster_mask_key_referenced = is_referenced


async def _expire_mask_annotation_revisions(
    db,
    *,
    now: datetime | None = None,
) -> int:
    """Delete snapshots after their retention window has actually elapsed."""
    result = await db.execute(
        delete(MaskAnnotationRevision)
        .where(MaskAnnotationRevision.expires_at <= (now or datetime.now(timezone.utc)))
        .returning(MaskAnnotationRevision.id)
    )
    return len(result.scalars().all())


async def _expire_annotation_conversion_plans(
    db,
    *,
    now: datetime | None = None,
) -> int:
    result = await db.execute(
        delete(AnnotationConversionPlan)
        .where(
            AnnotationConversionPlan.expires_at <= (now or datetime.now(timezone.utc))
        )
        .returning(AnnotationConversionPlan.id)
    )
    return len(result.scalars().all())


@celery_app.task(name="app.workers.cleanup.purge_soft_deleted_attachments")
def purge_soft_deleted_attachments() -> dict:
    """Retry up to 500 known expired or retired comment attachments."""
    return asyncio.run(_purge_async())


async def _purge_async() -> dict:
    """Retry only known attachment owners; never erase unknown legacy metadata."""
    from app.services import comment_attachment_uploads

    async with task_session() as db:
        return await comment_attachment_uploads.cleanup(db, limit=500)


@celery_app.task(name="app.workers.cleanup.purge_guide_assets")
def purge_guide_assets() -> dict:
    """Retire or retry saved guide-image owners without scanning legacy JSON."""
    return asyncio.run(_purge_guide_assets_async())


async def _purge_guide_assets_async() -> dict:
    from app.services import guide_asset_uploads

    async with task_session() as db:
        return await guide_asset_uploads.cleanup(db, limit=500)


async def _expire_stale_video_tracker_candidates(
    db,
    *,
    now: datetime | None = None,
) -> int:
    """Release abandoned staged Mask refs and correction track leases after 24h."""
    cutoff = (now or datetime.now(timezone.utc)) - VIDEO_TRACKER_STAGED_TTL
    expirable = (
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
        VideoTrackerJobStatus.CANCELLED.value,
    )
    result = await db.execute(
        update(VideoTrackerJob)
        .where(
            VideoTrackerJob.staged_result.is_not(None),
            VideoTrackerJob.status.in_(expirable),
            VideoTrackerJob.completed_at.is_not(None),
            VideoTrackerJob.completed_at <= cutoff,
        )
        .values(
            staged_result=None,
            status=case(
                (
                    VideoTrackerJob.status.in_(
                        (
                            VideoTrackerJobStatus.PENDING_REVIEW.value,
                            VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
                        )
                    ),
                    VideoTrackerJobStatus.DISCARDED.value,
                ),
                else_=VideoTrackerJob.status,
            ),
            revision=VideoTrackerJob.revision + 1,
        )
        .returning(VideoTrackerJob.id)
    )
    return len(result.scalars().all())


@celery_app.task(name="app.workers.cleanup.purge_unreferenced_raster_masks")
def purge_unreferenced_raster_masks(dry_run: bool = False) -> dict:
    """Retire and collect known RLE owners after their protected publication window."""
    return asyncio.run(_purge_unreferenced_raster_masks_async(dry_run=dry_run))


async def _purge_unreferenced_raster_masks_async(*, dry_run: bool = False) -> dict:
    from app.services import raster_mask_objects

    async with task_session() as db:
        expired_tracker = expired_conversion = expired_revisions = 0
        if not dry_run:
            await db.execute(
                delete(AiMaskAcceptDecision).where(
                    AiMaskAcceptDecision.expires_at <= datetime.now(timezone.utc)
                )
            )
            expired_tracker = await _expire_stale_video_tracker_candidates(db)
            expired_conversion = await _expire_annotation_conversion_plans(db)
            expired_revisions = await _expire_mask_annotation_revisions(db)
            await db.commit()
            await refresh_raster_mask_active_geometries_safely(db)
        result = await raster_mask_objects.cleanup(db, dry_run=dry_run, limit=1000)
        result.update(
            expired_tracker_candidates=expired_tracker,
            expired_conversion_plans=expired_conversion,
            expired_mask_revisions=expired_revisions,
        )
    log.info("purge_unreferenced_raster_masks done: %s", result)
    return result


@celery_app.task(name="app.workers.cleanup.refresh_user_perf_mv")
def refresh_user_perf_mv() -> dict:
    """v0.8.4 · celery beat 每小时第 5 分钟触发：REFRESH MATERIALIZED VIEW CONCURRENTLY mv_user_perf_daily。

    CONCURRENTLY 要求视图上有 UNIQUE 索引；不阻塞读端。首次刷新需要 NON-CONCURRENTLY，
    迁移内已 REFRESH 一次填初始数据，所以 beat 这里直接 CONCURRENTLY 即可。
    """
    return asyncio.run(_refresh_mv_async())


async def _refresh_mv_async() -> dict:

    async with task_session() as db:
        try:
            await db.execute(
                text("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_user_perf_daily")
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            log.warning("refresh_user_perf_mv failed: %s", exc)
            return {"refreshed": False, "error": str(exc)}
    log.info("refresh_user_perf_mv done")
    return {"refreshed": True}


@celery_app.task(name="app.workers.cleanup.refresh_audit_bi_mv")
def refresh_audit_bi_mv() -> dict:
    """Refresh completed UTC audit days without blocking BI readers."""
    return asyncio.run(_refresh_audit_bi_mv_async())


async def _refresh_audit_bi_mv_async() -> dict:

    async with task_session() as db:
        try:
            await db.execute(
                text("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_audit_bi_daily")
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            error_type = type(exc).__name__
            log.warning("refresh_audit_bi_mv failed; error_type=%s", error_type)
            return {"refreshed": False, "error_type": error_type}
    log.info("refresh_audit_bi_mv done")
    return {"refreshed": True}
