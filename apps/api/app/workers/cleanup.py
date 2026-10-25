"""Celery cleanup adapters for attachment and raster-mask retention.

软删评论附件 7 天 grace 期后从 MinIO 删除。
annotations 桶的 comment-attachments/ 前缀有 90 天过期兜底，本任务提供更精确的近期清理路径。
"""

from __future__ import annotations

import asyncio
import logging

from app.application.raster_mask_retention import RasterMaskRetentionWorkflow
from app.services.annotation_comment import AnnotationCommentService
from app.services.storage import storage_service
from app.workers._db import task_session, task_session_factory
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.cleanup.purge_soft_deleted_attachments")
def purge_soft_deleted_attachments() -> dict:
    """每日 03:00 UTC 清理已软删超过 7 天的评论附件。

    每次最多选择 500 条评论（包括空附件行）。对象删除失败只记录日志，附件行仍会清空并提交；
    数据库提交失败可回滚行，但不能恢复此前已从 MinIO 删除的对象。
    """
    return asyncio.run(_purge_async())


async def _purge_async() -> dict:
    """领域服务选择评论和清空附件行，worker 编排对象删除及最终提交。"""
    deleted_objects = 0
    processed_comments = 0

    async with task_session() as db:
        comment_service = AnnotationCommentService(db)
        comments = await comment_service.select_expired_attachment_comments()

        for comment in comments:
            attachments = comment.attachments or []
            if not attachments:
                continue
            for att in attachments:
                key = att.get("storage_key") if isinstance(att, dict) else None
                if not key:
                    continue
                try:
                    # Run blocking boto3 deletion in a thread so the async GC loop
                    # is not blocked per object.
                    await asyncio.to_thread(storage_service.delete_object, key)
                    deleted_objects += 1
                except Exception as e:
                    log.warning("delete object %s failed: %s", key, e)
            # 标记已清空，避免下次重复扫
            await comment_service.clear_attachments(comment.id)
            processed_comments += 1

        await db.commit()

    log.info(
        "purge_soft_deleted_attachments done: comments=%d objects=%d",
        processed_comments,
        deleted_objects,
    )
    return {"comments": processed_comments, "objects": deleted_objects}


@celery_app.task(name="app.workers.cleanup.purge_unreferenced_raster_masks")
def purge_unreferenced_raster_masks(
    dry_run: bool = False,
    scan_pages: int = 1,
    delete_limit: int = 100,
    after_key: str | None = None,
) -> dict:
    """Delete unreferenced content-addressed mask objects after a 24-hour grace."""
    return asyncio.run(
        _run_raster_mask_retention(
            dry_run=dry_run,
            scan_pages=scan_pages,
            delete_limit=delete_limit,
            after_key=after_key,
        )
    )


async def _run_raster_mask_retention(
    *,
    dry_run: bool = False,
    scan_pages: int = 1,
    delete_limit: int = 100,
    after_key: str | None = None,
) -> dict:
    async with task_session_factory() as factory:
        async with factory() as db:
            workflow = RasterMaskRetentionWorkflow(db, session_factory=factory)
            return await workflow.purge_unreferenced_raster_masks(
                dry_run=dry_run,
                scan_pages=scan_pages,
                delete_limit=delete_limit,
                after_key=after_key,
            )


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
    """Refresh completed UTC audit days through Audit's owner capability."""
    return asyncio.run(_refresh_audit_bi_mv_async())


async def _refresh_audit_bi_mv_async() -> dict:
    from aap_audit.maintenance_postgres import AuditMaintenancePostgresData

    async with task_session() as db:
        try:
            await AuditMaintenancePostgresData(db).refresh_daily()
            await db.commit()
        except Exception as exc:
            await db.rollback()
            error_type = type(exc).__name__
            log.warning("refresh_audit_bi_mv failed; error_type=%s", error_type)
            return {"refreshed": False, "error_type": error_type}
    log.info("refresh_audit_bi_mv done")
    return {"refreshed": True}
