"""Celery cleanup adapters for attachment and raster-mask retention.

Comment attachments use saved owners, fixed expiry and a seven-day deletion
window. Unknown historical attachment metadata is retained for reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from app.observability.raster_mask import (
    refresh_raster_mask_active_geometries_safely,
)
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


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


@celery_app.task(name="app.workers.cleanup.purge_unreferenced_raster_masks")
def purge_unreferenced_raster_masks(dry_run: bool = False) -> dict:
    """Retire and collect known RLE owners after their protected publication window."""
    return asyncio.run(_purge_unreferenced_raster_masks_async(dry_run=dry_run))


async def _purge_unreferenced_raster_masks_async(*, dry_run: bool = False) -> dict:
    from app.services import raster_mask_objects
    from app.services.ai_mask_accept import expire_accept_decisions
    from app.services.annotation_conversion import expire_conversion_plans
    from app.services.mask_slice_restore import expire_mask_annotation_revisions
    from app.services.video_tracking.jobs import expire_stale_tracker_candidates

    async with task_session() as db:
        expired_tracker = expired_conversion = expired_revisions = 0
        if not dry_run:
            await expire_accept_decisions(db)
            expired_tracker = await expire_stale_tracker_candidates(db)
            expired_conversion = await expire_conversion_plans(db)
            expired_revisions = await expire_mask_annotation_revisions(db)
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
    """Refresh the Insights daily aggregate concurrently on the maintenance queue."""
    return asyncio.run(_refresh_mv_async())


async def _refresh_mv_async() -> dict:
    try:
        url = None
        direct = settings.insights_refresh_database_url
        path = settings.insights_refresh_database_url_file
        if direct and path:
            raise ValueError("Select only one Insights refresh database credential")
        if direct or path:
            url = make_url(
                Path(path).read_text(encoding="utf-8").strip() if path else direct
            )
            if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
                raise ValueError("Insights refresh requires PostgreSQL")
            url = url.set(drivername="postgresql+asyncpg")
        async with task_session(database_url=url) as db, db.begin():
            await db.execute(text("SELECT yoao_insights_v1.refresh_user_perf_daily()"))
    except Exception as exc:
        error_type = type(exc).__name__
        log.warning("refresh_user_perf_mv failed: %s", error_type)
        return {"refreshed": False, "error": error_type}
    log.info("refresh_user_perf_mv done")
    return {"refreshed": True}


@celery_app.task(name="app.workers.cleanup.refresh_audit_bi_mv")
def refresh_audit_bi_mv() -> dict:
    """Refresh completed UTC audit days through Audit's owner capability."""
    return asyncio.run(_refresh_audit_bi_mv_async())


async def _refresh_audit_bi_mv_async() -> dict:
    from yoao_audit.maintenance_postgres import platform_maintenance

    async with task_session() as db:
        try:
            await platform_maintenance(db, "refresh_daily")
            await db.commit()
        except Exception as exc:
            await db.rollback()
            error_type = type(exc).__name__
            log.warning("refresh_audit_bi_mv failed; error_type=%s", error_type)
            return {"refreshed": False, "error_type": error_type}
    log.info("refresh_audit_bi_mv done")
    return {"refreshed": True}
