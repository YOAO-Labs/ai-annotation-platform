from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from app.config import settings

log = logging.getLogger(__name__)


class ProgressPublisher:
    def __init__(self) -> None:
        self.redis = aioredis.from_url(settings.redis_url)

    async def publish(self, channel: str, data: dict) -> None:
        await self.redis.publish(channel, json.dumps(data))

    async def publish_prediction_progress(
        self,
        project_id: str,
        current: int,
        total: int,
        status: str = "running",
        error: str | None = None,
    ) -> None:
        await self.publish(
            f"project:{project_id}:preannotate",
            {"current": current, "total": total, "status": status, "error": error},
        )

    async def close(self) -> None:
        await self.redis.close()


async def _publish_batch_event(
    project_id: str,
    payload: dict,
    *,
    log_context: str,
) -> None:
    """向 `project:{project_id}:batch` 频道推一条 batch 事件.

    复用一次性 instance + close 模式（无连接池复用）；batch 事件频率远低于预标进度帧，
    这点开销可接受。广播失败不能阻塞业务事务, 只记 warning。
    """
    redis = aioredis.from_url(settings.redis_url)
    try:
        await redis.publish(f"project:{project_id}:batch", json.dumps(payload))
    except Exception as e:
        log.warning(
            "publish batch event failed project=%s ctx=%s err=%s",
            project_id,
            log_context,
            e,
        )
    finally:
        try:
            await redis.close()
        except Exception:
            pass


async def publish_batch_status_change(
    project_id: str,
    batch_id: str,
    from_status: str,
    to_status: str,
) -> None:
    """v0.9.13 · batch 状态变更广播 (B-15 / 多端实时刷新).

    由 BatchService.transition() 与 check_auto_transitions() 在 db.flush() 之后调用.
    频道: `project:{project_id}:batch`. 消费方: 前端 useBatchEventsSocket → invalidate
    ["batches", projectId]. 即便外层事务 commit 失败也只导致一次无效重拉, 不会数据
    不一致 (客户端拉到的是真实 DB 状态).
    """
    payload = {
        "type": "batch.status_changed",
        "batch_id": str(batch_id),
        "from": from_status,
        "to": to_status,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    await _publish_batch_event(
        project_id, payload, log_context=f"status batch={batch_id}"
    )


async def publish_batch_assignment_change(
    project_id: str,
    batch_ids: list[str],
) -> None:
    """issue #124 · batch 标注员 / 质检员分派变更广播.

    issue #124 起「未分派人员的 draft 批次」也进入可预标列表，其准入依赖
    `annotator_id` / `reviewer_id`。改派不改变 `batch.status`，因此不会触发
    `batch.status_changed`；若不单独广播，另一名管理员打开的面板会保留过期的
    可预标项，提交时才被后端 409 拒绝。此处对齐 transition 的实时刷新语义。
    """
    if not batch_ids:
        return
    payload = {
        "type": "batch.assignment_changed",
        "batch_ids": [str(b) for b in batch_ids],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    await _publish_batch_event(
        project_id, payload, log_context=f"assignment batches={len(batch_ids)}"
    )
