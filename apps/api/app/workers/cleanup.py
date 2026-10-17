"""Celery cleanup adapters for attachment and raster-mask retention.

软删评论附件 7 天 grace 期后从 MinIO 删除。MinIO bucket lifecycle 已配
180 天硬兜底，本任务作为更精确的近期清理路径。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.application.raster_mask_retention import RasterMaskRetentionWorkflow
from app.db.models.annotation_comment import AnnotationComment
from app.services.storage import storage_service
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.cleanup.purge_soft_deleted_attachments")
def purge_soft_deleted_attachments() -> dict:
    """v0.7.0 · celery beat 每日 03:00 UTC 触发：扫 7 天前软删的评论附件并从 MinIO 删除。

    每次最多处理 500 条；硬删除完成后把 attachments 字段置为 [] 避免重复扫。
    """
    return asyncio.run(_purge_async())


async def _purge_async() -> dict:
    """实际的清理逻辑（async）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    deleted_objects = 0
    processed_comments = 0

    async with task_session() as db:
        result = await db.execute(
            select(AnnotationComment)
            .where(
                AnnotationComment.is_active.is_(False),
                AnnotationComment.updated_at < cutoff,
            )
            .limit(500)
        )
        comments = list(result.scalars().all())

        for comment in comments:
            attachments = comment.attachments or []
            if not attachments:
                continue
            for att in attachments:
                key = att.get("storage_key") if isinstance(att, dict) else None
                if not key:
                    continue
                try:
                    # v0.23.5 · WS-D · D5 · boto3 sync delete wrapped in to_thread
                    # so the async GC loop isn't blocked per-object.
                    await asyncio.to_thread(storage_service.delete_object, key)
                    deleted_objects += 1
                except Exception as e:
                    log.warning("delete object %s failed: %s", key, e)
            # 标记已清空，避免下次重复扫
            await db.execute(
                update(AnnotationComment)
                .where(AnnotationComment.id == comment.id)
                .values(attachments=[])
            )
            processed_comments += 1

        await db.commit()

    log.info(
        "purge_soft_deleted_attachments done: comments=%d objects=%d",
        processed_comments,
        deleted_objects,
    )
    return {"comments": processed_comments, "objects": deleted_objects}


@celery_app.task(name="app.workers.cleanup.purge_unreferenced_raster_masks")
def purge_unreferenced_raster_masks(dry_run: bool = False) -> dict:
    """Delete unreferenced content-addressed mask objects after a 24-hour grace."""
    return asyncio.run(_run_raster_mask_retention(dry_run=dry_run))


async def _run_raster_mask_retention(*, dry_run: bool = False) -> dict:
    async with task_session() as db:
        workflow = RasterMaskRetentionWorkflow(db)
        return await workflow.purge_unreferenced_raster_masks(dry_run=dry_run)


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
