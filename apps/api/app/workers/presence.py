"""v0.8.3 · 在线状态扫描任务

每 2 分钟扫描一次：把 status='online' 但超 OFFLINE_THRESHOLD_MINUTES 未刷新
last_seen_at 的用户置 'offline'。

仅靠登录/登出事件无法准确反映在线状态——用户直接关浏览器、token 过期、网络
断开都会停留在 'online'。配合前端 30s 心跳 + 5min 阈值，实现接近实时的在线
状态机制。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.services.identity_commands import IdentityCommandService
from app.services.system_settings_service import SystemSettingsService
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.presence.mark_inactive_offline")
def mark_inactive_offline() -> dict:
    return asyncio.run(_run_async())


async def _run_async() -> dict:
    async with task_session() as db:
        return await mark_inactive_offline_with_session(db)


async def mark_inactive_offline_with_session(db) -> dict:
    """以传入 session 执行扫描 + 提交。供 Celery 任务包装与测试复用。"""
    # Resolve the setting at the start of each scheduled scan.  The service's
    # 30-second process cache keeps the original beat cadence while allowing a
    # runtime override to reach the next scan without a worker restart.
    threshold_minutes = await SystemSettingsService.get(db, "offline_threshold_minutes")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    result = await IdentityCommandService(db).mark_inactive_accounts_offline(cutoff)
    await db.commit()
    affected = result.rowcount or 0
    log.info(
        "mark_inactive_offline: affected=%d threshold_minutes=%d",
        affected,
        threshold_minutes,
    )
    return {"affected": affected, "threshold_minutes": threshold_minutes}
