"""Persist Workbench task-time events after a second trust-boundary check.

工作台前端每 N 条 flush，路由 POST /me/task-events:batch 收到后投递到此 task。
broker 不可用时调用方 fallback 同步路径。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.task_events.persist_task_events_batch")
def persist_task_events_batch(payload_list: list[dict[str, Any]]) -> int:
    """批量写入 task_events。

    单条 payload shape：
      {
        id: str (uuid),
        task_id: str,
        user_id: str,
        project_id: str,
        kind: 'annotate' | 'review',
        started_at: ISO8601,
        ended_at: ISO8601,
        duration_ms: int,
        annotation_count: int,
        was_rejected: bool,
        collector_version: str | None,
        collection_coverage: str (qualified, partial, or unverified_collection),
      }
    """
    return asyncio.run(_async_persist(payload_list))


async def _async_persist(payload_list: list[dict[str, Any]]) -> int:
    from app.workers._db import task_session
    from app.services.task_event_ingestion import (
        insert_task_events,
        validate_worker_event,
    )

    if not payload_list:
        return 0

    async with task_session() as session:
        rows: list[dict[str, Any]] = []
        discarded = 0
        for p in payload_list:
            row = await validate_worker_event(session, p)
            if row is not None:
                rows.append(row)
            else:
                discarded += 1

        if not rows:
            if discarded:
                logger.info("discarded %d invalid task-event payload(s)", discarded)
            return 0
        if discarded:
            logger.info("discarded %d invalid task-event payload(s)", discarded)
        try:
            result = await insert_task_events(session, rows)
            if result.conflicts:
                logger.warning(
                    "discarded %d task-event payload(s): duplicate_client_event_conflict ids=%s",
                    len(result.conflicts),
                    sorted(str(event_id) for event_id in result.conflicts),
                )
            return result.inserted
        except Exception:
            await session.rollback()
            logger.warning("persist_task_events_batch commit failed n=%d", len(rows))
            raise
