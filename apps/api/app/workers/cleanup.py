"""v0.7.0 · celery beat 后台清理任务

Comment attachments use saved owners, fixed expiry and a seven-day deletion
window. Unknown historical attachment metadata is retained for reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, select, text, update

from app.db.models.annotation_conversion_plan import AnnotationConversionPlan
from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
from app.db.models.mask_annotation_revision import MaskAnnotationRevision
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.observability.raster_mask import (
    refresh_raster_mask_active_geometries_safely,
)
from app.services.raster_mask_storage import lock_raster_mask_references
from app.services.storage import storage_service
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

VIDEO_TRACKER_STAGED_TTL = timedelta(hours=24)


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


_MASK_REFERENCE_QUERIES = (
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM training_dataset_versions,
         LATERAL jsonb_path_query(snapshot, '$.**.object_key') value
    WHERE status <> 'ready'
      AND snapshot->>'bridge' = 'ultralytics-instance-rle-1'
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM trained_model_deployments,
         LATERAL jsonb_path_query(trial, '$.**.object_key') value
    WHERE value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM annotations, LATERAL jsonb_path_query(geometry, '$.**.object_key') value
    WHERE is_active IS TRUE AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM predictions, LATERAL jsonb_path_query(result, '$.**.object_key') value
    WHERE value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM video_tracker_jobs, LATERAL jsonb_path_query(staged_result, '$.**.object_key') value
    WHERE staged_result IS NOT NULL
      AND status IN ('pending_review', 'partially_reviewed', 'cancelled')
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM ai_mask_accept_decisions,
         LATERAL jsonb_path_query(response_json, '$.**.object_key') value
    WHERE expires_at > now()
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM mask_annotation_revisions,
         LATERAL jsonb_path_query(geometry, '$.**.object_key') value
    WHERE expires_at > now()
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM mask_qc_runs,
         LATERAL jsonb_path_query(source_snapshot, '$.**.object_key') value
    WHERE status IN ('pending', 'running')
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT region_mask_ref->>'object_key' AS object_key
    FROM mask_qc_issues
    WHERE region_mask_ref->>'object_key' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM mask_repair_batches,
         LATERAL jsonb_path_query(plan_json, '$.**.object_key') value
    WHERE (
        (status = 'planned' AND receipt_expires_at > now())
        OR status IN ('pending', 'running', 'rolling_back')
        OR (
            status IN ('completed', 'partial', 'rollback_failed')
            AND rollback_expires_at > now()
        )
      )
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
)

_MASK_REFERENCE_EXISTS_QUERIES = (
    """
    SELECT EXISTS (
        SELECT 1 FROM training_dataset_versions,
             LATERAL jsonb_path_query(snapshot, '$.**.object_key') value
        WHERE status <> 'ready'
          AND snapshot->>'bridge' = 'ultralytics-instance-rle-1'
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1 FROM trained_model_deployments,
             LATERAL jsonb_path_query(trial, '$.**.object_key') value
        WHERE value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM annotations, LATERAL jsonb_path_query(geometry, '$.**.object_key') value
        WHERE is_active IS TRUE AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM predictions, LATERAL jsonb_path_query(result, '$.**.object_key') value
        WHERE value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM video_tracker_jobs,
             LATERAL jsonb_path_query(staged_result, '$.**.object_key') value
        WHERE staged_result IS NOT NULL
          AND status IN ('pending_review', 'partially_reviewed', 'cancelled')
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM ai_mask_accept_decisions,
             LATERAL jsonb_path_query(response_json, '$.**.object_key') value
        WHERE expires_at > now()
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_annotation_revisions,
             LATERAL jsonb_path_query(geometry, '$.**.object_key') value
        WHERE expires_at > now()
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_qc_runs,
             LATERAL jsonb_path_query(source_snapshot, '$.**.object_key') value
        WHERE status IN ('pending', 'running')
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_qc_issues
        WHERE region_mask_ref->>'object_key' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_repair_batches,
             LATERAL jsonb_path_query(plan_json, '$.**.object_key') value
        WHERE (
            (status = 'planned' AND receipt_expires_at > now())
            OR status IN ('pending', 'running', 'rolling_back')
            OR (
                status IN ('completed', 'partial', 'rollback_failed')
                AND rollback_expires_at > now()
            )
          )
          AND value #>> '{}' = :key
    )
    """,
)


async def _referenced_raster_mask_keys(db) -> set[str]:
    referenced: set[str] = set()
    for query in _MASK_REFERENCE_QUERIES:
        rows = (await db.execute(text(query))).scalars().all()
        referenced.update(str(key) for key in rows if key)
    return referenced


async def _is_raster_mask_key_referenced(db, key: str) -> bool:
    for query in _MASK_REFERENCE_EXISTS_QUERIES:
        if bool((await db.execute(text(query), {"key": key})).scalar()):
            return True
    return False


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


def _eligible_raster_mask_objects(
    candidates: list[dict], referenced: set[str], cutoff: datetime
) -> list[dict]:
    return [
        item
        for item in candidates
        if item["key"] not in referenced
        and item.get("last_modified") is not None
        and item["last_modified"] < cutoff
    ][:1000]


@celery_app.task(name="app.workers.cleanup.purge_unreferenced_raster_masks")
def purge_unreferenced_raster_masks(dry_run: bool = False) -> dict:
    """Delete unreferenced content-addressed mask objects after a 24-hour grace."""
    return asyncio.run(_purge_unreferenced_raster_masks_async(dry_run=dry_run))


async def _purge_unreferenced_raster_masks_async(*, dry_run: bool = False) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with task_session() as db:
        # Native Mask accept 幂等快照只保留 24 小时；过期后重放稳定拒绝，
        # 同时不再阻止其中 RLE 引用进入宽限 GC。在扫描引用前删除过期大快照。
        await db.execute(
            delete(AiMaskAcceptDecision).where(
                AiMaskAcceptDecision.expires_at <= datetime.now(timezone.utc)
            )
        )
        expired_tracker_candidates = await _expire_stale_video_tracker_candidates(db)
        expired_conversion_plans = await _expire_annotation_conversion_plans(db)
        expired_mask_revisions = await _expire_mask_annotation_revisions(db)
        await db.commit()
        await refresh_raster_mask_active_geometries_safely(db)
        referenced = await _referenced_raster_mask_keys(db)
        candidates = await asyncio.to_thread(
            storage_service.list_objects, "raster-masks/sha256/"
        )
        deletable = _eligible_raster_mask_objects(candidates, referenced, cutoff)
        deleted = 0
        errors = 0
        if not dry_run:
            for item in deletable:
                key = item["key"]
                try:
                    await lock_raster_mask_references(
                        db,
                        {"object_key": key},
                        verify=False,
                    )
                    if not await _is_raster_mask_key_referenced(db, key):
                        await db.execute(
                            delete(RasterMaskUpload).where(
                                RasterMaskUpload.object_key == key
                            )
                        )
                        # Commit the recoverable database state first.  If the
                        # process stops before object deletion, the next GC pass
                        # sees a harmless storage orphan; the inverse order could
                        # leave a committed upload row pointing at missing bytes.
                        await db.commit()
                        # A same-key upload can start immediately after that
                        # commit. Reacquire the content lock in a second
                        # transaction and recheck both references and upload
                        # reservations before deleting while the lock is held.
                        await lock_raster_mask_references(
                            db,
                            {"object_key": key},
                            verify=False,
                        )
                        upload_exists = (
                            await db.execute(
                                select(RasterMaskUpload.id)
                                .where(RasterMaskUpload.object_key == key)
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        if (
                            upload_exists is None
                            and not await _is_raster_mask_key_referenced(db, key)
                        ):
                            await asyncio.to_thread(storage_service.delete_object, key)
                            deleted += 1
                        await db.commit()
                        continue
                    await db.commit()
                except Exception as exc:  # noqa: BLE001 - conservative GC retains on failure
                    await db.rollback()
                    errors += 1
                    log.warning(
                        "delete unreferenced raster mask failed; error_type=%s",
                        type(exc).__name__,
                    )
    result = {
        "dry_run": dry_run,
        "referenced": len(referenced),
        "scanned": len(candidates),
        "eligible": len(deletable),
        "deleted": deleted,
        "errors": errors,
        "expired_tracker_candidates": expired_tracker_candidates,
        "expired_conversion_plans": expired_conversion_plans,
        "expired_mask_revisions": expired_mask_revisions,
    }
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
    from sqlalchemy import text

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
    from sqlalchemy import text

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
