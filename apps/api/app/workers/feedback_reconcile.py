"""v0.11.0 · ADR-0027 双写一致性对账 Celery beat 任务（A 组安全网）.

reconcile_annotation_feedback: 每日 03:00 UTC（避开 03:30 的分区维护），调纯函数
`compute_feedback_drift` 比对双写一致性；drift>0 时写 audit_logs
（action=FEEDBACK_RECONCILE_DRIFT）+ notify 所有 superadmin。

切单源（v0.11.9+）前用它积累「双写长期一致」的客观证据。本任务无 schema 变更，
回退 = 删任务 + 注销 beat schedule。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.user import User
from app.services.audit import AuditAction, AuditService
from app.services.feedback_reconcile import compute_feedback_drift
from app.services.notification import NotificationService
from app.workers._db import task_session
from app.workers.celery_app import celery_app


log = logging.getLogger(__name__)

# 对账告警通知没有具体目标实体，用固定 nil UUID 占位 target_id。
_RECONCILE_TARGET_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@celery_app.task(name="app.workers.feedback_reconcile.reconcile_annotation_feedback")
def reconcile_annotation_feedback() -> dict:
    return asyncio.run(_reconcile_async())


async def _reconcile_async() -> dict:
    async with task_session() as db:
        result = await run_reconcile(db)
        await db.commit()
        pending = result.pop("pending_notifications", [])
        if pending:
            await NotificationService(db).publish_committed(pending)
    return result


async def run_reconcile(db: AsyncSession) -> dict:
    """对账 + 告警核心逻辑（不管理 session 生命周期，便于单测注入 db）。

    drift>0 时写 audit_logs + notify 所有 superadmin；flush 但不 commit（由调用方
    决定 commit/rollback）。返回 {total_missing, drift, pending_notifications}；
    通知行在调用方 commit 后经 publish_committed 发布。
    """
    drift = await compute_feedback_drift(db)
    total_missing = sum(len(v["missing_ids"]) for v in drift.values())
    pending_notifications: list = []

    if total_missing > 0:
        await AuditService.log(
            db,
            actor=None,
            action=AuditAction.FEEDBACK_RECONCILE_DRIFT,
            target_type="feedback_reconcile",
            detail={"drift": drift, "total_missing": total_missing},
        )
        superadmin_ids = list(
            (
                await db.execute(
                    select(User.id).where(User.role == UserRole.SUPER_ADMIN.value)
                )
            )
            .scalars()
            .all()
        )
        pending_notifications = []
        if superadmin_ids:
            pending_notifications = await NotificationService(db).notify_many(
                user_ids=superadmin_ids,
                type="feedback.reconcile_drift",
                target_type="feedback_reconcile",
                target_id=_RECONCILE_TARGET_ID,
                payload={
                    "total_missing": total_missing,
                    "missing_by_source": {
                        src: len(v["missing_ids"]) for src, v in drift.items()
                    },
                },
            )
        log.warning(
            "reconcile_annotation_feedback: DRIFT detected total_missing=%d %s",
            total_missing,
            drift,
        )
    else:
        log.info("reconcile_annotation_feedback: drift=0 (consistent) %s", drift)

    return {
        "total_missing": total_missing,
        "drift": drift,
        "pending_notifications": pending_notifications,
    }
